"""Add admin_settings k/v table for global controls

Revision ID: 0008_admin_settings
Revises: 0007_profile_url
Create Date: 2026-05-14

Stores admin-level controls that override or supplement per-position
settings. Currently two keys:

  - admin_thumbs_up_floor : str (integer 1-5) — global minimum 👍 floor
    that's applied on top of per-recruiter computed thresholds. The
    effective floor is `max(per_recruiter_min, admin_floor)`.

  - admin_brief : str — global free-text guidance appended to every
    scoring prompt regardless of position. E.g. "All hires must be IC,
    not management" or "Skip anyone currently at our direct competitors."

K/V shape (instead of a proper schema) because the set of admin levers
is going to keep growing and migration-per-lever is too much ceremony
for an internal tool. We only ever have a handful of rows.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_admin_settings"
down_revision: str | Sequence[str] | None = "0007_profile_url"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("admin_settings")
