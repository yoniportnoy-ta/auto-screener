"""Store candidate profile_url on debug_scoring + candidate_enrichment

Revision ID: 0007_profile_url
Revises: 0006_verdict_feedback_text
Create Date: 2026-05-13

The Comeet web app URL (`https://app.comeet.co/app/req/<X>/can/<Y>`) needs
the numeric IDs Comeet's own pages use, not the alphanumeric API uids we
were constructing client-side. We can't reverse-engineer the numeric IDs
locally, but the public candidate object already includes a fully-formed
`URL` field — so we just capture it at scoring time and surface it through
the calibration queue.

Two columns added:
  - debug_scoring.profile_url   — populated on every fresh score
  - candidate_enrichment.profile_url — populated when we extract the CV
    (so old scoring rows that pre-date this migration can still resolve
    a working link via enrichment lookup)
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_profile_url"
down_revision: str | Sequence[str] | None = "0006_verdict_feedback_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "debug_scoring",
        sa.Column("profile_url", sa.String(500), nullable=True),
    )
    op.add_column(
        "candidate_enrichment",
        sa.Column("profile_url", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("candidate_enrichment", "profile_url")
    op.drop_column("debug_scoring", "profile_url")
