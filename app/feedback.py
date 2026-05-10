"""Recruiter feedback persistence + read helpers.

Replaces the per-class tabs in the feedback Spreadsheet. All entries live in
the single `feedback` table, keyed by (class_id, position_uid, candidate_uid).
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import desc, select

from .db import db_session
from .models import Feedback

log = logging.getLogger(__name__)


@dataclass
class FeedbackEntry:
    """Read-side view of a feedback row — same fields the Apps Script version surfaced."""
    timestamp: datetime
    recruiter_email: str
    class_id: str
    class_name: str
    position_uid: str
    position_name: str
    candidate_uid: str
    candidate_name: str
    ai_rating: int | None
    recruiter_rating: int | None
    note: str

    @property
    def margin(self) -> int:
        if self.ai_rating is None or self.recruiter_rating is None:
            return 0
        return abs(self.ai_rating - self.recruiter_rating)


def save_feedback(
    *,
    class_id: str,
    class_name: str,
    position_uid: str,
    position_name: str,
    candidate_uid: str,
    candidate_name: str,
    ai_rating: int | None,
    recruiter_rating: int | None,
    note: str = "",
    recruiter_email: str = "",
) -> int:
    """Insert a feedback row. Returns the new row id."""
    with db_session() as session:
        row = Feedback(
            class_id=class_id,
            class_name=class_name,
            position_uid=position_uid,
            position_name=position_name,
            candidate_uid=candidate_uid,
            candidate_name=candidate_name,
            ai_rating=ai_rating,
            recruiter_rating=recruiter_rating,
            note=(note or "").strip()[:2000],
            recruiter_email=(recruiter_email or "").strip()[:200],
        )
        session.add(row)
        session.flush()
        log.info(
            "feedback saved id=%s class=%s candidate=%s ai=%s rec=%s",
            row.id, class_id, candidate_uid, ai_rating, recruiter_rating,
        )
        return row.id


def list_feedback_for_class(class_id: str, *, limit: int | None = None) -> list[FeedbackEntry]:
    with db_session() as session:
        stmt = select(Feedback).where(Feedback.class_id == class_id).order_by(desc(Feedback.timestamp))
        if limit:
            stmt = stmt.limit(limit)
        rows = session.scalars(stmt).all()
        return [_to_entry(r) for r in rows]


def list_feedback_for_position(position_uid: str, *, limit: int | None = None) -> list[FeedbackEntry]:
    with db_session() as session:
        stmt = select(Feedback).where(Feedback.position_uid == position_uid).order_by(desc(Feedback.timestamp))
        if limit:
            stmt = stmt.limit(limit)
        rows = session.scalars(stmt).all()
        return [_to_entry(r) for r in rows]


def list_feedback_for_candidate(candidate_uid: str) -> list[FeedbackEntry]:
    with db_session() as session:
        stmt = select(Feedback).where(Feedback.candidate_uid == candidate_uid).order_by(desc(Feedback.timestamp))
        return [_to_entry(r) for r in session.scalars(stmt).all()]


def feedback_count_for_class(class_id: str) -> int:
    with db_session() as session:
        from sqlalchemy import func
        stmt = select(func.count()).select_from(Feedback).where(Feedback.class_id == class_id)
        return int(session.scalar(stmt) or 0)


def saturated_candidate_uids_for_position(
    position_uid: str, *, threshold: int = 3,
) -> set[str]:
    """Candidates that already have >= `threshold` feedback rows for this position.

    Used by the scan flow to skip candidates the recruiter has already rated
    multiple times — same logic as the Apps Script's getSaturatedCandidateUids_.
    """
    with db_session() as session:
        from sqlalchemy import func
        stmt = (
            select(Feedback.candidate_uid, func.count().label("c"))
            .where(Feedback.position_uid == position_uid)
            .group_by(Feedback.candidate_uid)
            .having(func.count() >= threshold)
        )
        rows: Sequence[tuple[str, int]] = session.execute(stmt).all()
        return {row[0] for row in rows if row[0]}


def _to_entry(row: Feedback) -> FeedbackEntry:
    return FeedbackEntry(
        timestamp=row.timestamp,
        recruiter_email=row.recruiter_email or "",
        class_id=row.class_id,
        class_name=row.class_name,
        position_uid=row.position_uid,
        position_name=row.position_name or "",
        candidate_uid=row.candidate_uid,
        candidate_name=row.candidate_name or "",
        ai_rating=row.ai_rating,
        recruiter_rating=row.recruiter_rating,
        note=row.note or "",
    )


__all__ = [
    "FeedbackEntry",
    "save_feedback",
    "list_feedback_for_class",
    "list_feedback_for_position",
    "list_feedback_for_candidate",
    "feedback_count_for_class",
    "saturated_candidate_uids_for_position",
]
