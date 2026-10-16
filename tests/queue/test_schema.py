"""Schema + engine wiring specs."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, insert, inspect, select, text

from firm._core.clock import now_utc
from firm._core.database import immediate_transaction
from firm.queue import schema

EXPECTED_TABLES = {
    "firm_queue_jobs",
    "firm_queue_ready_executions",
    "firm_queue_claimed_executions",
    "firm_queue_scheduled_executions",
    "firm_queue_blocked_executions",
    "firm_queue_failed_executions",
    "firm_queue_semaphores",
    "firm_queue_pauses",
    "firm_queue_processes",
    "firm_queue_recurring_tasks",
    "firm_queue_recurring_executions",
}


def test_all_eleven_tables_created(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES


def test_sqlite_pragmas_applied(engine: Engine, is_sqlite: bool) -> None:
    if not is_sqlite:
        pytest.skip("PRAGMAs are SQLite-only")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
        assert int(conn.exec_driver_sql("PRAGMA foreign_keys").scalar()) == 1
        assert int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar()) == 5000


def test_ready_execution_job_id_is_unique(engine: Engine) -> None:
    indexes = inspect(engine).get_indexes("firm_queue_ready_executions")
    unique = {ix["name"] for ix in indexes if ix["unique"]}
    assert "index_firm_queue_ready_executions_on_job_id" in unique


def test_recurring_executions_dedupe_index(engine: Engine) -> None:
    rows = inspect(engine).get_indexes("firm_queue_recurring_executions")
    indexes = {ix["name"]: ix for ix in rows}
    idx = indexes["index_firm_queue_recurring_executions_on_task_key_and_run_at"]
    assert idx["unique"]
    assert idx["column_names"] == ["task_key", "run_at"]


def test_execution_tables_reference_jobs(engine: Engine) -> None:
    fks = inspect(engine).get_foreign_keys("firm_queue_ready_executions")
    assert any(fk["referred_table"] == "firm_queue_jobs" for fk in fks)


def test_foreign_key_cascade_deletes_executions(engine: Engine) -> None:
    with engine.begin() as conn:
        job_id = conn.execute(
            insert(schema.jobs).values(queue_name="default", class_name="X")
        ).inserted_primary_key[0]
        conn.execute(
            insert(schema.ready_executions).values(job_id=job_id, queue_name="default", priority=0)
        )

    with engine.begin() as conn:
        conn.execute(schema.jobs.delete().where(schema.jobs.c.id == job_id))

    with engine.connect() as conn:
        remaining = conn.execute(
            select(schema.ready_executions).where(schema.ready_executions.c.job_id == job_id)
        ).all()
    assert remaining == []


def test_immediate_transaction_commits(engine: Engine) -> None:
    with immediate_transaction(engine) as conn:
        conn.execute(insert(schema.semaphores).values(key="k", value=1, expires_at=now_utc()))
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM firm_queue_semaphores")).scalar()
    assert count == 1
