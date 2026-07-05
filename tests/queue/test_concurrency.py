"""Concurrency-control specs."""

from __future__ import annotations

from collections.abc import Callable

import firm.queue as bq
from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.worker import run_ready

_SINK: list[int] = []


@bq.job(concurrency={"key": lambda x: f"k{x}", "to": 1, "duration": 300})
def limited(x: int) -> None:
    _SINK.append(x)


@bq.job(concurrency={"key": lambda x: f"d{x}", "to": 1, "on_conflict": "discard"})
def discardable(x: int) -> None:
    _SINK.append(x)


@bq.job(concurrency={"key": lambda x: f"t{x}", "to": 2})
def throttled(x: int) -> None:
    _SINK.append(x)


def test_first_acquires_second_blocks(runtime: Runtime, count: Callable[..., int]) -> None:
    limited.enqueue(1)
    limited.enqueue(1)
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 1
    assert count(schema.semaphores) == 1


def test_different_keys_do_not_block(runtime: Runtime, count: Callable[..., int]) -> None:
    limited.enqueue(1)
    limited.enqueue(2)
    assert count(schema.ready_executions) == 2
    assert count(schema.blocked_executions) == 0


def test_discard_on_conflict(runtime: Runtime, count: Callable[..., int]) -> None:
    first = discardable.enqueue(5)
    second = discardable.enqueue(5)
    assert first is not None
    assert second is None
    assert count(schema.jobs) == 1
    assert count(schema.ready_executions) == 1
    assert count(schema.blocked_executions) == 0


def test_throttle_limit_two(runtime: Runtime, count: Callable[..., int]) -> None:
    throttled.enqueue(1)
    throttled.enqueue(1)
    throttled.enqueue(1)
    assert count(schema.ready_executions) == 2
    assert count(schema.blocked_executions) == 1


def test_release_promotes_next_blocked(runtime: Runtime, count: Callable[..., int]) -> None:
    _SINK.clear()
    limited.enqueue(7)
    limited.enqueue(7)
    assert count(schema.blocked_executions) == 1

    run_ready(runtime)
    assert _SINK == [7]
    assert count(schema.blocked_executions) == 0
    assert count(schema.ready_executions) == 1

    run_ready(runtime)
    assert _SINK == [7, 7]
