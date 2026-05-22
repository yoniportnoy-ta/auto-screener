"""Expand feedback rating CHECK constraints from 1-5 to 1-10

Revision ID: 0013_feedback_rating_range
Revises: 0012_broken_axes
Create Date: 2026-05-22

Critical bug discovered during the Poland Full Stack calibration: the
calibration UI has written 1-10 ratings since task #97 (May 2026), but
the feedback table's CHECK constraints still enforced the original
1-5 range. Every dual-write from `_mirror_verdict_to_feedback` was
silently failing the constraint check and being swallowed by the
broad except clause. Net result: rubric synthesis only ever saw
legacy 1-5 ratings, never the new 1-10 verdicts from any calibration
session since May.

Fix: drop the 1-5 CHECK constraints, add new 1-10 CHECK constraints.
No data change — existing 1-5 rows are still valid under the new
constraint.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0013_feedback_rating_range"
down_revision: str | Sequence[str] | None = "0012_broken_axes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_feedback_ai_rating_range", "feedback", type_="check")
    op.drop_constraint("ck_feedback_rec_rating_range", "feedback", type_="check")
    op.create_check_constraint(
        "ck_feedback_ai_rating_range",
        "feedback",
        "ai_rating IS NULL OR (ai_rating >= 1 AND ai_rating <= 10)",
    )
    op.create_check_constraint(
        "ck_feedback_rec_rating_range",
        "feedback",
        "recruiter_rating IS NULL OR (recruiter_rating >= 1 AND recruiter_rating <= 10)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_feedback_ai_rating_range", "feedback", type_="check")
    op.drop_constraint("ck_feedback_rec_rating_range", "feedback", type_="check")
    op.create_check_constraint(
        "ck_feedback_ai_rating_range",
        "feedback",
        "ai_rating BETWEEN 1 AND 5",
    )
    op.create_check_constraint(
        "ck_feedback_rec_rating_range",
        "feedback",
        "recruiter_rating BETWEEN 1 AND 5",
    )
