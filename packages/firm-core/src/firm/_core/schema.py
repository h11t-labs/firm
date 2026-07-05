"""Schema for firm-core's own table: ``firm_processes``.

Process registration (:mod:`firm._core.process`) is core infrastructure, so its table is
defined here rather than in a feature package. firm-core has no migration story of its own:
the table is copied into ``firm.queue.schema.metadata`` (via ``Table.to_metadata``) so the
queue — the only module that spawns registered processes today — creates and migrates it
alongside its own tables.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.dialects.mysql import LONGBLOB, LONGTEXT

from .clock import now_utc

metadata = MetaData()

# Dialect-variant column types, defined once. Every firm table must build its columns from
# these helpers: a module hand-rolling its own variant (e.g. forgetting fsp=6) would silently
# truncate sub-second timestamps on MySQL only — exactly the drift class this file prevents.


def dt_type() -> DateTime:
    """Timestamp type; MySQL needs ``DATETIME(6)`` to keep fractional seconds."""
    return DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")


def pk_bigint() -> BigInteger:
    """Autoincrement PK type: ``BIGINT`` everywhere, plain ``INTEGER`` on SQLite (required
    for rowid autoincrement)."""
    return BigInteger().with_variant(Integer, "sqlite")


def long_blob() -> LargeBinary:
    """Binary values of arbitrary size; MySQL's plain ``BLOB`` caps at 64 KiB, so map to
    ``LONGBLOB`` (Postgres BYTEA / SQLite BLOB are unbounded regardless)."""
    return LargeBinary().with_variant(LONGBLOB, "mysql")


def long_text() -> Text:
    """Text of arbitrary size; MySQL's plain ``TEXT`` caps at 64 KiB, so map to ``LONGTEXT``."""
    return Text().with_variant(LONGTEXT, "mysql")


processes = Table(
    "firm_processes",
    metadata,
    Column("id", pk_bigint(), primary_key=True),
    Column("kind", String(255), nullable=False),
    Column("last_heartbeat_at", dt_type(), nullable=False),
    Column("supervisor_id", BigInteger),
    Column("pid", Integer, nullable=False),
    Column("hostname", String(255)),
    Column("metadata", Text),
    Column("name", String(255), nullable=False),
    Column("created_at", dt_type(), nullable=False, default=now_utc),
    Index("index_firm_processes_on_last_heartbeat_at", "last_heartbeat_at"),
    Index(
        "index_firm_processes_on_name_and_supervisor_id",
        "name",
        "supervisor_id",
        unique=True,
    ),
    Index("index_firm_processes_on_supervisor_id", "supervisor_id"),
)
