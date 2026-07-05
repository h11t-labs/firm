"""The Alembic baseline migration creates the same schema as ``create_all``."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import firm.queue
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
