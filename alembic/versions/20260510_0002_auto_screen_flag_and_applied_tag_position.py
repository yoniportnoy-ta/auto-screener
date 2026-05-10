"""auto-screen flag + position context on applied_tags

Revision ID: 0002_auto_screen
Revises: 0001_initial_schema
Create Date: 2026-05-10

Adds:
  - position_classes.auto_screen_enabled (recruiter opts a position into the cron)
  - applied_tags.position_uid + .position_name (so tag-change polling knows
    which position context to attribute auto-feedback to)
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_auto_screen"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "position_classes",
        sa.Column(
            "auto_screen_enabled", sa.Boolean(),
            server_default=sa.text("false"), nullable=False,
        ),
    )
    op.add_column("applied_tags", sa.Column("position_uid", sa.String(64), nullable=True))
    op.add_column("applied_tags", sa.Column("position_name", sa.String(200), nullable=True))


def downgrade() -> None:
    op.drop_column("applied_tags", "position_name")
    op.drop_column("applied_tags", "position_uid")
    op.drop_column("position_classes", "auto_screen_enabled")
