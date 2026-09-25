"""Offline dialect checks — DDL + locking SQL compile correctly for Postgres and MySQL."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.schema import CreateTable

from firm.queue import schema

DIALECTS = {"postgresql": postgresql.dialect(), "mysql": mysql.dialect()}


@pytest.mark.parametrize("name", list(DIALECTS))
def test_every_table_compiles(name: str) -> None:
    dialect = DIALECTS[name]
    for table in schema.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert table.name in ddl


def test_mysql_uses_datetime6_and_longtext() -> None:
    ddl = str(CreateTable(schema.jobs).compile(dialect=mysql.dialect()))
    assert "DATETIME(6)" in ddl
    assert "LONGTEXT" in ddl


def test_skip_locked_renders_for_pg_and_mysql() -> None:
    stmt = select(schema.ready_executions.c.id).with_for_update(skip_locked=True)
    assert "SKIP LOCKED" in str(stmt.compile(dialect=postgresql.dialect()))
    assert "SKIP LOCKED" in str(stmt.compile(dialect=mysql.dialect()))


def test_upsert_renders_native_conflict_handling() -> None:
    from sqlalchemy.dialects import sqlite

    from firm._core.dialects import MysqlDialect, PostgresDialect, SqliteDialect

    values = {"queue_name": "q", "class_name": "C"}
    cases = [
        (PostgresDialect(), postgresql.dialect(), "ON CONFLICT (class_name) DO UPDATE"),
        (MysqlDialect(), mysql.dialect(), "ON DUPLICATE KEY UPDATE"),
        (SqliteDialect(), sqlite.dialect(), "ON CONFLICT (class_name) DO UPDATE"),
    ]
    for firm_dialect, sa_dialect, marker in cases:
        stmt = firm_dialect.upsert(
            schema.jobs, values, index_elements=("class_name",), update_columns=("queue_name",)
        )
        assert marker in str(stmt.compile(dialect=sa_dialect))


def test_insert_ignore_renders_native_conflict_handling() -> None:
    from sqlalchemy.dialects import sqlite

    from firm._core.dialects import MysqlDialect, PostgresDialect, SqliteDialect

    values = {"queue_name": "q", "class_name": "C"}
    cases = [
        # Postgres also RETURNINGs the PK: inserted_count() relies on it because psycopg
        # reports rowcount -1 for compiled single-row INSERTs.
        # Postgres also RETURNINGs the PK: inserted_count() relies on it because psycopg
        # reports rowcount -1 for compiled single-row INSERTs.
        (PostgresDialect(), postgresql.dialect(), "ON CONFLICT (class_name) DO NOTHING"),
        (PostgresDialect(), postgresql.dialect(), "RETURNING firm_queue_jobs.id"),
        # MySQL uses INSERT IGNORE — the only form that meets the 1/0 rowcount contract under
        # the CLIENT.FOUND_ROWS flag SQLAlchemy always sets (see Dialect.insert_ignore).
        (MysqlDialect(), mysql.dialect(), "INSERT IGNORE"),
        (SqliteDialect(), sqlite.dialect(), "ON CONFLICT (class_name) DO NOTHING"),
    ]
    for firm_dialect, sa_dialect, marker in cases:
        stmt = firm_dialect.insert_ignore(schema.jobs, values, index_elements=("class_name",))
        assert marker in str(stmt.compile(dialect=sa_dialect))


def test_bare_urls_normalize_to_shipped_drivers() -> None:
    from firm._core.database import normalize_url

    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("mysql://u:p@h/db") == "mysql+pymysql://u:p@h/db"
    assert normalize_url("postgresql+psycopg2://x") == "postgresql+psycopg2://x"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"


def test_missing_driver_gives_actionable_error(monkeypatch) -> None:
    import importlib.util

    from firm._core.database import _require_driver

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ImportError, match=r"firm-core\[postgres\]"):
        _require_driver("postgresql+psycopg://localhost/x")
    with pytest.raises(ImportError, match=r"firm-core\[mysql\]"):
        _require_driver("mysql+pymysql://localhost/x")
