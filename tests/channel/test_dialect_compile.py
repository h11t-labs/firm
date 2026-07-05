"""Offline dialect checks for the channel schema (Postgres + MySQL)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from firm._core.dialects import MysqlDialect, PostgresDialect, SqliteDialect
from firm.channel import schema


def test_messages_compiles_for_pg_and_mysql() -> None:
    for dialect in (postgresql.dialect(), mysql.dialect()):
        ddl = str(CreateTable(schema.messages).compile(dialect=dialect))
        assert "firm_messages" in ddl


def test_mysql_uses_longblob_varbinary_and_datetime6() -> None:
    ddl = str(CreateTable(schema.messages).compile(dialect=mysql.dialect()))
    assert "LONGBLOB" in ddl
    assert "VARBINARY(1024)" in ddl
    assert "DATETIME(6)" in ddl


def test_trim_select_uses_skip_locked_on_pg_and_mysql() -> None:
    stmt = select(schema.messages.c.id)
    for dialect, compiler in (
        (PostgresDialect(), postgresql.dialect()),
        (MysqlDialect(), mysql.dialect()),
    ):
        sql = str(dialect.with_skip_locked(stmt).compile(dialect=compiler))
        assert "FOR UPDATE SKIP LOCKED" in sql


def test_trim_select_has_no_row_lock_on_sqlite() -> None:
    stmt = SqliteDialect().with_skip_locked(select(schema.messages.c.id))
    assert "FOR UPDATE" not in str(stmt.compile(dialect=sqlite.dialect()))
