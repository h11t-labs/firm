"""The Alembic baseline migration creates the messages table."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import firm.channel
from firm._core.database import create_engine_for

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
        assert "firm_messages" in insp.get_table_names()
        names = {ix["name"] for ix in insp.get_indexes("firm_messages")}
        assert "index_firm_messages_on_channel_hash" in names
        assert "index_firm_messages_on_created_at" in names
    finally:
        engine.dispose()

    command.downgrade(cfg, "base")
    engine = create_engine_for(url)
    try:
        assert "firm_messages" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
