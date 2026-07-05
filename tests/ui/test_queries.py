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


def test_processes_alive_vs_stale(runtime, seed) -> None:
    seed.process(name="fresh", age_seconds=0.0)
    seed.process(name="stale", age_seconds=10_000.0)
    with runtime.engine.connect() as conn:
        procs = {p["name"]: p for p in queries.processes(conn, now_utc())}
    assert procs["fresh"]["alive"] is True
    assert procs["stale"]["alive"] is False
