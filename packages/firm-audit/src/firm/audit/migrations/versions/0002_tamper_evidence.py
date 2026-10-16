"""tamper-evidence: rename to firm_audit_events, row MAC columns + seals + verify-status

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-19

First renames the released 0.1.0 ``firm_audits`` table (and its five secondary indexes) to the
workspace ``firm_<module>_<entity>`` convention — ``firm_audit_events`` — then adds Layer 1's
nullable ``entry_id``/``row_mac``/``key_id`` columns (with the unique index on ``entry_id``), plus
the ``firm_audit_seals`` (Layer 2) and ``firm_audit_verify_status`` tables. Nullable columns are
zero-downtime; the two new tables are empty until the opt-in sealer/verifier write to them, so a
key-less deployment sees no behavior change (design "Schema & migration").

**Idempotent by design.** The 0001 baseline provisions the schema with ``metadata.create_all``,
which reflects the *current* metadata — so on a freshly built chain 0001 already creates the table
under its new name ``firm_audit_events`` with these columns/tables, and this revision must not fail
trying to rename or re-add them. Each step therefore acts only on what a given database still has
in the old shape (checked with the inspector in online mode): a fresh chain has ``firm_audit_events``
already and skips every step, while a *real* pre-existing 0.1.0 database still carries ``firm_audits``
with rows that predate tamper-evidence — there the table is renamed in place (rows intact) and the
columns/tables/index are genuinely absent and get added.

**Index renames are drop-and-recreate.** Postgres (``ALTER INDEX … RENAME``), MySQL 8
(``RENAME INDEX``) and SQLite (no rename at all) share no index-rename syntax, so each secondary
index is dropped and recreated under the new name — portable everywhere.

**The ``entry_id`` unique index is not zero-downtime by default** (design outside voice #9). On
Postgres it is built with ``CREATE UNIQUE INDEX CONCURRENTLY`` inside an autocommit block, so it
does not lock the table. MySQL and SQLite have no concurrent build: the index is created inline
and its build **blocks writes for a duration that scales with table size** — run this migration in
a quiet window on a large ``firm_audit_events`` table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from firm._core.schema import dt_type, pk_bigint

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_TABLE = "firm_audits"
_TABLE = "firm_audit_events"
_ENTRY_ID_INDEX = "index_firm_audit_events_on_entry_id"

# The five secondary indexes the 0.1.0 baseline shipped, as ``(old name, new name, columns)``.
# Renamed by drop-and-recreate (no portable ALTER INDEX RENAME across our three dialects).
_INDEX_RENAMES: tuple[tuple[str, str, list[str]], ...] = (
    ("index_firm_audits_on_subject", "index_firm_audit_events_on_subject", ["subject_type", "subject_id"]),
    ("index_firm_audits_on_actor", "index_firm_audit_events_on_actor", ["actor_type", "actor_id"]),
    ("index_firm_audits_on_action", "index_firm_audit_events_on_action", ["action"]),
    ("index_firm_audits_on_correlation_id", "index_firm_audit_events_on_correlation_id", ["correlation_id"]),
    ("index_firm_audits_on_created_at", "index_firm_audit_events_on_created_at", ["created_at"]),
)


def _inspector() -> sa.Inspector | None:
    """The live inspector, or ``None`` in offline (``--sql``) mode where nothing can be reflected
    and every step is emitted unconditionally (the real 0001→0002 upgrade adds everything)."""
    if context.is_offline_mode():
        return None
    return sa.inspect(op.get_bind())


def _has_column(insp: sa.Inspector | None, table: str, column: str) -> bool:
    return insp is not None and any(c["name"] == column for c in insp.get_columns(table))


def _has_table(insp: sa.Inspector | None, table: str) -> bool:
    return insp is not None and table in insp.get_table_names()


def _has_index(insp: sa.Inspector | None, table: str, index: str) -> bool:
    return insp is not None and any(i["name"] == index for i in insp.get_indexes(table))


def _swap_indexes(
    insp: sa.Inspector | None,
    table: str,
    pairs: Sequence[tuple[str, str, list[str]]],
) -> None:
    """Drop each ``drop_name`` index and recreate it as ``create_name`` on ``table`` — the portable
    stand-in for an index rename (see the module docstring). Each side is inspector-guarded so a
    re-run is a no-op; in offline mode both statements are emitted for the legacy upgrade path."""
    for drop_name, create_name, columns in pairs:
        if insp is None or _has_index(insp, table, drop_name):
            op.drop_index(drop_name, table_name=table)
        if insp is None or not _has_index(insp, table, create_name):
            op.create_index(create_name, table, columns)


def _create_seals() -> None:
    op.create_table(
        "firm_audit_seals",
        sa.Column("id", pk_bigint(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("from_id", sa.BigInteger()),
        sa.Column("to_id", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.Integer()),
        sa.Column("rows_mac", sa.String(64)),
        sa.Column("seal_mac", sa.String(64), nullable=False),
        sa.Column("sealed_at", dt_type(), nullable=False),
        sa.Column("key_id", sa.String(16), nullable=False),
    )
    op.create_index(
        "index_firm_audit_seals_on_from_id", "firm_audit_seals", ["from_id"], unique=True
    )


def _create_verify_status() -> None:
    op.create_table(
        "firm_audit_verify_status",
        sa.Column("id", pk_bigint(), primary_key=True),
        sa.Column("ran_at", dt_type(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("ok_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("unprotected_count", sa.Integer(), nullable=False),
        sa.Column("tampered_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("last_full_coverage_at", dt_type()),
        sa.Column("newest_anchor_at", dt_type()),
        sa.Column("anchor_configured", sa.Boolean(), nullable=False),
        sa.Column("sealing_observed", sa.Boolean(), nullable=False),
        sa.Column("unsealed_tail_count", sa.Integer(), nullable=False),
        sa.Column("unsealed_tail_oldest_at", dt_type()),
        sa.Column("affected_identifiers", sa.Text()),
        sa.Column("duration_seconds", sa.Float()),
    )


def _create_entry_id_index() -> None:
    """Unique index on ``firm_audit_events.entry_id`` — CONCURRENTLY on Postgres (no table lock),
    a plain (blocking) build on MySQL/SQLite (see the module docstring caveat)."""
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.create_index(
                _ENTRY_ID_INDEX,
                _TABLE,
                ["entry_id"],
                unique=True,
                postgresql_concurrently=True,
            )
    else:
        op.create_index(_ENTRY_ID_INDEX, _TABLE, ["entry_id"], unique=True)


def upgrade() -> None:
    insp = _inspector()

    # Rename the released 0.1.0 ``firm_audits`` table (and its secondary indexes) to the workspace
    # convention first, so every step below targets ``firm_audit_events``. A fresh chain already
    # built the new name from 0001's metadata and skips this; only a real 0.1.0 database still has
    # the old name and is renamed in place with its rows intact.
    if insp is None or _has_table(insp, _OLD_TABLE):
        op.rename_table(_OLD_TABLE, _TABLE)
        insp = _inspector()  # reflect the renamed table before touching its indexes
        _swap_indexes(insp, _TABLE, _INDEX_RENAMES)

    missing = [
        sa.Column(name, type_)
        for name, type_ in (
            ("entry_id", sa.String(26)),
            ("row_mac", sa.String(64)),
            ("key_id", sa.String(16)),
        )
        if not _has_column(insp, _TABLE, name)
    ]
    # One batch = one table rebuild on SQLite (render_as_batch); a no-op wrapper doing direct
    # ALTERs on Postgres/MySQL.
    if missing:
        sqlite = op.get_bind().dialect.name == "sqlite"
        table_kwargs = {"sqlite_autoincrement": True} if sqlite else {}
        with op.batch_alter_table(
            _TABLE, table_kwargs=table_kwargs, recreate="always" if sqlite else "auto"
        ) as batch_op:
            for column in missing:
                batch_op.add_column(column)

    if not _has_table(insp, "firm_audit_seals"):
        _create_seals()
    if not _has_table(insp, "firm_audit_verify_status"):
        _create_verify_status()

    if not _has_index(insp, _TABLE, _ENTRY_ID_INDEX):
        _create_entry_id_index()


def downgrade() -> None:
    insp = _inspector()

    if _has_index(insp, _TABLE, _ENTRY_ID_INDEX) or insp is None:
        op.drop_index(_ENTRY_ID_INDEX, table_name=_TABLE)
    if _has_table(insp, "firm_audit_verify_status") or insp is None:
        op.drop_table("firm_audit_verify_status")
    if _has_table(insp, "firm_audit_seals") or insp is None:
        op.drop_table("firm_audit_seals")

    existing = [
        name
        for name in ("entry_id", "row_mac", "key_id")
        if insp is None or _has_column(insp, _TABLE, name)
    ]
    if existing:
        with op.batch_alter_table(_TABLE) as batch_op:
            for name in existing:
                batch_op.drop_column(name)

    # Mirror the rename back to the 0.1.0 shape (``firm_audit_events`` → ``firm_audits`` and its
    # secondary indexes) so a downgrade lands exactly on what 0001 shipped.
    if insp is None or _has_table(insp, _TABLE):
        op.rename_table(_TABLE, _OLD_TABLE)
        insp = _inspector()
        _swap_indexes(insp, _OLD_TABLE, [(new, old, cols) for old, new, cols in _INDEX_RENAMES])
