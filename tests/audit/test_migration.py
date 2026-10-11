"""The Alembic baseline migration creates the audit table."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import firm.audit
from firm._core.database import create_engine_for

MIGRATIONS_DIR = Path(firm.audit.__file__).parent / "migrations"


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_creates_audits_table(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_config(url), "head")
    engine = create_engine_for(url)
    try:
        assert "firm_audits" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_upgrade_creates_reference_label_columns(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_config(url), "head")
    engine = create_engine_for(url)
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("firm_audits")}
        assert {"subject_label", "actor_label"} <= cols
    finally:
        engine.dispose()


def test_upgrade_from_original_baseline_creates_archive_tables(tmp_path) -> None:
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path / 'baseline.db'}"
    cfg = _config(url)
    command.upgrade(cfg, "0001")
    engine = create_engine_for(url)
    try:
        assert "firm_audit_archive_batches" not in inspect(engine).get_table_names()
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO firm_audits(action, created_at) VALUES ('old', '2020-01-01')")
            )
        command.upgrade(cfg, "head")
        assert {
            "firm_audit_archive_policy",
            "firm_audit_archive_batches",
            "firm_audit_archive_members",
        } <= set(inspect(engine).get_table_names())
        with engine.connect() as conn:
            assert conn.execute(text("SELECT action FROM firm_audits")).scalar_one() == "old"
    finally:
        engine.dispose()


def test_copy_export_supports_original_schema(tmp_path, archive):
    from uuid import UUID, uuid4

    from firm.audit import AuditLog

    url = f"sqlite:///{tmp_path / 'original-export.db'}"
    command.upgrade(_config(url), "0001")
    with AuditLog(url, create_schema=False) as source:
        source.record("old")
        receipt = archive.export_sql(source, stream=str(UUID(int=1)), batch_id=str(uuid4()))
        assert archive.read(receipt)[0]["event"]["action"] == "old"
        manifest = archive.inspect(receipt)
        assert manifest["min_created_at"] == manifest["max_created_at"]
