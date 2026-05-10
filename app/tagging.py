"""High-level tagging — apply rating tags to Comeet candidates after scoring.

Wires the internal Comeet API (ComeetAppClient) to the screener's rating model.
Idempotent: re-running a scan won't re-tag candidates that already carry the
right rating tag. Records every applied tag in the `applied_tags` table for
auditability.

Entry points used by the rest of the app:
    apply_rating_tag(candidate_uid, rating)  → tag string ("AI: Great") or None on skip
    remove_rating_tags(candidate_uid)        → drop any AI:* tag(s) before reapplying
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .comeet_app_client import ComeetAppClient, ComeetAppError
from .config import settings
from .db import db_session
from .models import AppliedTag, TagCatalog

log = logging.getLogger(__name__)

# Tag prefix lets us identify our own tags vs. anything a recruiter applied manually.
AI_TAG_PREFIX = "AI: "

# Rating → suffix. Same vocabulary the existing screener uses for note headlines.
RATING_TAG_SUFFIX: dict[int, str] = {
    5: "Superstar",
    4: "Great",
    3: "OK",
    2: "Not a fit",
    1: "Way off",
}


def rating_tag_name(rating: int) -> str:
    suffix = RATING_TAG_SUFFIX.get(int(rating))
    if not suffix:
        raise ValueError(f"unsupported rating {rating!r} (expected 1-5)")
    return f"{AI_TAG_PREFIX}{suffix}"


# ─── Tag-id cache (Postgres) ─────────────────────────────────────────────────
def _cached_tag_id(name: str) -> int | None:
    with db_session() as session:
        row = session.scalar(select(TagCatalog).where(TagCatalog.name == name))
        return row.comeet_tag_id if row else None


def _cache_tag_id(name: str, comeet_tag_id: int) -> None:
    with db_session() as session:
        stmt = pg_insert(TagCatalog).values(name=name, comeet_tag_id=comeet_tag_id)
        stmt = stmt.on_conflict_do_update(
            index_elements=[TagCatalog.name],
            set_={"comeet_tag_id": stmt.excluded.comeet_tag_id},
        )
        session.execute(stmt)


def _record_applied_tag(candidate_uid: str, tag_name: str, person_id: int | None) -> None:
    with db_session() as session:
        stmt = pg_insert(AppliedTag).values(
            candidate_uid=candidate_uid, tag_name=tag_name, person_id=person_id,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[AppliedTag.candidate_uid, AppliedTag.tag_name],
        )
        session.execute(stmt)


def _was_tag_applied(candidate_uid: str, tag_name: str) -> bool:
    with db_session() as session:
        return session.scalar(
            select(AppliedTag).where(
                AppliedTag.candidate_uid == candidate_uid,
                AppliedTag.tag_name == tag_name,
            )
        ) is not None


# ─── Public helpers ──────────────────────────────────────────────────────────
def get_or_create_tag_id(client: ComeetAppClient, name: str) -> int:
    """Idempotent tag-id resolver. Hits Postgres cache first, then Comeet."""
    cached = _cached_tag_id(name)
    if cached is not None:
        return cached
    tag = client.get_or_create_persontag(name)
    tag_id = int(tag["id"])
    _cache_tag_id(name, tag_id)
    return tag_id


def apply_rating_tag(
    candidate_uid: str,
    rating: int,
    *,
    client: ComeetAppClient | None = None,
    person_id: int | None = None,
    force: bool = False,
) -> str | None:
    """Apply the `AI: <verdict>` tag for the given rating to the candidate.

    Returns the applied tag name on success, or None if the operation was
    skipped (auto-tagging disabled, rating below threshold, or already applied).

    `person_id` may be provided when the caller already knows it (e.g. from a
    previous `resolve_person_id` call) — otherwise we resolve it here.
    """
    if not settings.auto_tag_enabled and not force:
        log.debug("apply_rating_tag: AUTO_TAG_ENABLED=0; skipping")
        return None
    if int(rating) < settings.tag_rating_threshold and not force:
        log.debug("apply_rating_tag: rating %s < threshold %s; skipping", rating, settings.tag_rating_threshold)
        return None

    tag_name = rating_tag_name(rating)
    if not force and _was_tag_applied(candidate_uid, tag_name):
        log.debug("apply_rating_tag: %s already applied to %s; skipping", tag_name, candidate_uid)
        return None

    owner_client = client or ComeetAppClient()
    if person_id is None:
        person_id = owner_client.resolve_person_id(candidate_uid)
        if person_id is None:
            log.warning("apply_rating_tag: cannot resolve person_id for %s", candidate_uid)
            return None

    tag_id = get_or_create_tag_id(owner_client, tag_name)

    try:
        owner_client.assign_tag_to_person(person_id, tag_id)
    except ComeetAppError as exc:
        log.warning("apply_rating_tag: assign failed for %s tag=%s: %s", candidate_uid, tag_name, exc)
        return None

    _record_applied_tag(candidate_uid, tag_name, person_id)
    log.info("tagged candidate=%s person_id=%s with %s", candidate_uid, person_id, tag_name)
    return tag_name


def remove_rating_tags(
    candidate_uid: str,
    *,
    client: ComeetAppClient | None = None,
    person_id: int | None = None,
) -> int:
    """Drop any AI:* tag(s) currently applied to this candidate (per our DB log).

    Useful when a candidate is re-rated and we want to replace their old tag
    with a new one. Returns the number of tags removed.

    Note: this only removes tags WE applied (recorded in applied_tags). Tags
    a recruiter manually added stay put.
    """
    owner_client = client or ComeetAppClient()
    with db_session() as session:
        rows = session.scalars(
            select(AppliedTag).where(
                AppliedTag.candidate_uid == candidate_uid,
                AppliedTag.tag_name.startswith(AI_TAG_PREFIX),
            )
        ).all()
    if not rows:
        return 0

    if person_id is None:
        # Use the recorded person_id if we have one, else resolve fresh.
        for row in rows:
            if row.person_id:
                person_id = row.person_id
                break
        if person_id is None:
            person_id = owner_client.resolve_person_id(candidate_uid)
        if person_id is None:
            log.warning("remove_rating_tags: cannot resolve person_id for %s", candidate_uid)
            return 0

    removed = 0
    for row in rows:
        tag_id = _cached_tag_id(row.tag_name)
        if tag_id is None:
            log.debug("remove_rating_tags: no cached id for %s; skipping", row.tag_name)
            continue
        try:
            ok = owner_client.remove_tag_from_person(person_id, tag_id)
        except ComeetAppError as exc:
            log.warning("remove_rating_tags: %s on %s: %s", row.tag_name, candidate_uid, exc)
            continue
        if ok:
            removed += 1
            with db_session() as session:
                session.query(AppliedTag).filter(
                    AppliedTag.candidate_uid == candidate_uid,
                    AppliedTag.tag_name == row.tag_name,
                ).delete()
    return removed


def replace_rating_tag(
    candidate_uid: str,
    new_rating: int,
    *,
    client: ComeetAppClient | None = None,
) -> str | None:
    """Atomic-ish: drop existing AI:* tags then apply the new one.

    Convenient wrapper for the rescore flow where a candidate's rating has
    changed since we last tagged them.
    """
    owner_client = client or ComeetAppClient()
    person_id = owner_client.resolve_person_id(candidate_uid)
    if person_id is None:
        log.warning("replace_rating_tag: cannot resolve person_id for %s", candidate_uid)
        return None
    remove_rating_tags(candidate_uid, client=owner_client, person_id=person_id)
    return apply_rating_tag(
        candidate_uid, new_rating,
        client=owner_client, person_id=person_id,
    )


__all__ = [
    "AI_TAG_PREFIX",
    "RATING_TAG_SUFFIX",
    "rating_tag_name",
    "get_or_create_tag_id",
    "apply_rating_tag",
    "remove_rating_tags",
    "replace_rating_tag",
]
