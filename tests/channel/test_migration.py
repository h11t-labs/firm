"""The Alembic migrations: 0001 baseline creates the messages table; 0002 renames it to
``firm_channel_messages`` per the ``firm_<module>_<entity>`` convention."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    LargeBinary,
    MetaData,
    Table,
    inspect,
    select,
)

import firm.channel
from firm._core.clock import now_utc
from firm._core.database import create_engine_for
from firm._core.schema import dt_type, long_blob, pk_bigint
from firm.channel import schema

MIGRATIONS_DIR = Path(firm.channel.__file__).parent / "migrations"


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_creates_messages_table_and_indexes(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    cfg = _config(url)
    command.upgrade(cfg, "head")
    engine = create_engine_for(url)
    try:
        insp = inspect(engine)
        assert "firm_channel_messages" in insp.get_table_names()
        assert "firm_messages" not in insp.get_table_names()
        names = {ix["name"] for ix in insp.get_indexes("firm_channel_messages")}
        assert "index_firm_channel_messages_on_channel_hash" in names
        assert "index_firm_channel_messages_on_created_at" in names
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine = create_engine_for(url)
    try:
        assert "firm_channel_messages" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def _pre_0002_messages() -> Table:
    """The ``firm_messages`` table exactly as the 0.x baseline shipped it — under its old name, with
    the old-style ``index_firm_messages_on_*`` secondary indexes. Used to stand up a legacy database
    at revision 0001 so the 0002 upgrade has a real table to *rename* (on a freshly built chain
    0001's ``metadata.create_all`` would already create it under the new name)."""
    md = MetaData()
    return Table(
        "firm_messages",
        md,
        Column("id", pk_bigint(), primary_key=True),
        Column("channel", LargeBinary(1024), nullable=False),
        Column("payload", long_blob(), nullable=False),
        Column("channel_hash", BigInteger, nullable=False),
        Column("created_at", dt_type(), nullable=False),
        Index("index_firm_messages_on_channel", "channel"),
        Index("index_firm_messages_on_channel_hash", "channel_hash"),
        Index("index_firm_messages_on_created_at", "created_at"),
    )


def test_upgrade_0001_to_0002_on_populated_db(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'legacy.db'}"

    # Stand up a legacy database: the pre-0002 ``firm_messages`` table, populated, stamped at 0001.
    engine = create_engine_for(url)
    legacy = _pre_0002_messages()
    legacy.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            legacy.insert(),
            [
                {"channel": b"c1", "payload": b"one", "channel_hash": 11, "created_at": now_utc()},
                {"channel": b"c2", "payload": b"two", "channel_hash": 22, "created_at": now_utc()},
            ],
        )
    engine.dispose()
    command.stamp(_config(url), "0001")

    # The upgrade under test: 0002 renames the table to ``firm_channel_messages`` (rows intact) and
    # renames its secondary indexes.
    command.upgrade(_config(url), "head")

    engine = create_engine_for(url)
    try:
        insp = inspect(engine)
        names = set(insp.get_table_names())
        assert "firm_channel_messages" in names
        assert "firm_messages" not in names

        index_names = {i["name"] for i in insp.get_indexes("firm_channel_messages")}
        assert {
            "index_firm_channel_messages_on_channel",
            "index_firm_channel_messages_on_channel_hash",
            "index_firm_channel_messages_on_created_at",
        } <= index_names
        assert not any(name.startswith("index_firm_messages_on_") for name in index_names)

        # The pre-existing rows survive the rename.
        with engine.connect() as conn:
            hashes = conn.execute(
                select(schema.messages.c.channel_hash).order_by(schema.messages.c.id)
            ).scalars().all()
        assert hashes == [11, 22]
    finally:
        engine.dispose()
