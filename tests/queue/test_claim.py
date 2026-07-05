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
