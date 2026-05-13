"""Cached structured profile per candidate (timeline, LinkedIn, education)

Revision ID: 0005_candidate_enrichment
Revises: 0004_calibration
Create Date: 2026-05-13

Adds a candidate_enrichment table that caches the Claude-extracted
career timeline + education for each candidate, plus the LinkedIn URL
pulled from the Comeet candidate object. Lazy on-demand extraction so
we only pay tokens for candidates a recruiter actually looks at; cache
is keyed by candidate_uid and lives across sessions.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_candidate_enrichment"
down_revision: str | Sequence[str] | None = "0004_calibration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidate_enrichment",
        sa.Column("candidate_uid", sa.String(64), primary_key=True),
        sa.Column("linkedin_url", sa.String(500), nullable=True),
        # career_timeline_json is a list of {company, role, start, end, highlights}
        sa.Column("career_timeline_json", sa.JSON(), nullable=True),
        # education_json is a list of {school, degree, year}
        sa.Column("education_json", sa.JSON(), nullable=True),
        # null when extraction succeeded; set to a short failure reason otherwise.
        # Lets us cache misses too (e.g. no CV available) so we don't retry every page load.
        sa.Column("extraction_error", sa.String(200), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("candidate_enrichment")
