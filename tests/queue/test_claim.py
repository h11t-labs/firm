"""Claim specs — ready/claimed execution lifecycle."""

from __future__ import annotations

import threading
from collections.abc import Callable

from sqlalchemy import Engine, insert

from firm._core.dialects import get_dialect
from firm.queue import schema
from firm.queue.claim import claim_ready


def test_orders_by_priority_then_job_id(engine: Engine, add_ready: Callable[..., int]) -> None:
    a = add_ready(priority=2)
    b = add_ready(priority=1)
    c = add_ready(priority=1)
    claimed = claim_ready(engine, get_dialect(engine), ["*"], 10, None)
    assert claimed == [b, c, a]


def test_respects_limit(
    engine: Engine, add_ready: Callable[..., int], count: Callable[..., int]
) -> None:
    for _ in range(5):
        add_ready()
    claimed = claim_ready(engine, get_dialect(engine), ["*"], 2, None)
    assert len(claimed) == 2
    assert count(schema.ready_executions) == 3
    assert count(schema.claimed_executions) == 2


def test_excludes_paused_queue(engine: Engine, add_ready: Callable[..., int]) -> None:
    keep = add_ready(queue="default")
    add_ready(queue="reports")
    with engine.begin() as conn:
        conn.execute(insert(schema.pauses).values(queue_name="reports"))
    claimed = claim_ready(engine, get_dialect(engine), ["*"], 10, None)
    assert claimed == [keep]


def test_prefix_match(engine: Engine, add_ready: Callable[..., int]) -> None:
    hi = add_ready(queue="mailers_high")
    lo = add_ready(queue="mailers_low")
    add_ready(queue="reports")
    claimed = claim_ready(engine, get_dialect(engine), ["mailers*"], 10, None)
    assert set(claimed) == {hi, lo}


def test_exact_queue_only(engine: Engine, add_ready: Callable[..., int]) -> None:
    add_ready(queue="default")
    reports = add_ready(queue="reports")
    claimed = claim_ready(engine, get_dialect(engine), ["reports"], 10, None)
    assert claimed == [reports]


def test_empty_queue_claims_nothing(engine: Engine) -> None:
    assert claim_ready(engine, get_dialect(engine), ["*"], 10, None) == []


def test_two_threads_never_double_claim(
    engine: Engine, add_ready: Callable[..., int], count: Callable[..., int]
) -> None:
    job_ids = [add_ready() for _ in range(50)]
    dialect = get_dialect(engine)
    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        mine: list[int] = []
        while True:
            got = claim_ready(engine, dialect, ["*"], 5, None)
            if not got:
                break
            mine.extend(got)
        with lock:
            results.extend(mine)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == sorted(job_ids)
    assert len(results) == len(set(results))
    assert count(schema.ready_executions) == 0
    assert count(schema.claimed_executions) == 50


def test_immediate_flag_does_not_leak_to_later_transactions(tmp_path) -> None:
    """conn.info survives pool check-in: without clearing the flag, one immediate
    transaction turned every later plain transaction on that pooled connection into
    BEGIN IMMEDIATE, serializing reads for the life of the connection (PLAN 2.2)."""
    from sqlalchemy import event

    from firm._core.database import (
        create_engine_for,
        immediate_transaction,
        transaction,
    )

    engine = create_engine_for(f"sqlite:///{tmp_path / 'leak.db'}", pool_size=1, max_overflow=0)
    begins: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.startswith("BEGIN"):
            begins.append(statement)

    try:
        with immediate_transaction(engine) as conn:
            conn.exec_driver_sql("SELECT 1")
        with transaction(engine) as conn:  # same pooled connection (pool of one)
            conn.exec_driver_sql("SELECT 1")
    finally:
        engine.dispose()

    assert begins == ["BEGIN IMMEDIATE", "BEGIN"]


def _claim_all(engine: Engine, queues: list[str], limit: int = 100) -> list[int]:
    return claim_ready(engine, get_dialect(engine), queues, limit, None)


def test_claim_using_both_exact_names_and_a_prefix(
    engine: Engine, add_ready: Callable[..., int]
) -> None:
    # upstream: ready_execution_test.rb "claim jobs using both exact names and a prefix".
    # An exact name AND a "prefix*" selector in one spec list claim from both.
    backend = add_ready(queue="backend")
    mailers_high = add_ready(queue="mailers_high")
    mailers_low = add_ready(queue="mailers_low")
    add_ready(queue="reports")  # not selected

    claimed = _claim_all(engine, ["backend", "mailers*"])

    assert set(claimed) == {backend, mailers_high, mailers_low}


def test_queue_order_then_priority_with_list_of_queues(
    engine: Engine, add_ready: Callable[..., int]
) -> None:
    # upstream: ready_execution_test.rb "queue order and then priority is respected when using a
    # list of queues". Jobs in the earlier-listed queue are claimed before later ones, then by
    # priority within a queue -- even when a later queue holds higher-priority work.
    q1_high = add_ready(queue="queue1", priority=1)
    q1_low = add_ready(queue="queue1", priority=5)
    q2_high = add_ready(queue="queue2", priority=1)
    q2_low = add_ready(queue="queue2", priority=5)

    claimed = _claim_all(engine, ["queue2", "queue1"])

    # queue2 first (by priority within), then queue1 (by priority within).
    assert claimed == [q2_high, q2_low, q1_high, q1_low]


def test_queue_order_respected_when_using_prefixes(
    engine: Engine, add_ready: Callable[..., int]
) -> None:
    # upstream: ready_execution_test.rb "queue order is respected when using prefixes".
    # With prefix selectors, queues are still polled in the order their selectors are listed.
    alpha = add_ready(queue="alpha_one", priority=1)
    beta = add_ready(queue="beta_one", priority=5)

    claimed = _claim_all(engine, ["beta*", "alpha*"])

    assert claimed == [beta, alpha]


def test_queue_order_respected_when_mixing_names_and_prefixes(
    engine: Engine, add_ready: Callable[..., int]
) -> None:
    # upstream: ready_execution_test.rb "queue order is respected when mixing exact names with
    # prefixes". An exact name listed first is polled before a later prefix selector.
    exact = add_ready(queue="urgent", priority=5)
    prefixed = add_ready(queue="mailers_high", priority=1)

    claimed = _claim_all(engine, ["urgent", "mailers*"])

    assert claimed == [exact, prefixed]
