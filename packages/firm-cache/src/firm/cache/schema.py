"""Cache schema — the single ``firm_entries`` table.

``key_hash`` (a signed 64-bit hash of the key) is the unique lookup column, so reads/writes
never need an index on the raw (up to 1 KiB) key. ``id`` gives FIFO ordering for eviction.

The Table object is a supported *read* surface (the dashboard's queries build on it); renaming
a column is a breaking change. Mutations must go through :class:`~firm.cache.Cache`.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
)
from sqlalchemy.engine import Connection, Engine

from .._core.clock import now_utc
from .._core.schema import dt_type, long_blob, pk_bigint
from .._core.schema_setup import create_all_and_stamp, drop_all_and_unstamp

metadata = MetaData()

VERSION_TABLE = "firm_cache_alembic_version"

_VALUE_TYPE = long_blob()
_DT = dt_type()

entries = Table(
    "firm_entries",
    metadata,
    Column("id", pk_bigint(), primary_key=True),
    Column("key", LargeBinary(1024), nullable=False),
    Column("value", _VALUE_TYPE, nullable=False),
    Column("key_hash", BigInteger, nullable=False),
    Column("byte_size", Integer, nullable=False),
    Column("created_at", _DT, nullable=False, default=now_utc),
    Index("index_firm_entries_on_key_hash", "key_hash", unique=True),
    Index("index_firm_entries_on_key_hash_and_byte_size", "key_hash", "byte_size"),
    Index("index_firm_entries_on_byte_size", "byte_size"),
)


def create_all(bind: Engine | Connection) -> None:
    """Create the firm-cache table and stamp the Alembic baseline, so an auto-created schema
    stays ``alembic upgrade``-able later."""
    create_all_and_stamp(
        bind, metadata, migrations_package="firm.cache.migrations", version_table=VERSION_TABLE
    )


def drop_all(bind: Engine | Connection) -> None:
    drop_all_and_unstamp(bind, metadata, version_table=VERSION_TABLE)
