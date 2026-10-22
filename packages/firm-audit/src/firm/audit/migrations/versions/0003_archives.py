"""Durable audit archive policy, batches, and exact membership.

Revision ID: 0003
Revises: 0002
"""

from alembic import op
from firm._core.dialects import get_dialect
from firm.audit import schema

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in (
        schema.archive_policy,
        schema.archive_batches,
        schema.archive_members,
        schema.archive_imports,
    ):
        table.create(op.get_bind(), checkfirst=True)
    conn = op.get_bind()
    conn.execute(
        get_dialect(conn.engine).insert_ignore(
            schema.archive_policy, {"id": "1"}, index_elements=("id",)
        )
    )


def downgrade() -> None:
    for table in (
        schema.archive_imports,
        schema.archive_members,
        schema.archive_batches,
        schema.archive_policy,
    ):
        table.drop(op.get_bind(), checkfirst=True)
