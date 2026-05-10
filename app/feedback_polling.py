"""Tag-change polling — passive feedback capture.

When the autoscanner applies an `AI: …` rating tag to a candidate, the recruiter
may disagree and swap it for a different rating tag in Comeet's UI. This module
detects those swaps and records them as feedback rows automatically — same
shape as a manual UI feedback submission, with `recruiter_email='auto:tag-change'`.

Flow:
  1. Read every applied_tags row.
  2. For each, fetch the candidate's current tags via the public Comeet API.
  3. If our tag is gone AND a different rating tag is present, that's a recruiter
     correction → save_feedback with old_rating → new_rating.
  4. Drop the applied_tags row so we don't keep observing the same change.

Idempotency:
  - We stop tracking a candidate-tag pair once we've recorded one feedback row.
  - The candidate could later be re-scored and re-tagged; that creates a fresh
    applied_tags row with a new tag/timestamp and the cycle resumes.

CLI: `python -m app.cli poll-feedback`
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from .comeet_client import ComeetClient, ComeetError
from .db import db_session
from .feedback import save_feedback
from .models import AppliedTag
from .position_classes import get_position_class
from .tagging import RATING_TAG_NAMES

log = logging.getLogger(__name__)


@dataclass
class PollResult:
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    candidates_checked: int = 0
    feedback_recorded: int = 0
    tags_dropped: int = 0
    errors: list[str] = field(default_factory=list)


def _name_to_rating() -> dict[str, int]:
    return {name: rating for rating, name in RATING_TAG_NAMES.items()}


def _extract_tag_names(candidate_payload: dict) -> list[str]:
    """Public-API candidate.tags is a list of dicts (or strings) with `name`."""
    out: list[str] = []
    for t in candidate_payload.get("tags") or []:
        if isinstance(t, dict):
            n = (t.get("name") or "").strip()
        else:
            n = str(t or "").strip()
        if n:
            out.append(n)
    return out


def poll_tag_changes() -> PollResult:
    """One pass over all applied_tags. Returns a summary; never raises."""
    import time

    start = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    result = PollResult(started_at=started_at)

    rating_lookup = _name_to_rating()
    rating_names = set(rating_lookup.keys())

    with db_session() as ses:
        rows = list(ses.scalars(select(AppliedTag)).all())
    if not rows:
        result.finished_at = datetime.now(timezone.utc).isoformat()
        result.duration_s = round(time.monotonic() - start, 2)
        return result

    # Group by candidate so we issue one API call per candidate, not per (cand,tag).
    by_candidate: dict[str, list[AppliedTag]] = {}
    for row in rows:
        by_candidate.setdefault(row.candidate_uid, []).append(row)

    try:
        client_cm = ComeetClient()
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"comeet client init: {exc}")
        result.finished_at = datetime.now(timezone.utc).isoformat()
        return result

    with client_cm as client:
        for cand_uid, applied_rows in by_candidate.items():
            result.candidates_checked += 1
            try:
                candidate = client.get_candidate(cand_uid)
            except ComeetError as exc:
                result.errors.append(f"{cand_uid}: comeet fetch: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{cand_uid}: {exc}")
                continue

            if not candidate:
                _drop_applied_tag_rows(applied_rows)
                result.tags_dropped += len(applied_rows)
                continue

            current_tags = _extract_tag_names(candidate)
            current_rating_tags = [n for n in current_tags if n in rating_names]
            cand_name = " ".join(
                p for p in (
                    (candidate.get("first_name") or "").strip(),
                    (candidate.get("last_name") or "").strip(),
                ) if p
            )

            for row in applied_rows:
                # If our tag is still present, no change.
                if row.tag_name in current_tags:
                    continue

                # Tag was removed. Was a different rating tag put in its place?
                # Pick any other rating tag on the candidate that we did NOT apply.
                other_rating_tag = next(
                    (n for n in current_rating_tags if n != row.tag_name),
                    None,
                )
                old_rating = rating_lookup.get(row.tag_name)

                if other_rating_tag and old_rating is not None:
                    new_rating = rating_lookup[other_rating_tag]
                    if new_rating != old_rating:
                        if _record_auto_feedback(
                            row=row, candidate_name=cand_name,
                            old_rating=old_rating, new_rating=new_rating,
                            note=f"detected via tag swap: {row.tag_name} → {other_rating_tag}",
                        ):
                            result.feedback_recorded += 1

                # Whether or not we recorded feedback, drop the applied_tags row —
                # we won't keep checking a tag that's no longer on the candidate.
                _drop_applied_tag_row(row)
                result.tags_dropped += 1

    result.finished_at = datetime.now(timezone.utc).isoformat()
    result.duration_s = round(time.monotonic() - start, 2)
    log.info(
        "tag-change poll done: checked=%d feedback=%d dropped=%d errors=%d duration=%.1fs",
        result.candidates_checked, result.feedback_recorded, result.tags_dropped,
        len(result.errors), result.duration_s,
    )
    return result


def _record_auto_feedback(
    *,
    row: AppliedTag,
    candidate_name: str,
    old_rating: int,
    new_rating: int,
    note: str,
) -> bool:
    """Save a feedback row corresponding to a recruiter's tag swap."""
    cls = get_position_class(row.position_uid) if row.position_uid else None
    if not cls:
        # Without a class we can't attribute the feedback to a class tab — skip.
        log.info(
            "tag-change: no class for position %s on candidate %s; skipping feedback",
            row.position_uid, row.candidate_uid,
        )
        return False
    try:
        save_feedback(
            class_id=cls["classId"],
            class_name=cls["className"],
            position_uid=row.position_uid or "",
            position_name=row.position_name or "",
            candidate_uid=row.candidate_uid,
            candidate_name=candidate_name,
            ai_rating=old_rating,
            recruiter_rating=new_rating,
            note=note,
            recruiter_email="auto:tag-change",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("tag-change: save_feedback failed for %s: %s", row.candidate_uid, exc)
        return False


def _drop_applied_tag_row(row: AppliedTag) -> None:
    with db_session() as ses:
        ses.query(AppliedTag).filter(
            AppliedTag.candidate_uid == row.candidate_uid,
            AppliedTag.tag_name == row.tag_name,
        ).delete()


def _drop_applied_tag_rows(rows: Iterable[AppliedTag]) -> None:
    for r in rows:
        _drop_applied_tag_row(r)


__all__ = ["PollResult", "poll_tag_changes"]
