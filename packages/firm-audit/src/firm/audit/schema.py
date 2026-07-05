"""Audit schema — the single ``firm_audits`` table.

Append-only: nothing in this package updates or deletes a row except :mod:`.retention`'s opt-in
pruning. Every column :func:`~firm.audit.log.AuditLog.history` filters on (action/subject/actor/
correlation_id/created_at) is indexed; ``data``/``changes``/``context`` are opaque JSON strings
(see :mod:`.serialization`) and are never filtered on in SQL. ``subject_label``/``actor_label`` are
optional human-readable names captured at event time (so a row stays legible after the referenced
record is deleted or renamed); like the JSON payloads they are display-only and never filtered on.

The Table object is a supported *read* surface (the dashboard's queries build on it); renaming
a column is a breaking change. Writes must go through :func:`~firm.audit.record` /
:class:`~firm.audit.AuditLog` — nothing else may insert, and only retention may delete.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    Index,
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

audits = Table(
    "firm_audits",
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
    Index("index_firm_audits_on_subject", "subject_type", "subject_id"),
    Index("index_firm_audits_on_actor", "actor_type", "actor_id"),
    Index("index_firm_audits_on_action", "action"),
    Index("index_firm_audits_on_correlation_id", "correlation_id"),
    Index("index_firm_audits_on_created_at", "created_at"),
)


def create_all(bind: Engine | Connection) -> None:
    """Create the firm-audit table and stamp the Alembic baseline, so an auto-created schema
    stays ``alembic upgrade``-able later."""
    create_all_and_stamp(
        bind, metadata, migrations_package="firm.audit.migrations", version_table=VERSION_TABLE
    )


def drop_all(bind: Engine | Connection) -> None:
    drop_all_and_unstamp(bind, metadata, version_table=VERSION_TABLE)
