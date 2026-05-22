"""Add broken_axes_json to feedback + calibration_verdicts

Revision ID: 0012_broken_axes
Revises: 0011_domain_split
Create Date: 2026-05-22

New per-axis disagreement tag captured by the calibration UI when
|recruiter - ai| >= 1.5. JSON array of axis ids, e.g.
  ["company_domain", "profession_domain"]

Valid axis ids (validated at the API layer):
  - profession_domain
  - company_domain
  - company_tier
  - career_progression
  - university_tier

Empty list / NULL means either "below threshold" or "AI got every axis right".

Stored on BOTH tables:
  - calibration_verdicts.broken_axes_json — authoritative log
  - feedback.broken_axes_json             — mirror for the rubric pipeline,
                                            which only reads `feedback`
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_broken_axes"
down_revision: str | Sequence[str] | None = "0011_domain_split"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "feedback",
        sa.Column("broken_axes_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "calibration_verdicts",
        sa.Column("broken_axes_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("calibration_verdicts", "broken_axes_json")
    op.drop_column("feedback", "broken_axes_json")
