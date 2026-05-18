"""Add recruiter_rating column to calibration_verdicts

Revision ID: 0010_recruiter_rating
Revises: 0009_dimension_scoring
Create Date: 2026-05-17

Per-candidate 1-10 ground truth from the recruiter. The existing
`verdict` ('up' / 'down' / 'question') is still derived from this rating
(1-3 = down, 4-6 = question, 7-10 = up) so legacy threshold math keeps
working. The new column lets us measure |AI - recruiter| delta directly
— the right metric for tuning the scoring algorithm against a benchmark.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_recruiter_rating"
down_revision: str | Sequence[str] | None = "0009_dimension_scoring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "calibration_verdicts",
        sa.Column("recruiter_rating", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("calibration_verdicts", "recruiter_rating")
