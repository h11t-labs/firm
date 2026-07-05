"""Lifecycle-hook specs."""

from __future__ import annotations

from firm.queue.hooks import LifecycleHooks


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
