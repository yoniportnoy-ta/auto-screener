"""Add feedback_text column to calibration_verdicts

Revision ID: 0006_verdict_feedback_text
Revises: 0005_candidate_enrichment
Create Date: 2026-05-13

Recruiters wanted a textarea alongside the 👍 / 👎 / ❓ buttons so they could
record *why* a candidate is a thumbs-down ("too senior for this round",
"wrong stack"). Stored as a freeform note next to the verdict; the rating
bucket still drives threshold math, but the text is captured for review and
later for feeding back into the scoring prompt.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_verdict_feedback_text"
down_revision: str | Sequence[str] | None = "0005_candidate_enrichment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calibration_verdicts",
        sa.Column("feedback_text", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("calibration_verdicts", "feedback_text")
