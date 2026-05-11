"""Per-position recruiter notes

Revision ID: 0003_recruiter_notes
Revises: 0002_auto_screen
Create Date: 2026-05-12

Adds position_classes.recruiter_notes — free-form text the recruiter types
on the home page for any extra context the AI should keep in mind when
scoring candidates for this position.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_recruiter_notes"
down_revision: str | Sequence[str] | None = "0002_auto_screen"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position_classes",
        sa.Column("recruiter_notes", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("position_classes", "recruiter_notes")
