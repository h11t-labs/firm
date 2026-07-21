"""The Alembic migrations: 0001 baseline creates the cache table; 0002 renames it to
``firm_cache_entries`` per the ``firm_<module>_<entity>`` convention."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    inspect,
    select,
)

import firm.cache
from firm._core.clock import now_utc
from firm._core.database import create_engine_for
from firm._core.schema import dt_type, long_blob, pk_bigint
from firm.cache import schema

MIGRATIONS_DIR = Path(firm.cache.__file__).parent / "migrations"


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_creates_entries_table(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_config(url), "head")
    engine = create_engine_for(url)
    try:
        names = inspect(engine).get_table_names()
        assert "firm_cache_entries" in names
        assert "firm_entries" not in names
    finally:
        engine.dispose()


def _pre_0002_entries() -> Table:
    """The ``firm_entries`` table exactly as the 0.x baseline shipped it — under its old name, with
    the old-style ``index_firm_entries_on_*`` secondary indexes. Used to stand up a legacy database
    at revision 0001 so the 0002 upgrade has a real table to *rename* (on a freshly built chain
    0001's ``metadata.create_all`` would already create it under the new name)."""
    md = MetaData()
    return Table(
        "firm_entries",
        md,
        Column("id", pk_bigint(), primary_key=True),
        Column("key", LargeBinary(1024), nullable=False),
        Column("value", long_blob(), nullable=False),
        Column("key_hash", BigInteger, nullable=False),
        Column("byte_size", Integer, nullable=False),
        Column("created_at", dt_type(), nullable=False),
        Index("index_firm_entries_on_key_hash", "key_hash", unique=True),
        Index("index_firm_entries_on_key_hash_and_byte_size", "key_hash", "byte_size"),
        Index("index_firm_entries_on_byte_size", "byte_size"),
    )


def test_upgrade_0001_to_0002_on_populated_db(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'legacy.db'}"

    # Stand up a legacy database: the pre-0002 ``firm_entries`` table, populated, stamped at 0001.
    engine = create_engine_for(url)
    legacy = _pre_0002_entries()
    legacy.metadata.create_all(engine)
    now = now_utc()
    with engine.begin() as conn:
        conn.execute(
            legacy.insert(),
            [
                {"key": b"a", "value": b"one", "key_hash": 11, "byte_size": 3, "created_at": now},
                {"key": b"b", "value": b"two", "key_hash": 22, "byte_size": 3, "created_at": now},
            ],
        )
    engine.dispose()
    command.stamp(_config(url), "0001")

    # The upgrade under test: 0002 renames the table to ``firm_cache_entries`` (rows intact) and
    # renames its secondary indexes.
    command.upgrade(_config(url), "head")

    engine = create_engine_for(url)
    try:
        insp = inspect(engine)
        names = set(insp.get_table_names())
        assert "firm_cache_entries" in names
        assert "firm_entries" not in names

        index_names = {i["name"] for i in insp.get_indexes("firm_cache_entries")}
        assert {
            "index_firm_cache_entries_on_key_hash",
            "index_firm_cache_entries_on_key_hash_and_byte_size",
            "index_firm_cache_entries_on_byte_size",
        } <= index_names
        assert not any(name.startswith("index_firm_entries_on_") for name in index_names)
        assert any(
            i["name"] == "index_firm_cache_entries_on_key_hash" and i["unique"]
            for i in insp.get_indexes("firm_cache_entries")
        )

        # The pre-existing rows survive the rename.
        with engine.connect() as conn:
            hashes = (
                conn.execute(select(schema.entries.c.key_hash).order_by(schema.entries.c.id))
                .scalars()
                .all()
            )
        assert hashes == [11, 22]
    finally:
        engine.dispose()
