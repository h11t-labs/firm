"""namespace the channel table to firm_channel_messages

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-21

Renames the released ``firm_messages`` table and its three secondary indexes to the workspace
``firm_<module>_<entity>`` convention — ``firm_messages`` → ``firm_channel_messages``,
``index_firm_messages_on_channel_hash`` → ``index_firm_channel_messages_on_channel_hash``, and so on.

**Idempotent by design.** The 0001 baseline provisions the schema with ``metadata.create_all``,
which reflects the *current* metadata — so on a freshly built chain 0001 already creates the table
under its new ``firm_channel_messages`` name and this revision skips the rename. Each step is guarded
with the inspector in online mode: a fresh database already has the new name and skips everything,
while a real pre-existing 0.x database still carries ``firm_messages`` and is renamed in place with
its rows intact.

**Index renames are drop-and-recreate.** Postgres (``ALTER INDEX … RENAME``), MySQL 8
(``RENAME INDEX``) and SQLite (no rename at all) share no portable index-rename syntax, so each
secondary index is dropped and recreated under the new name — portable across all three dialects.

The old/new names and index columns are derived from the live ``schema.metadata`` (the same source
0001 builds from), so the two never drift.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from firm.channel import schema

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TABLE_PREFIX = "firm_"
_NEW_TABLE_PREFIX = "firm_channel_"
_OLD_INDEX_PREFIX = "index_firm_"
_NEW_INDEX_PREFIX = "index_firm_channel_"


def _old_table(new_name: str) -> str:
    return new_name.replace(_NEW_TABLE_PREFIX, _OLD_TABLE_PREFIX, 1)


def _old_index(new_name: str) -> str:
    return new_name.replace(_NEW_INDEX_PREFIX, _OLD_INDEX_PREFIX, 1)


def _table_renames() -> list[tuple[str, str]]:
    """``(old_name, new_name)`` for every table this package owns, from live metadata."""
    return [(_old_table(t.name), t.name) for t in schema.metadata.sorted_tables]


def _index_renames() -> list[tuple[str, str, str, str, list[str], bool]]:
    """``(old_table, new_table, old_index, new_index, columns, unique)`` from live metadata."""
    out: list[tuple[str, str, str, str, list[str], bool]] = []
    for table in schema.metadata.sorted_tables:
        for idx in table.indexes:
            cols = [str(c.name) for c in idx.columns]
            new_index = str(idx.name)
            out.append(
                (
                    _old_table(table.name),
                    str(table.name),
                    _old_index(new_index),
                    new_index,
                    cols,
                    bool(idx.unique),
                )
            )
    return out


def _inspector() -> sa.Inspector | None:
    """The live inspector, or ``None`` in offline (``--sql``) mode where nothing can be reflected
    and every step is emitted unconditionally (the real 0001→0002 legacy upgrade)."""
    if context.is_offline_mode():
        return None
    return sa.inspect(op.get_bind())


def _has_table(insp: sa.Inspector | None, table: str) -> bool:
    return insp is not None and table in insp.get_table_names()


def _has_index(insp: sa.Inspector | None, table: str, index: str) -> bool:
    return (
        insp is not None
        and _has_table(insp, table)
        and any(i["name"] == index for i in insp.get_indexes(table))
    )


def upgrade() -> None:
    insp = _inspector()

    for old, new in _table_renames():
        if insp is None or (_has_table(insp, old) and not _has_table(insp, new)):
            op.rename_table(old, new)

    insp = _inspector()  # reflect the renamed tables before touching their indexes
    for _old_tbl, new_tbl, old_idx, new_idx, cols, unique in _index_renames():
        if insp is None or _has_index(insp, new_tbl, old_idx):
            op.drop_index(old_idx, table_name=new_tbl)
        if insp is None or (_has_table(insp, new_tbl) and not _has_index(insp, new_tbl, new_idx)):
            op.create_index(new_idx, new_tbl, cols, unique=unique)


def downgrade() -> None:
    insp = _inspector()

    for _old_tbl, new_tbl, _old_idx, new_idx, _cols, _unique in _index_renames():
        if insp is None or _has_index(insp, new_tbl, new_idx):
            op.drop_index(new_idx, table_name=new_tbl)

    for old, new in _table_renames():
        if insp is None or (_has_table(insp, new) and not _has_table(insp, old)):
            op.rename_table(new, old)

    insp = _inspector()  # reflect the restored old table names before recreating their indexes
    for old_tbl, _new_tbl, old_idx, _new_idx, cols, unique in _index_renames():
        if insp is None or (_has_table(insp, old_tbl) and not _has_index(insp, old_tbl, old_idx)):
            op.create_index(old_idx, old_tbl, cols, unique=unique)
