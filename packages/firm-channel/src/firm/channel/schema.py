"""Pub/sub schema — the single ``firm_channel_messages`` table.

Broadcasting inserts a row; subscribers poll for rows newer than the last ``id`` they have seen on
their channels. ``channel_hash`` (a signed 64-bit hash of the channel) is the indexed column the
listener filters on, ``created_at`` drives age-based trimming, and ``id`` gives delivery ordering.

The Table object is a supported *read* surface (the dashboard's queries build on it); renaming
a column is a breaking change. Mutations must go through :class:`~firm.channel.Channel`.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    LargeBinary,
    MetaData,
    Table,
)
from sqlalchemy.dialects.mysql import VARBINARY as MYSQL_VARBINARY
from sqlalchemy.engine import Connection, Engine

from .._core.clock import now_utc
from .._core.schema import dt_type, long_blob, pk_bigint
from .._core.schema_setup import create_all_and_stamp, drop_all_and_unstamp

metadata = MetaData()

VERSION_TABLE = "firm_channel_alembic_version"

# Payloads use the shared long-binary helper. Postgres uses BYTEA and
# SQLite a BLOB regardless of length. created_at needs sub-second precision on MySQL.
_PAYLOAD_TYPE = long_blob()
_DT = dt_type()
# ``channel`` is indexed, so on MySQL it is a VARBINARY rather than a BLOB (a BLOB can't be indexed
# without a key-prefix length). Postgres/SQLite store it as BYTEA/BLOB.
_CHANNEL_TYPE = LargeBinary(1024).with_variant(MYSQL_VARBINARY(1024), "mysql")

messages = Table(
    "firm_channel_messages",
    metadata,
    Column("id", pk_bigint(), primary_key=True),
    Column("channel", _CHANNEL_TYPE, nullable=False),
    Column("payload", _PAYLOAD_TYPE, nullable=False),
    Column("channel_hash", BigInteger, nullable=False),
    Column("created_at", _DT, nullable=False, default=now_utc),
    # The raw-channel index serves the dashboard's per-channel GROUP BY (channel_top);
    # message polling itself only ever filters on channel_hash.
    Index("index_firm_channel_messages_on_channel", "channel"),
    Index("index_firm_channel_messages_on_channel_hash", "channel_hash"),
    Index("index_firm_channel_messages_on_created_at", "created_at"),
)


def create_all(bind: Engine | Connection) -> None:
    """Create the firm-channel table and stamp the Alembic baseline, so an auto-created schema
    stays ``alembic upgrade``-able later."""
    create_all_and_stamp(
        bind, metadata, migrations_package="firm.channel.migrations", version_table=VERSION_TABLE
    )


def drop_all(bind: Engine | Connection) -> None:
    drop_all_and_unstamp(bind, metadata, version_table=VERSION_TABLE)
