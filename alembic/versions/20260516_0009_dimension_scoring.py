"""Add per-dimension scoring and per-position weights

Revision ID: 0009_dimension_scoring
Revises: 0008_admin_settings
Create Date: 2026-05-16

Two changes that work together:

  1. position_classes.dimension_weights_json — per-position weight dict
     so recruiters can decide what matters for THIS role before scoring
     (e.g., for Talent Acquisition: company_tier 35%, domain_match 15%;
     for Engineering Lead: domain_match 25%, company_tier 20%).

  2. debug_scoring.dim_* columns — per-dimension sub-scores from each
     scoring call. Lets the calibration UI show the recruiter WHY a
     candidate scored 7/10 instead of just "7/10". Also enables future
     auto-learning of weight suggestions from calibration history.

The six dimensions:
    domain_match        — skills/stack-to-role fit
    company_tier        — tier-1 product vs agency vs unknown
    career_progression  — title/scope growth over time
    location_match      — country / relocation alignment
    university_tier     — tier-1 / tier-2 / other
    achievements        — concrete scale or scope evidence

All scored 1-10 internally. Weighted sum (each weight 0-100, total 100)
gives the overall rating that recruiters see + threshold against.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_dimension_scoring"
down_revision: str | Sequence[str] | None = "0008_admin_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Per-position weight dict. JSON keeps the schema flexible — if we
    # later add a 7th dimension we don't need another migration.
    op.add_column(
        "position_classes",
        sa.Column("dimension_weights_json", sa.JSON(), nullable=True),
    )

    # Per-dimension sub-scores on each scoring call.
    op.add_column("debug_scoring", sa.Column("dim_domain_match", sa.Integer(), nullable=True))
    op.add_column("debug_scoring", sa.Column("dim_company_tier", sa.Integer(), nullable=True))
    op.add_column("debug_scoring", sa.Column("dim_career_progression", sa.Integer(), nullable=True))
    op.add_column("debug_scoring", sa.Column("dim_location_match", sa.Integer(), nullable=True))
    op.add_column("debug_scoring", sa.Column("dim_university_tier", sa.Integer(), nullable=True))
    op.add_column("debug_scoring", sa.Column("dim_achievements", sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in (
        "dim_domain_match",
        "dim_company_tier",
        "dim_career_progression",
        "dim_location_match",
        "dim_university_tier",
        "dim_achievements",
    ):
        op.drop_column("debug_scoring", col)
    op.drop_column("position_classes", "dimension_weights_json")
