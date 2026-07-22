"""Audit schema — append-only events plus independent tamper-evidence records.

Append-only: nothing in this package updates or deletes a ``firm_audit_events`` row except
:mod:`.retention`'s opt-in pruning. Every column :func:`~firm.audit.log.AuditLog.history`
filters on (action/subject/actor/correlation_id/created_at) is indexed; ``data``/``changes``/
``context`` are opaque JSON strings (see :mod:`.serialization`) and are never filtered on in SQL.
``subject_label``/``actor_label`` are optional human-readable names captured at event time (so a
row stays legible after the referenced record is deleted or renamed); like the JSON payloads they
are display-only and never filtered on.

The Table object is a supported *read* surface (the dashboard's queries build on it); renaming
a column is a breaking change. Writes must go through :func:`~firm.audit.record` /
:class:`~firm.audit.AuditLog` — nothing else may insert, and only retention may delete.

**Tamper-evidence columns and tables are opt-in and inert without a key** (design review 4A).
When no ``FIRM_AUDIT_KEY`` is configured, :mod:`.events` never populates ``entry_id``/``row_mac``/
``key_id`` and they stay NULL — behavior and schema semantics are exactly as they were before
these columns existed. The two side tables (``firm_audit_seals``, ``firm_audit_verify_status``)
are created regardless but stay empty until the sealer and verifier (opt-in) write to them:

* ``firm_audit_events.entry_id`` — client-generated ULID, unique index; identity + anti-replay.
* ``firm_audit_events.row_mac`` — hex ``HMAC-SHA256`` over the canonical row (:mod:`.integrity`).
* ``firm_audit_events.key_id`` — which key signed the row (rotation).
* ``firm_audit_seals`` — independent range seals plus signed activation/floor markers.
* ``firm_audit_verify_status`` — single-row snapshot of the latest verification, read by the
  dashboard's integrity panel.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.engine import Connection, Engine

from .._core.clock import now_utc
from .._core.schema import dt_type, pk_bigint
from .._core.schema_setup import create_all_and_stamp, drop_all_and_unstamp

metadata = MetaData()

VERSION_TABLE = "firm_audit_alembic_version"

_DT = dt_type()

audit_events = Table(
    "firm_audit_events",
    metadata,
    Column("id", pk_bigint(), primary_key=True),
    Column("action", String(255), nullable=False),
    Column("subject_type", String(255)),
    Column("subject_id", String(255)),
    Column("subject_label", String(255)),
    Column("actor_type", String(255)),
    Column("actor_id", String(255)),
    Column("actor_label", String(255)),
    Column("correlation_id", String(255)),
    Column("data", Text),
    Column("changes", Text),
    Column("context", Text),
    Column("created_at", _DT, nullable=False, default=now_utc),
    # Tamper-evidence (Layer 1). Nullable: a key-less deployment leaves all three NULL and is
    # byte-identical to the pre-tamper-evidence schema. See :mod:`.integrity` / :mod:`.events`.
    Column("entry_id", String(26)),
    Column("row_mac", String(64)),
    Column("key_id", String(16)),
    Index("index_firm_audit_events_on_subject", "subject_type", "subject_id"),
    Index("index_firm_audit_events_on_actor", "actor_type", "actor_id"),
    Index("index_firm_audit_events_on_action", "action"),
    Index("index_firm_audit_events_on_correlation_id", "correlation_id"),
    Index("index_firm_audit_events_on_created_at", "created_at"),
    # Unique on the ULID: rejects a replayed row at insert and gives verify an anti-replay
    # check. NULLs are exempt (multiple NULLs are allowed under a unique index on every
    # supported dialect), so pre-key rows never collide. Migration 0002 builds this index
    # CONCURRENTLY on Postgres; here (fresh create_all) it is inline on an empty table.
    Index("index_firm_audit_events_on_entry_id", "entry_id", unique=True),
    sqlite_autoincrement=True,
)

# Layer 2 — three independent signed record kinds. A ``seal`` covers ``(from_id, to_id]`` and
# supplies ``row_count``/``rows_mac``. The one ``activation`` marker stores its ``boundary_id`` in
# ``to_id`` and reserves ``from_id=-1`` so racing first sealers hit the same unique constraint. A
# ``floor`` advance stores ``through_id`` in ``to_id`` and leaves ``from_id`` NULL; advances are
# append-only. NULL ``from_id`` values do not collide on any supported database, while the unique
# index arbitrates racing covering seals by their shared high-water mark.
seals = Table(
    "firm_audit_seals",
    metadata,
    Column("id", pk_bigint(), primary_key=True),
    Column("kind", String(32), nullable=False),  # seal | floor | activation
    Column("from_id", BigInteger),
    Column("to_id", BigInteger, nullable=False),
    Column("row_count", Integer),
    Column("rows_mac", String(64)),
    Column("seal_mac", String(64), nullable=False),
    Column("sealed_at", _DT, nullable=False),
    Column("key_id", String(16), nullable=False),
    Index("index_firm_audit_seals_on_from_id", "from_id", unique=True),
)

# Single-row snapshot of the latest verification run, upserted by ``verify`` and read by the
# dashboard's integrity panel (design review D11, D22-D25). "Single-row" is a contract the
# writer keeps (it upserts one fixed row), not a schema constraint; the surrogate ``id`` PK
# keeps the convention. Every field mirrors something the panel's state table renders.
verify_status = Table(
    "firm_audit_verify_status",
    metadata,
    Column("id", pk_bigint(), primary_key=True),
    Column("ran_at", _DT, nullable=False),
    Column("outcome", String(16), nullable=False),  # ok | warning | error | tampered
    Column("ok_count", Integer, nullable=False, default=0),
    Column("warning_count", Integer, nullable=False, default=0),
    Column("unprotected_count", Integer, nullable=False, default=0),
    Column("tampered_count", Integer, nullable=False, default=0),
    Column("error_message", Text),  # populated on the ERROR outcome (design D24)
    Column("last_full_coverage_at", _DT),  # last explicit ``--full`` coverage
    Column("newest_anchor_at", _DT),  # age of the freshest anchor the run saw
    Column("anchor_configured", Boolean, nullable=False, default=False),  # vs. "no anchor" (D22)
    Column("unsealed_tail_count", Integer, nullable=False, default=0),
    Column("unsealed_tail_oldest_at", _DT),  # oldest unsealed row → tail age via ``When``
    # JSON list of the top-N tampered findings on tampering (kind/label/id?/message/verdict), read
    # by the dashboard's integrity banner as linked chips + per-finding "what/why" (D22).
    Column("affected_identifiers", Text),
    Column("duration_seconds", Float),
)


def create_all(bind: Engine | Connection) -> None:
    """Create the firm-audit table and stamp the Alembic baseline, so an auto-created schema
    stays ``alembic upgrade``-able later."""
    create_all_and_stamp(
        bind, metadata, migrations_package="firm.audit.migrations", version_table=VERSION_TABLE
    )


def drop_all(bind: Engine | Connection) -> None:
    drop_all_and_unstamp(bind, metadata, version_table=VERSION_TABLE)
