"""Offline dialect checks for the cache schema (Postgres + MySQL)."""

from __future__ import annotations

from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.schema import CreateTable

from firm.cache import schema


def test_entries_compiles_for_pg_and_mysql() -> None:
    for dialect in (postgresql.dialect(), mysql.dialect()):
        ddl = str(CreateTable(schema.entries).compile(dialect=dialect))
        assert "firm_entries" in ddl


def test_mysql_value_is_longblob_with_datetime6() -> None:
    ddl = str(CreateTable(schema.entries).compile(dialect=mysql.dialect()))
    assert "LONGBLOB" in ddl
    assert "DATETIME(6)" in ddl
