"""The Alembic baseline migration creates the cache table."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import firm.cache
from firm._core.database import create_engine_for

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
        assert "firm_entries" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
