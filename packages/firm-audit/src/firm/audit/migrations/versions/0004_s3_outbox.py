"""Outbox for S3 audit events staged inside a caller's SQL transaction.

Revision ID: 0004
Revises: 0003
"""

from alembic import op
from firm.audit import schema

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema.s3_outbox.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    schema.s3_outbox.drop(op.get_bind(), checkfirst=True)
