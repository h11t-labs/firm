"""initial schema (firm_entries)

Revision ID: 0001
Revises:
Create Date: 2026-06-28
"""

from __future__ import annotations

from alembic import op

from firm.cache import schema

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema.metadata.create_all(op.get_bind())


def downgrade() -> None:
    schema.metadata.drop_all(op.get_bind())
