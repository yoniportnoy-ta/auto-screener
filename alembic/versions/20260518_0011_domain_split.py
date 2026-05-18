"""Split domain_match into company_domain + profession_domain

Revision ID: 0011_domain_split
Revises: 0010_recruiter_rating
Create Date: 2026-05-18

Benchmark feedback (pos 53.B61, n=41) showed the AI conflates "they work
at a tech company" with "they do the same kind of work" — a Senior PM at
a bank has high company tier but low profession adjacency. Splitting the
single `domain_match` axis into two facets lets the AI evaluate both
independently and lets the recruiter weight them per position.

New columns:
  debug_scoring.dim_company_domain     INTEGER NULL
  debug_scoring.dim_profession_domain  INTEGER NULL

`dim_domain_match` stays nullable for historical rows (legacy values
remain queryable for any pre-deprecation analyses).

For PositionClass.dimension_weights_json: no DDL change, the JSON dict
is loose-typed. Application-level `get_weights()` migrates legacy shapes
on read — a stored row with `domain_match` is split equally into
company_domain + profession_domain on the next read.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_domain_split"
down_revision: str | Sequence[str] | None = "0010_recruiter_rating"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "debug_scoring",
        sa.Column("dim_company_domain", sa.Integer(), nullable=True),
    )
    op.add_column(
        "debug_scoring",
        sa.Column("dim_profession_domain", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("debug_scoring", "dim_profession_domain")
    op.drop_column("debug_scoring", "dim_company_domain")
