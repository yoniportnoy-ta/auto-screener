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
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .comeet_app_client import ComeetAppClient, ComeetAppError
from .config import settings
from .db import db_session
from .models import AppliedTag, TagCatalog


# Public API candidate.URL looks like:
#   https://app.comeet.co/app/req/{numericPos}/can/{numericCand}
# We extract numericCand because the internal API only accepts numeric candidate IDs.
_NUMERIC_CAND_RE = re.compile(r"/can/(\d+)(?:[/?#]|$)")


def numeric_candidate_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = _NUMERIC_CAND_RE.search(url)
    return m.group(1) if m else None


def resolve_person_id_via_public_api(candidate_uid: str) -> int | None:
    """Fallback resolver when we don't have the candidate URL handy.

    Fetches the candidate via the PUBLIC api.comeet.co (alphanumeric UID works
    there), reads `URL` to get the numeric internal ID, then queries the
    INTERNAL api at /api/v1/candidates/{numeric_id} to get `person`.
    """
    from .comeet_client import ComeetClient

    try:
        with ComeetClient() as pub_client:
            cand = pub_client.get_candidate(candidate_uid)
    except Exception as exc:  # noqa: BLE001
        log.warning("resolve_person_id_via_public_api: public fetch failed for %s: %s", candidate_uid, exc)
        return None
    if not cand:
        return None
    numeric_id = numeric_candidate_id_from_url(cand.get("URL"))
    if not numeric_id:
        log.warning("resolve_person_id_via_public_api: no numeric id in URL for %s", candidate_uid)
        return None
    return ComeetAppClient().resolve_person_id(numeric_id)

log = logging.getLogger(__name__)

# Tag prefix lets us identify our own tags vs. anything a recruiter applied manually.
# Prefix kept for legacy log-output / `remove_rating_tags` filtering. The actual
# Comeet tag names live in RATING_TAG_NAMES below.
AI_TAG_PREFIX = "AI: "

# The exact tag names that already exist in Comeet (manually created by recruiters
# with colors set in the Comeet tag-management UI). The screener never creates
# these — it only assigns them. Mind the spacing inside "Way off ( AI Screener)";
# we match by exact string.
RATING_TAG_NAMES: dict[int, str] = {
    5: "Superstar (AI Screener)",
    4: "Great (AI Screener)",
    3: "OK (AI Screener)",
    2: "Not a fit (AI Screener)",
    1: "Way off ( AI Screener)",
}

# Color is set in Comeet's tag UI when the recruiter creates the tag. The screener
# never overrides it — pass None on every call so get_or_create_persontag won't
# PATCH the color of an existing tag.
RATING_TAG_COLOR: dict[int, str | None] = {k: None for k in RATING_TAG_NAMES}

# Legacy alias kept for backwards-compatibility with any callers that imported it.
RATING_TAG_SUFFIX = RATING_TAG_NAMES


def rating_tag_name(rating: int) -> str:
    name = RATING_TAG_NAMES.get(int(rating))
    if not name:
        raise ValueError(f"unsupported rating {rating!r} (expected 1-5)")
    return name


def rating_tag_color(rating: int) -> str | None:
    return RATING_TAG_COLOR.get(int(rating))


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


