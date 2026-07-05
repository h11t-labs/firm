"""Same-transaction atomicity (shared DB) vs. independent durability (separate DB).

Uses a throwaway ``widgets_for_audit_test`` table to stand in for "the business change", so the
guarantee under test is genuine: the audit row and an unrelated row share one transaction's fate.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, select

from firm._core.database import create_engine_for, transaction
from firm.audit import AuditLog, record, schema

_meta = MetaData()
_widgets = Table("widgets_for_audit_test", _meta, Column("id", Integer, primary_key=True))


def _reset_widgets_table(engine) -> None:
    _meta.drop_all(engine, tables=[_widgets], checkfirst=True)
    _meta.create_all(engine, tables=[_widgets])


def test_audit_row_commits_with_business_write(db_url: str) -> None:
    engine = create_engine_for(db_url)
    schema.create_all(engine)
    _reset_widgets_table(engine)
    try:
        with transaction(engine) as conn:
            conn.execute(_widgets.insert().values(id=1))
            record(conn, "widget.created", subject=("Widget", 1))

        with engine.connect() as conn:
            assert conn.execute(select(_widgets)).all() == [(1,)]
            assert len(conn.execute(select(schema.audits)).all()) == 1
    finally:
        engine.dispose()


def test_audit_row_rolls_back_with_business_write(db_url: str) -> None:
    engine = create_engine_for(db_url)
    schema.create_all(engine)
    _reset_widgets_table(engine)
    try:
        with pytest.raises(RuntimeError), transaction(engine) as conn:
            conn.execute(_widgets.insert().values(id=2))
            record(conn, "widget.created", subject=("Widget", 2))
            raise RuntimeError("simulated failure after the audit write")

        with engine.connect() as conn:
            assert conn.execute(select(_widgets)).all() == []
            assert conn.execute(select(schema.audits)).all() == []
    finally:
        engine.dispose()


def test_audit_log_record_uses_own_transaction_when_no_conn_given(audit: AuditLog) -> None:
    audit.record("standalone.event")
    assert len(audit.history()) == 1


def test_separate_database_is_independently_durable(tmp_path) -> None:
    business_url = f"sqlite:///{tmp_path / 'business.db'}"
    audit_url = f"sqlite:///{tmp_path / 'audit.db'}"
    business_engine = create_engine_for(business_url)
    _reset_widgets_table(business_engine)
    audit = AuditLog(database_url=audit_url)
    try:
        with transaction(business_engine) as conn:
            conn.execute(_widgets.insert().values(id=3))
        audit.record("widget.created", subject=("Widget", 3))

        assert len(audit.history()) == 1
        with business_engine.connect() as conn:
            assert conn.execute(select(_widgets)).all() == [(3,)]
    finally:
        audit.close()
        business_engine.dispose()
