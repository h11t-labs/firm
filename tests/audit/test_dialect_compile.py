"""Offline dialect checks for the audit schema (Postgres + MySQL)."""

from __future__ import annotations

from sqlalchemy.dialects import mysql, postgresql
from sqlalchemy.schema import CreateTable

from firm.audit import schema


def test_audits_compiles_for_pg_and_mysql() -> None:
    for dialect in (postgresql.dialect(), mysql.dialect()):
        ddl = str(CreateTable(schema.audits).compile(dialect=dialect))
        assert "firm_audits" in ddl


def test_mysql_created_at_is_datetime6() -> None:
    ddl = str(CreateTable(schema.audits).compile(dialect=mysql.dialect()))
    assert "DATETIME(6)" in ddl
