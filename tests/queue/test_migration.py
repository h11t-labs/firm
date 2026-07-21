"""The Alembic baseline migration creates the same schema as ``create_all``."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Column, Index, MetaData, Table, inspect, select

import firm.queue
from firm._core.clock import now_utc
from firm._core.database import create_engine_for
from firm.queue import schema

EXPECTED_TABLES = set(schema.metadata.tables.keys())
MIGRATIONS_DIR = Path(firm.queue.__file__).parent / "migrations"


def _alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_head_creates_all_tables(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_alembic_config(url), "head")
    engine = create_engine_for(url)
    try:
        assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    finally:
        engine.dispose()


def test_downgrade_base_drops_tables(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine_for(url)
    try:
        remaining = set(inspect(engine).get_table_names()) & EXPECTED_TABLES
        assert remaining == set()
    finally:
        engine.dispose()


def _legacy_metadata() -> MetaData:
    """The pre-0002 queue schema — every table under its old ``firm_<entity>`` name with the
    old-style ``index_firm_<entity>_on_*`` secondary indexes (and no FK constraints, which the
    rename does not touch). Derived from the live metadata by mapping the new names back to the old
    ones, so a legacy 0.x database can be stood up at revision 0001 and given to the 0002 upgrade to
    actually *rename* (on a freshly built chain 0001 would already create the new names)."""
    md = MetaData()
    for table in schema.metadata.sorted_tables:
        old_table = table.name.replace("firm_queue_", "firm_", 1)
        clone = Table(
            old_table,
            md,
            *[
                Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
                for c in table.columns
            ],
        )
        for idx in table.indexes:
            old_index = idx.name.replace("index_firm_queue_", "index_firm_", 1)
            Index(old_index, *[clone.c[c.name] for c in idx.columns], unique=idx.unique)
    return md


def test_upgrade_0001_to_0002_on_populated_db(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'legacy.db'}"

    # Stand up a legacy database: the pre-0002 old-named tables, ``firm_jobs`` populated, at 0001.
    engine = create_engine_for(url)
    legacy = _legacy_metadata()
    legacy.create_all(engine)
    jobs = legacy.tables["firm_jobs"]
    with engine.begin() as conn:
        conn.execute(
            jobs.insert(),
            [
                {
                    "queue_name": "default",
                    "class_name": "LegacyJob",
                    "priority": 0,
                    "attempts": 0,
                    "created_at": now_utc(),
                    "updated_at": now_utc(),
                }
                for _ in range(2)
            ],
        )
    engine.dispose()
    command.stamp(_alembic_config(url), "0001")

    # The upgrade under test: 0002 renames every table to ``firm_queue_*`` (rows intact) and renames
    # their secondary indexes.
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine_for(url)
    try:
        insp = inspect(engine)
        names = set(insp.get_table_names())
        assert names >= EXPECTED_TABLES
        old_names = {n.replace("firm_queue_", "firm_", 1) for n in EXPECTED_TABLES}
        assert old_names.isdisjoint(names)

        job_indexes = {i["name"] for i in insp.get_indexes("firm_queue_jobs")}
        assert "index_firm_queue_jobs_on_class_name" in job_indexes
        assert not any(name.startswith("index_firm_jobs_") for name in job_indexes)

        # A renamed unique index still reads as unique after the drop-and-recreate.
        ready_indexes = {i["name"]: i for i in insp.get_indexes("firm_queue_ready_executions")}
        assert ready_indexes["index_firm_queue_ready_executions_on_job_id"]["unique"]

        # The pre-existing rows survive the rename.
        with engine.connect() as conn:
            classes = conn.execute(
                select(schema.jobs.c.class_name).order_by(schema.jobs.c.id)
            ).scalars().all()
        assert classes == ["LegacyJob", "LegacyJob"]
    finally:
        engine.dispose()
