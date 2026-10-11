"""initial schema (firm_audits)

Revision ID: 0001
Revises:
Create Date: 2026-06-30
"""

from __future__ import annotations

from alembic import op

from firm.audit import schema

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema.audits.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    schema.audits.drop(op.get_bind(), checkfirst=True)
