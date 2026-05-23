"""Per-position industry preferences (favor / discount)

Revision ID: 0015_industries
Revises: 0014_position_rubric
Create Date: 2026-05-23

Adds position_classes.industries_up and position_classes.industries_down —
free-text fields the recruiter fills out during position briefing to tell
the AI which industries to weight upward and which to discount.

Motivation: the 76.85A trial calibration produced 24+ broken_axes flags
naming company_tier / company_domain with feedback consistently saying
"non-realtime SaaS / fintech-heavy / banking / cybersec" etc. The recruiter
was repeating the same industry signal 12+ times in per-candidate feedback
when it could have been stated ONCE at brief-time and applied to every
candidate. These two columns are the structured place to put that signal.

Both columns are TEXT nullable — empty/null = no preference set, treat
exactly like the existing recruiter_notes field. Storage is human-readable
free text (one industry per line, comma-separated, whatever the recruiter
types) since the AI is the consumer and it'll parse natural language fine.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_industries"
down_revision: str | Sequence[str] | None = "0014_position_rubric"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position_classes",
        sa.Column("industries_up", sa.Text(), nullable=True),
    )
    op.add_column(
        "position_classes",
        sa.Column("industries_down", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("position_classes", "industries_down")
    op.drop_column("position_classes", "industries_up")
