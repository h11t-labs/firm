"""Read-layer specs."""

from __future__ import annotations

from firm._core.clock import now_utc
from firm.ui import queries


def test_state_counts(runtime, seed) -> None:
    seed.ready()
    seed.ready()
    seed.scheduled()
    seed.failed()
    seed.finished()
    with runtime.engine.connect() as conn:
        counts = queries.state_counts(conn)
    assert counts["ready"] == 2
    assert counts["scheduled"] == 1
    assert counts["failed"] == 1
    assert counts["finished"] == 1
    assert counts["total"] == 5


def test_jobs_by_state(runtime, seed) -> None:
    a = seed.ready()
    b = seed.ready()
    seed.failed()
    with runtime.engine.connect() as conn:
        ready = queries.jobs_by_state(conn, "ready")
    assert {row["id"] for row in ready} == {a, b}
    assert all(row["class_name"] == "app.task" for row in ready)


def test_state_counts_scoped_to_queue(runtime, seed) -> None:
    seed.ready(queue="mailers")
    seed.ready(queue="mailers")
    seed.ready(queue="default")
    seed.failed()  # failed_executions has no queue_name of its own; must join back to jobs
    with runtime.engine.connect() as conn:
        mailers = queries.state_counts(conn, queue="mailers")
        default = queries.state_counts(conn, queue="default")
    assert mailers["ready"] == 2
    assert mailers["failed"] == 0
    assert mailers["total"] == 2
    assert default["ready"] == 1


def test_jobs_by_state_scoped_to_queue(runtime, seed) -> None:
    a = seed.ready(queue="mailers")
    seed.ready(queue="default")
    with runtime.engine.connect() as conn:
        mailers_ready = queries.jobs_by_state(conn, "ready", queue="mailers")
    assert {row["id"] for row in mailers_ready} == {a}


def test_job_detail_failed_includes_error(runtime, seed) -> None:
    job_id = seed.failed(error="Traceback...\nValueError: nope")
    with runtime.engine.connect() as conn:
        detail = queries.job_detail(conn, job_id)
    assert detail is not None
    assert detail["state"] == "failed"
    assert "ValueError: nope" in detail["error"]


def test_job_detail_missing_returns_none(runtime) -> None:
    with runtime.engine.connect() as conn:
        assert queries.job_detail(conn, 999) is None


def test_queue_rows_reports_size_and_paused(runtime, seed) -> None:
    seed.ready(queue="mailers")
    seed.ready(queue="mailers")
    seed.ready(queue="default")
    from firm.queue import queues

    queues.pause(runtime, "mailers")
    with runtime.engine.connect() as conn:
        rows = {r["name"]: r for r in queries.queue_rows(conn, now_utc())}
    assert rows["mailers"]["size"] == 2
    assert rows["mailers"]["paused"] is True
    assert rows["default"]["paused"] is False


def test_queue_rows_includes_paused_queue_with_zero_ready(runtime, seed) -> None:
    """A paused queue that has no ready executions still gets a row (size 0) — the paused-name
    merge must survive the single-query rewrite, not just queues that appear in the grouped scan."""
    from firm.queue import queues

    seed.ready(queue="default")
    queues.pause(runtime, "drained")  # paused, but never had ready work
    with runtime.engine.connect() as conn:
        rows = {r["name"]: r for r in queries.queue_rows(conn, now_utc())}
    assert rows["drained"]["size"] == 0
    assert rows["drained"]["paused"] is True
    assert rows["drained"]["latency"] == 0.0
    assert rows["default"]["paused"] is False


def test_queue_rows_latency_from_oldest_ready(runtime, seed) -> None:
    """Latency comes from the oldest ready row's age; the grouped MIN(created_at) must feed it."""
    from datetime import timedelta

    from sqlalchemy import update

    from firm.queue import schema

    seed.ready(queue="slow")
    seed.ready(queue="slow")
    with runtime.engine.begin() as conn:
        # age the whole queue's ready rows; MIN(created_at) drives latency
        conn.execute(
            update(schema.ready_executions)
            .where(schema.ready_executions.c.queue_name == "slow")
            .values(created_at=now_utc() - timedelta(seconds=120))
        )
    with runtime.engine.connect() as conn:
        rows = {r["name"]: r for r in queries.queue_rows(conn, now_utc())}
    assert rows["slow"]["size"] == 2
    assert rows["slow"]["latency"] >= 119.0


def test_queue_rows_issues_two_queries_regardless_of_queue_count(runtime, seed) -> None:
    """The overview auto-refreshes, so this must stay O(1) queries — one grouped scan plus the
    paused-names lookup — not two SELECTs per queue as the old per-name loop did."""
    from sqlalchemy import event

    for i in range(6):
        seed.ready(queue=f"q{i}")
    statements: list[str] = []
    with runtime.engine.connect() as conn:
        event.listen(conn, "before_cursor_execute", lambda *a: statements.append(a[2]))
        queries.queue_rows(conn, now_utc())
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # one GROUP BY over ready_executions + one scan of pauses == 2, independent of the 6 queues
    assert len(selects) == 2


def test_processes_alive_vs_stale(runtime, seed) -> None:
    seed.process(name="fresh", age_seconds=0.0)
    seed.process(name="stale", age_seconds=10_000.0)
    with runtime.engine.connect() as conn:
        procs = {p["name"]: p for p in queries.processes(conn, now_utc())}
    assert procs["fresh"]["alive"] is True
    assert procs["stale"]["alive"] is False
