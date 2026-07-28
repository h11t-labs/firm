"""Specs for the read-query surface (:mod:`firm.queue.queries`)."""

from __future__ import annotations

import pytest

from firm._core.clock import now_utc
from firm.queue import queries, queues


def test_state_counts(engine, seed) -> None:
    seed.ready()
    seed.ready()
    seed.scheduled()
    seed.failed()
    seed.finished()
    with engine.connect() as conn:
        counts = queries.state_counts(conn)
    assert counts["ready"] == 2
    assert counts["scheduled"] == 1
    assert counts["failed"] == 1
    assert counts["finished"] == 1
    assert counts["total"] == 5


def test_jobs_by_state(engine, seed) -> None:
    a = seed.ready()
    b = seed.ready()
    seed.failed()
    with engine.connect() as conn:
        ready = queries.jobs_by_state(conn, "ready")
    assert {row["id"] for row in ready} == {a, b}
    assert all(row["class_name"] == "app.task" for row in ready)


def test_state_counts_scoped_to_queue(engine, seed) -> None:
    seed.ready(queue="mailers")
    seed.ready(queue="mailers")
    seed.ready(queue="default")
    seed.failed()  # failed_executions has no queue_name of its own; must join back to jobs
    with engine.connect() as conn:
        mailers = queries.state_counts(conn, queue="mailers")
        default = queries.state_counts(conn, queue="default")
    assert mailers["ready"] == 2
    assert mailers["failed"] == 0
    assert mailers["total"] == 2
    assert default["ready"] == 1


def test_jobs_by_state_scoped_to_queue(engine, seed) -> None:
    a = seed.ready(queue="mailers")
    seed.ready(queue="default")
    with engine.connect() as conn:
        mailers_ready = queries.jobs_by_state(conn, "ready", queue="mailers")
    assert {row["id"] for row in mailers_ready} == {a}


def test_jobs_by_state_paginates(engine, seed) -> None:
    for _ in range(5):
        seed.ready()
    with engine.connect() as conn:
        newest_first = [row["id"] for row in queries.jobs_by_state(conn, "ready")]
        page = queries.jobs_by_state(conn, "ready", limit=2, offset=2)
    assert [row["id"] for row in page] == newest_first[2:4]


def test_jobs_by_state_rejects_an_unknown_state(engine) -> None:
    # A caller typo used to surface as a bare KeyError from the exec-table lookup.
    with engine.connect() as conn, pytest.raises(ValueError, match="unknown state"):
        queries.jobs_by_state(conn, "not-a-state")


def test_jobs_by_state_rejects_a_negative_window(engine) -> None:
    with engine.connect() as conn:
        with pytest.raises(ValueError, match="limit"):
            queries.jobs_by_state(conn, "ready", limit=-1)
        with pytest.raises(ValueError, match="offset"):
            queries.jobs_by_state(conn, "ready", offset=-1)


def test_job_detail_failed_includes_error(engine, seed) -> None:
    job_id = seed.failed(error="Traceback...\nValueError: nope")
    with engine.connect() as conn:
        detail = queries.job_detail(conn, job_id)
    assert detail is not None
    assert detail["state"] == "failed"
    assert "ValueError: nope" in detail["error"]


def test_job_detail_missing_returns_none(engine) -> None:
    with engine.connect() as conn:
        assert queries.job_detail(conn, 999) is None


def test_queue_rows_reports_size_and_paused(engine, runtime, seed) -> None:
    seed.ready(queue="mailers")
    seed.ready(queue="mailers")
    seed.ready(queue="default")
    queues.pause(runtime, "mailers")
    with engine.connect() as conn:
        rows = {r["name"]: r for r in queries.queue_rows(conn, now_utc())}
    assert rows["mailers"]["size"] == 2
    assert rows["mailers"]["paused"] is True
    assert rows["default"]["paused"] is False


def test_queue_rows_includes_a_paused_queue_with_no_ready_work(engine, runtime) -> None:
    # A paused queue with nothing ready must still show up (that is how an operator finds the
    # pause to lift), with a zero size/latency rather than a missing row.
    queues.pause(runtime, "drained")
    with engine.connect() as conn:
        rows = queries.queue_rows(conn, now_utc())
    assert rows == [{"name": "drained", "size": 0, "latency": 0.0, "paused": True}]


def test_single_queue_reads_match_queue_rows(engine, runtime, seed) -> None:
    # queue_names/queue_size/queue_latency are the single-queue flavor of queue_rows (and what
    # queues.all_queues/size/latency delegate to); the two views must agree.
    seed.ready(queue="mailers")
    seed.ready(queue="mailers")
    seed.ready(queue="default")
    now = now_utc()
    with engine.connect() as conn:
        rows = {r["name"]: r for r in queries.queue_rows(conn, now)}
        assert queries.queue_names(conn) == ["default", "mailers"]
        for name, row in rows.items():
            assert queries.queue_size(conn, name) == row["size"]
            assert queries.queue_latency(conn, name, now) == row["latency"]
        assert queries.queue_size(conn, "absent") == 0
        assert queries.queue_latency(conn, "absent", now) == 0.0


def test_processes_alive_vs_stale(engine, seed) -> None:
    seed.process(name="fresh", age_seconds=0.0)
    seed.process(name="stale", age_seconds=10_000.0)
    with engine.connect() as conn:
        procs = {p["name"]: p for p in queries.processes(conn, now_utc())}
    assert procs["fresh"]["alive"] is True
    assert procs["stale"]["alive"] is False


def test_recurring_lists_registered_schedules(engine, seed) -> None:
    seed.recurring_task(key="nightly", schedule="0 3 * * *")
    with engine.connect() as conn:
        rows = queries.recurring(conn)
    assert [r["key"] for r in rows] == ["nightly"]
    assert rows[0]["schedule"] == "0 3 * * *"
