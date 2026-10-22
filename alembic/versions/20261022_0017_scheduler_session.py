"""Second Comeet recruiter-app session for the scheduling account

Revision ID: 0017_scheduler_session
Revises: 0016_cv_text_cache
Create Date: 2026-10-22

Interviewer calendars are read through tlv-interviews@riverside.fm, its own Comeet user
with a Google connection; the bot (comeetadmin) has none. Its login is cached in the same
table as the bot's, in row 2, so the two sessions never overwrite each other.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0017_scheduler_session"
down_revision: str | Sequence[str] | None = "0016_cv_text_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_comeet_app_session_singleton", "comeet_app_session", type_="check")
    op.create_check_constraint("ck_comeet_app_session_slot", "comeet_app_session", "id in (1, 2)")


def downgrade() -> None:
    op.execute("delete from comeet_app_session where id <> 1")
    op.drop_constraint("ck_comeet_app_session_slot", "comeet_app_session", type_="check")
    op.create_check_constraint("ck_comeet_app_session_singleton", "comeet_app_session", "id = 1")