def _record_applied_tag(
    candidate_uid: str,
    tag_name: str,
    person_id: int | None,
    *,
    position_uid: str | None = None,
    position_name: str | None = None,
) -> None:
    with db_session() as session:
        stmt = pg_insert(AppliedTag).values(
            candidate_uid=candidate_uid,
            tag_name=tag_name,
            person_id=person_id,
            position_uid=position_uid,
            position_name=position_name,
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
def get_or_create_tag_id(client: ComeetAppClient, name: str, *, color: str | None = None) -> int:
    """Idempotent tag-id resolver. Hits Postgres cache first, then Comeet.

    `color` is the Comeet palette token to apply when creating the tag (and to
    PATCH onto an existing tag if it differs). None leaves the color alone.
    """
    cached = _cached_tag_id(name)
    if cached is not None:
        return cached
    tag = client.get_or_create_persontag(name, color=color)
    tag_id = int(tag["id"])
    _cache_tag_id(name, tag_id)
    return tag_id


def apply_rating_tag(
    candidate_uid: str,
    rating: int,
    *,
    client: ComeetAppClient | None = None,
    person_id: int | None = None,
    candidate_url: str | None = None,
    position_uid: str | None = None,
    position_name: str | None = None,
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
        # Path 1: caller supplied the candidate URL — extract numeric candidate id
        # and look up person via internal api at /api/v1/candidates/{numeric_id}.
        numeric_id = numeric_candidate_id_from_url(candidate_url)
        if numeric_id:
            person_id = owner_client.resolve_person_id(numeric_id)
        # Path 2: alphanumeric uid (rarely works against the internal API, but
        # cheap to try in case Comeet ever accepts it).
        if person_id is None:
            person_id = owner_client.resolve_person_id(candidate_uid)
        # Path 3: fall back to a fresh public-API fetch to recover the URL.
        if person_id is None:
            person_id = resolve_person_id_via_public_api(candidate_uid)
        if person_id is None:
            log.warning("apply_rating_tag: cannot resolve person_id for %s", candidate_uid)
            return None

    # Before applying the new tag, drop any *other* AI Screener tags this
    # candidate carries. Without this, a candidate re-scored from 4→2 would
    # end up with both "Great (AI Screener)" and "Not a fit (AI Screener)"
    # tags simultaneously.
    try:
        removed = _remove_other_ai_tags(
            candidate_uid, keep=tag_name,
            client=owner_client, person_id=person_id,
        )
        if removed:
            log.info("re-score cleanup: removed %d stale AI tag(s) from %s before applying %s",
                     removed, candidate_uid, tag_name)
    except Exception as exc:  # noqa: BLE001
        # Cleanup failure is non-fatal — better to have two tags than to
        # block the new one from landing.
        log.warning("apply_rating_tag: stale-tag cleanup failed for %s: %s", candidate_uid, exc)

    tag_id = get_or_create_tag_id(owner_client, tag_name, color=rating_tag_color(rating))

    try:
        owner_client.assign_tag_to_person(person_id, tag_id)
    except ComeetAppError as exc:
        log.warning("apply_rating_tag: assign failed for %s tag=%s: %s", candidate_uid, tag_name, exc)
        return None

    _record_applied_tag(
        candidate_uid, tag_name, person_id,
        position_uid=position_uid, position_name=position_name,
    )
    log.info("tagged candidate=%s person_id=%s with %s", candidate_uid, person_id, tag_name)

    # Flag management: candidates at or above flag_rating_threshold get
    # is_favorite=true; on a re-score that drops below the threshold we ALSO
    # clear the flag so it actually reflects the current rating.
    if settings.auto_flag_enabled:
        should_flag = int(rating) >= int(settings.flag_rating_threshold)
        numeric_id = numeric_candidate_id_from_url(candidate_url)
        if not numeric_id:
            # Fallback: derive from a fresh public-API fetch.
            from .comeet_client import ComeetClient
            try:
                with ComeetClient() as pub_client:
                    cand = pub_client.get_candidate(candidate_uid)
                if cand:
                    numeric_id = numeric_candidate_id_from_url(cand.get("URL"))
            except Exception as exc:  # noqa: BLE001
                log.warning("flag: could not resolve numeric id for %s: %s", candidate_uid, exc)
        if numeric_id:
            try:
                owner_client.set_candidate_flag(numeric_id, should_flag)
                log.info("flag candidate=%s numeric=%s rating=%d is_favorite=%s",
                         candidate_uid, numeric_id, rating, should_flag)
            except ComeetAppError as exc:
                log.warning("flag failed for %s: %s", candidate_uid, exc)

    return tag_name


def _remove_other_ai_tags(
    candidate_uid: str,
    *,
    keep: str,
    client: ComeetAppClient,
    person_id: int,
) -> int:
    """Drop every AI Screener / AI: tag we've previously applied to this candidate
    EXCEPT `keep`. Used by apply_rating_tag to enforce one-tag-per-candidate.

    Returns the count of tags actually deleted from Comeet.
    """
    rating_names = list(RATING_TAG_NAMES.values())
    with db_session() as session:
        rows = session.scalars(
            select(AppliedTag).where(
                AppliedTag.candidate_uid == candidate_uid,
                AppliedTag.tag_name != keep,
                (AppliedTag.tag_name.startswith(AI_TAG_PREFIX))
                | (AppliedTag.tag_name.in_(rating_names)),
            )
        ).all()
    if not rows:
        return 0

    removed = 0
    for row in rows:
        tag_id = _cached_tag_id(row.tag_name)
        if tag_id is None:
            # Skip silently — without the Comeet tag id we can't delete it
            # anyway, and on the next scan we'll just hit this same branch.
            continue
        try:
            ok = client.remove_tag_from_person(person_id, tag_id)
        except ComeetAppError as exc:
            log.warning("_remove_other_ai_tags: %s on %s: %s", row.tag_name, candidate_uid, exc)
            continue
        if ok:
            removed += 1
            with db_session() as session:
                session.query(AppliedTag).filter(
                    AppliedTag.candidate_uid == candidate_uid,
                    AppliedTag.tag_name == row.tag_name,
                ).delete()
    return removed


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
        # Match any AI-rating tag we've previously applied to this candidate —
        # both legacy "AI: …" names and the current "<verdict> (AI Screener)" set.
        rating_names = list(RATING_TAG_NAMES.values())
        rows = session.scalars(
            select(AppliedTag).where(
                AppliedTag.candidate_uid == candidate_uid,
                (AppliedTag.tag_name.startswith(AI_TAG_PREFIX))
                | (AppliedTag.tag_name.in_(rating_names)),
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
