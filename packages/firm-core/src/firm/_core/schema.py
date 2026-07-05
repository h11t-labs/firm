"""Schema for firm-core's own table: ``firm_processes``.

Process registration (:mod:`firm._core.process`) is core infrastructure, so its table is
defined here rather than in a feature package. firm-core has no migration story of its own:
the table is copied into ``firm.queue.schema.metadata`` (via ``Table.to_metadata``) so the
queue — the only module that spawns registered processes today — creates and migrates it
alongside its own tables.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, MetaData, String, Table, Text
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME

from .clock import now_utc

metadata = MetaData()


def _dt() -> DateTime:
    """Timestamp type; MySQL needs ``DATETIME(6)`` to keep fractional seconds."""
    return DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")


processes = Table(
    "firm_processes",
    metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True),
    Column("kind", String(255), nullable=False),
    Column("last_heartbeat_at", _dt(), nullable=False),
    Column("supervisor_id", BigInteger),
    Column("pid", Integer, nullable=False),
    Column("hostname", String(255)),
    Column("metadata", Text),
    Column("name", String(255), nullable=False),
    Column("created_at", _dt(), nullable=False, default=now_utc),
    Index("index_firm_processes_on_last_heartbeat_at", "last_heartbeat_at"),
    Index(
        "index_firm_processes_on_name_and_supervisor_id",
        "name",
        "supervisor_id",
        unique=True,
    ),
    Index("index_firm_processes_on_supervisor_id", "supervisor_id"),
)
