"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-03

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate_locks",
        sa.Column("key", sa.String(160), primary_key=True),
        sa.Column("value", sa.Text()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )

    op.create_table(
        "position_classes",
        sa.Column("position_uid", sa.String(64), primary_key=True),
        sa.Column("class_id", sa.String(64), nullable=False),
        sa.Column("class_name", sa.String(120), nullable=False),
        sa.Column("level", sa.String(60)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "custom_position_classes",
        sa.Column("class_id", sa.String(64), primary_key=True),
        sa.Column("class_name", sa.String(120), nullable=False, unique=True),
        sa.Column("levels_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("recruiter_email", sa.String(200)),
        sa.Column("class_id", sa.String(64), nullable=False, index=True),
        sa.Column("class_name", sa.String(120), nullable=False),
        sa.Column("position_uid", sa.String(64), nullable=False, index=True),
        sa.Column("position_name", sa.String(200)),
        sa.Column("candidate_uid", sa.String(64), nullable=False, index=True),
        sa.Column("candidate_name", sa.String(200)),
        sa.Column("ai_rating", sa.Integer()),
        sa.Column("recruiter_rating", sa.Integer()),
        sa.Column("note", sa.Text()),
        sa.CheckConstraint("ai_rating IS NULL OR ai_rating BETWEEN 1 AND 5", name="ck_feedback_ai_rating_range"),
        sa.CheckConstraint("recruiter_rating IS NULL OR recruiter_rating BETWEEN 1 AND 5", name="ck_feedback_rec_rating_range"),
    )
    op.create_index("ix_feedback_class_position", "feedback", ["class_id", "position_uid"])
    op.create_index("ix_feedback_candidate", "feedback", ["candidate_uid"])

    op.create_table(
        "learned_rubrics",
        sa.Column("class_id", sa.String(64), primary_key=True),
        sa.Column("class_name", sa.String(120), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("feedback_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rubric", sa.Text(), nullable=False),
    )

    op.create_table(
        "debug_scoring",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("candidate_uid", sa.String(64), index=True),
        sa.Column("candidate_name", sa.String(200)),
        sa.Column("position_uid", sa.String(64), index=True),
        sa.Column("position_name", sa.String(200)),
        sa.Column("class_id", sa.String(64)),
        sa.Column("anchors_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("anchors_critical", sa.Integer(), server_default="0", nullable=False),
        sa.Column("anchors_block", sa.Text()),
        sa.Column("rubric_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("rubric_snippet", sa.Text()),
        sa.Column("raw_rating", sa.Integer()),
        sa.Column("final_rating", sa.Integer()),
        sa.Column("calibration_delta", sa.Float()),
        sa.Column("arithmetic_applied", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("summary", sa.Text()),
        sa.Column("strengths_json", sa.JSON()),
        sa.Column("gaps_json", sa.JSON()),
    )

    op.create_table(
        "comeet_app_session",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cookies_json", sa.JSON(), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("id = 1", name="ck_comeet_app_session_singleton"),
    )

    op.create_table(
        "tag_catalog",
        sa.Column("name", sa.String(120), primary_key=True),
        sa.Column("comeet_tag_id", sa.Integer(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "applied_tags",
        sa.Column("candidate_uid", sa.String(64), primary_key=True),
        sa.Column("tag_name", sa.String(120), primary_key=True),
        sa.Column("person_id", sa.Integer()),
        sa.Column("applied_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("applied_tags")
    op.drop_table("tag_catalog")
    op.drop_table("comeet_app_session")
    op.drop_table("debug_scoring")
    op.drop_table("learned_rubrics")
    op.drop_index("ix_feedback_candidate", table_name="feedback")
    op.drop_index("ix_feedback_class_position", table_name="feedback")
    op.drop_table("feedback")
    op.drop_table("custom_position_classes")
    op.drop_table("position_classes")
    op.drop_table("candidate_locks")
