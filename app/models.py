"""SQLAlchemy 2.0 ORM models — Postgres replacements for the Apps Script sheet/lock tables.

Mapping from the Apps Script schema:
    candidate_locks      ← SCREEN_NOTE_POSTED_*, SCREENER_SCORE_DONE_*, SCREEN_LAST_REVIEW_*
                           (was the "_Locks" sheet)
    feedback             ← per-class tabs in the feedback spreadsheet
    learned_rubrics      ← _LearnedRubrics tab
    debug_scoring        ← _DebugScoring tab
    position_classes     ← SCREENER_POS_CLASS_* + SCREENER_CUSTOM_CLASSES script properties
    comeet_app_session   ← script property cache (single row, like referral bot)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CandidateLock(Base):
    """Replaces SCORE_DONE / NOTE_POSTED / LAST_REVIEW script-property locks.

    `key` is the lock identifier (e.g. "score_done:9E.AE639", "note_posted:9E.AE639",
    "last_review:DC.E45"); `value` is the cached payload (timestamp, ISO date, etc.).
    """

    __tablename__ = "candidate_locks"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PositionClass(Base):
    """Per-position class assignment (Backend / PM / etc.).

    Replaces SCREENER_POS_CLASS_<position_uid> script properties.
    `auto_screen_enabled` opts the position into the hourly background cron;
    when False (default), the position is training-only.
    """

    __tablename__ = "position_classes"

    position_uid: Mapped[str] = mapped_column(String(64), primary_key=True)
    class_id: Mapped[str] = mapped_column(String(64))
    class_name: Mapped[str] = mapped_column(String(120))
    level: Mapped[str | None] = mapped_column(String(60))
    auto_screen_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CustomPositionClass(Base):
    """Recruiter-created classes that didn't ship in the default catalogue.

    Replaces the SCREENER_CUSTOM_CLASSES JSON list.
    """

    __tablename__ = "custom_position_classes"

    class_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    class_name: Mapped[str] = mapped_column(String(120), unique=True)
    levels_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Feedback(Base):
    """Recruiter ratings used for calibration + rubric synthesis.

    Replaces the per-class tabs in the feedback spreadsheet. Same column shape;
    the class_id column is what used to be the tab name.
    """

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    recruiter_email: Mapped[str | None] = mapped_column(String(200))
    class_id: Mapped[str] = mapped_column(String(64), index=True)
    class_name: Mapped[str] = mapped_column(String(120))
    position_uid: Mapped[str] = mapped_column(String(64), index=True)
    position_name: Mapped[str | None] = mapped_column(String(200))
    candidate_uid: Mapped[str] = mapped_column(String(64), index=True)
    candidate_name: Mapped[str | None] = mapped_column(String(200))
    ai_rating: Mapped[int | None] = mapped_column(Integer)
    recruiter_rating: Mapped[int | None] = mapped_column(Integer)  # = "Verdict" in sheets
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("ai_rating BETWEEN 1 AND 5", name="ck_feedback_ai_rating_range"),
        CheckConstraint("recruiter_rating BETWEEN 1 AND 5", name="ck_feedback_rec_rating_range"),
    )


class LearnedRubric(Base):
    """One row per position class. The rubric is Claude-synthesised from feedback.

    Replaces the _LearnedRubrics tab.
    """

    __tablename__ = "learned_rubrics"

    class_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    class_name: Mapped[str] = mapped_column(String(120))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    feedback_count: Mapped[int] = mapped_column(Integer, default=0)
    rubric: Mapped[str] = mapped_column(Text)


class DebugScoring(Base):
    """One row per scoring call (when SCORING_DEBUG_LOG=1)."""

    __tablename__ = "debug_scoring"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    candidate_uid: Mapped[str | None] = mapped_column(String(64), index=True)
    candidate_name: Mapped[str | None] = mapped_column(String(200))
    position_uid: Mapped[str | None] = mapped_column(String(64), index=True)
    position_name: Mapped[str | None] = mapped_column(String(200))
    class_id: Mapped[str | None] = mapped_column(String(64))

    anchors_used: Mapped[int] = mapped_column(Integer, default=0)
    anchors_critical: Mapped[int] = mapped_column(Integer, default=0)
    anchors_block: Mapped[str | None] = mapped_column(Text)

    rubric_used: Mapped[bool] = mapped_column(Boolean, default=False)
    rubric_snippet: Mapped[str | None] = mapped_column(Text)

    raw_rating: Mapped[int | None] = mapped_column(Integer)
    final_rating: Mapped[int | None] = mapped_column(Integer)
    calibration_delta: Mapped[float | None] = mapped_column(Float)
    arithmetic_applied: Mapped[bool] = mapped_column(Boolean, default=False)

    confidence: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    strengths_json: Mapped[list[str] | None] = mapped_column(JSON)
    gaps_json: Mapped[list[str] | None] = mapped_column(JSON)


class ComeetAppSession(Base):
    """Single-row table storing the active app.comeet.co session.

    Mirrors the referral bot's `comeet_app_session` SQLite table. The CHECK
    constraint pins us to id=1 so the row UPSERTs cleanly.
    """

    __tablename__ = "comeet_app_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cookies_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    csrf_token: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("id = 1", name="ck_comeet_app_session_singleton"),)


class TagCatalog(Base):
    """Cache of known Comeet tag names → numeric IDs.

    Avoids re-creating "AI: Superstar" etc. every time we tag a candidate.
    Filled lazily by the tagging module.
    """

    __tablename__ = "tag_catalog"

    name: Mapped[str] = mapped_column(String(120), primary_key=True)
    comeet_tag_id: Mapped[int] = mapped_column(Integer, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppliedTag(Base):
    """Idempotency record: which (candidate, tag) pairs we've already applied.

    `position_uid` / `position_name` capture the position context at apply time
    so the tag-change feedback poller can attribute auto-feedback to the right
    position class.
    """

    __tablename__ = "applied_tags"

    candidate_uid: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag_name: Mapped[str] = mapped_column(String(120), primary_key=True)
    person_id: Mapped[int | None] = mapped_column(Integer)
    position_uid: Mapped[str | None] = mapped_column(String(64))
    position_name: Mapped[str | None] = mapped_column(String(200))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("candidate_uid", "tag_name", name="uq_applied_tag_candidate_tag"),
    )


__all__ = [
    "Base",
    "CandidateLock",
    "PositionClass",
    "CustomPositionClass",
    "Feedback",
    "LearnedRubric",
    "DebugScoring",
    "ComeetAppSession",
    "TagCatalog",
    "AppliedTag",
]
