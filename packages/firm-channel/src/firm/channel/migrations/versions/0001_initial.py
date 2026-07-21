"""initial schema (firm_channel_messages)

Revision ID: 0001
Revises:
Create Date: 2026-06-29

The baseline migration creates the whole schema straight from the SQLAlchemy metadata, so
``schema.py`` stays the single source of truth.
"""

from __future__ import annotations

from alembic import op

from firm.channel import schema

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema.metadata.create_all(op.get_bind())


def downgrade() -> None:
    schema.metadata.drop_all(op.get_bind())
