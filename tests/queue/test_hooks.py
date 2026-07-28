"""Lifecycle-hook specs."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest

from firm.queue.hooks import Execution, LifecycleHooks


def test_fire_invokes_registered_hooks_in_order() -> None:
    hooks = LifecycleHooks()
    calls: list[str] = []
    hooks.register("worker_start", lambda: calls.append("a"))
    hooks.register("worker_start", lambda: calls.append("b"))
    hooks.fire("worker_start")
    assert calls == ["a", "b"]


def test_hook_error_is_routed_not_raised() -> None:
    hooks = LifecycleHooks()
    errors: list[BaseException] = []
    hooks.register_error(errors.append)

    def boom() -> None:
        raise ValueError("x")

    hooks.register("worker_stop", boom)
    hooks.fire("worker_stop")  # must not raise

    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)


def test_fire_unknown_event_is_noop() -> None:
    LifecycleHooks().fire("never_registered")


# --- per-job middleware ---------------------------------------------------------------------


def _execution() -> Execution:
    return Execution(job=cast(Any, object()), job_id=1, attempts=1)


def test_middleware_wraps_the_body_in_registration_order() -> None:
    hooks = LifecycleHooks()
    calls: list[str] = []

    def outer(_execution: Execution) -> Iterator[None]:
        calls.append("outer in")
        yield
        calls.append("outer out")

    def inner(_execution: Execution) -> Iterator[None]:
        calls.append("inner in")
        yield
        calls.append("inner out")

    hooks.register_middleware(outer)
    hooks.register_middleware(inner)
    with hooks.around_perform(_execution()):
        calls.append("body")

    # First registered is outermost, the way stacked decorators read.
    assert calls == ["outer in", "inner in", "body", "inner out", "outer out"]


def test_middleware_cleanup_runs_when_the_body_raises() -> None:
    """The reason this exists: a `finally` has to run for a failing job too, or a worker leaks
    whatever the middleware was there to release."""
    hooks = LifecycleHooks()
    cleaned: list[str] = []

    def cleanup(_execution: Execution) -> Iterator[None]:
        try:
            yield
        finally:
            cleaned.append("closed")

    hooks.register_middleware(cleanup)
    with pytest.raises(ValueError), hooks.around_perform(_execution()):
        raise ValueError("job body blew up")

    assert cleaned == ["closed"]


def test_middleware_error_propagates_unlike_a_lifecycle_hook() -> None:
    """A lifecycle hook that raises is swallowed and reported; middleware wraps the unit of
    work, so its failure has to fail the job instead of letting it look successful."""
    hooks = LifecycleHooks()
    errors: list[BaseException] = []
    hooks.register_error(errors.append)

    def boom(_execution: Execution) -> Iterator[None]:
        raise RuntimeError("middleware is broken")
        yield  # pragma: no cover

    hooks.register_middleware(boom)
    with pytest.raises(RuntimeError), hooks.around_perform(_execution()):
        pass  # pragma: no cover - never reached

    assert errors == []  # not routed away; the caller sees it


def test_execution_carries_no_job_arguments() -> None:
    """Job arguments routinely hold user data, so middleware has to ask for them explicitly
    rather than getting them handed over by default."""
    assert not hasattr(_execution(), "args")
    assert not hasattr(_execution(), "kwargs")


def test_clear_drops_middleware_too() -> None:
    hooks = LifecycleHooks()
    hooks.register_middleware(lambda _execution: iter([None]))
    hooks.clear()
    calls: list[str] = []
    with hooks.around_perform(_execution()):
        calls.append("body")
    assert calls == ["body"]
