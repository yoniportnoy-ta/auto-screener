"""Add position_uid to learned_rubrics for per-position rubric storage

Revision ID: 0014_position_rubric
Revises: 0013_feedback_rating_range
Create Date: 2026-05-22

Three-layer scoring framework. Until now, learned rubrics were keyed
only by `class_id` — every position in a class shared one rubric.
The new framework introduces per-position rubrics that override the
class rubric once a position has accumulated >= 5 feedback rows of
its own.

Schema change:
  - learned_rubrics.class_id stays the PK for backward compatibility
    of existing class-level rows
  - new nullable `position_uid` column. NULL = class-level rubric
    (the existing rows, cold-start fallback). Non-NULL = position-
    specific rubric (overrides the class rubric for that position).
  - drop the class_id-only PK and replace with composite PK
    (class_id, position_uid_key) where position_uid_key is
    COALESCE(position_uid, '') so the unique constraint works on the
    NULL-as-class-level convention without a fancy partial index.

Read path: try (class_id, position_uid=X) first; if no row, fall back
to (class_id, position_uid=NULL).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_position_rubric"
down_revision: str | Sequence[str] | None = "0013_feedback_rating_range"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add the column nullable (existing rows = class-level, position_uid=NULL).
    op.add_column(
        "learned_rubrics",
        sa.Column("position_uid", sa.String(64), nullable=True),
    )
    # 2. Drop the old class_id-only PK.
    op.drop_constraint("learned_rubrics_pkey", "learned_rubrics", type_="primary")
    # 3. Add a composite PK. We use a generated computed column trick via
    #    COALESCE on lookup to handle the "NULL position_uid means
    #    class-level" convention. Postgres allows NULLs in PK only if
    #    the column is NOT NULL — so backfill NULLs to '' as a sentinel
    #    for class-level rubrics, then make NOT NULL.
    op.execute("UPDATE learned_rubrics SET position_uid = '' WHERE position_uid IS NULL")
    op.alter_column("learned_rubrics", "position_uid", nullable=False, server_default="")
    op.create_primary_key(
        "learned_rubrics_pkey",
        "learned_rubrics",
        ["class_id", "position_uid"],
    )
    # 4. Index for fast position lookups when scoring.
    op.create_index(
        "ix_learned_rubrics_position_uid",
        "learned_rubrics",
        ["position_uid"],
    )


def downgrade() -> None:
    op.drop_index("ix_learned_rubrics_position_uid", table_name="learned_rubrics")
    op.drop_constraint("learned_rubrics_pkey", "learned_rubrics", type_="primary")
    # Collapse position-specific rows back: keep only one row per class_id
    # (the class-level row, where position_uid=''). Drop position-specific
    # rows on downgrade — they're regenerable from feedback.
    op.execute("DELETE FROM learned_rubrics WHERE position_uid <> ''")
    op.alter_column("learned_rubrics", "position_uid", nullable=True, server_default=None)
    op.create_primary_key("learned_rubrics_pkey", "learned_rubrics", ["class_id"])
    op.drop_column("learned_rubrics", "position_uid")
