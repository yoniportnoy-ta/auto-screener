"""Per-recruiter calibration: thresholds + verdicts

Revision ID: 0004_calibration
Revises: 0003_recruiter_notes
Create Date: 2026-05-13

Adds two tables that back the new thumbs-up / thumbs-down / ❓ calibration
flow on the home page:

- recruiter_thresholds: per (recruiter_name, position_uid) — the lowest AI
  rating this recruiter has ever 👍'd and the highest they've ever 👎'd.
  These cutoffs define their personal 👍/❓/👎 buckets for that role.

- calibration_verdicts: one row per thumb click. Drives the threshold
  computation, the agreement metric, and the "don't show me a candidate
  I've already verdicted" logic.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_calibration"
down_revision: str | Sequence[str] | None = "0003_recruiter_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recruiter_thresholds",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("recruiter_name", sa.String(200), nullable=False, index=True),
        sa.Column("position_uid", sa.String(64), nullable=False, index=True),
        # Lowest AI rating this recruiter has ever 👍'd for this position.
        # Candidates at or above this rating are bucketed as 👍.
        sa.Column("thumbs_up_min_rating", sa.Integer(), nullable=True),
        # Highest AI rating this recruiter has ever 👎'd. At or below = 👎.
        # Everything strictly between thumbs_down_max and thumbs_up_min is ❓.
        sa.Column("thumbs_down_max_rating", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.UniqueConstraint("recruiter_name", "position_uid", name="uq_recruiter_threshold_pair"),
    )

    op.create_table(
        "calibration_verdicts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("recruiter_name", sa.String(200), nullable=False, index=True),
        sa.Column("position_uid", sa.String(64), nullable=False, index=True),
        sa.Column("candidate_uid", sa.String(64), nullable=False, index=True),
        # 'up' | 'down' | 'question'
        sa.Column("verdict", sa.String(16), nullable=False),
        # Snapshot of the AI's rating + confidence at the time of the verdict,
        # so threshold logic doesn't need to re-fetch from DebugScoring later.
        sa.Column("ai_rating", sa.Integer(), nullable=True),
        sa.Column("ai_confidence", sa.Float(), nullable=True),
        # Did the recruiter's verdict match what the AI's bucket was at the
        # time? Captured here so we can compute round-by-round agreement
        # without re-running the bucketization later.
        sa.Column("agreed_at_time", sa.Boolean(), nullable=True),
        # Which calibration round (1-indexed) this verdict belongs to. Useful
        # for charting agreement growth across rounds.
        sa.Column("round_num", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "verdict IN ('up', 'down', 'question')",
            name="ck_calibration_verdict_value",
        ),
    )
    op.create_index(
        "ix_calibration_verdicts_session",
        "calibration_verdicts",
        ["recruiter_name", "position_uid"],
    )


def downgrade() -> None:
    op.drop_index("ix_calibration_verdicts_session", table_name="calibration_verdicts")
    op.drop_table("calibration_verdicts")
    op.drop_table("recruiter_thresholds")
