"""CV text cache for the screening plugin

Revision ID: 0016_cv_text_cache
Revises: 0015_industries
Create Date: 2026-10-20

Reading a CV means downloading it from Comeet and extracting its text, which made
every screening batch of 25 take one to two minutes. The text is now kept per
candidate and resume link, and the hourly prewarm cron reads new CVs ahead of
time, so a recruiter's 9:00 batch comes straight from this table.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_cv_text_cache"
down_revision: str | Sequence[str] | None = "0015_industries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cv_text_cache",
        sa.Column("candidate_uid", sa.String(64), primary_key=True),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("cv_text_cache")
