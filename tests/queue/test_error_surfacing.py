"""Background-loop errors must surface somewhere (audit X-1 / PLAN 1.7).

Core pollers default to a stderr traceback (core can't import the queue's hooks and the
project bans stdlib logging); the queue routes through HOOKS.fire_error, which itself falls
back to stderr when nobody registered an error hook.
"""

from __future__ import annotations

import threading

from firm._core.poller import InterruptiblePoller
from firm.queue.hooks import LifecycleHooks


def test_unrouted_poller_error_lands_on_stderr(capfd) -> None:
    ran = threading.Event()

    class _Boom(InterruptiblePoller):
        def __init__(self) -> None:
            super().__init__(0.01, name="boom-default")

        def poll(self) -> int:
            ran.set()
            raise ValueError("surfaced-not-swallowed")

    poller = _Boom()
    poller.start()
    assert ran.wait(2.0)
    poller.stop()

    err = capfd.readouterr().err
    assert "surfaced-not-swallowed" in err
    assert "ValueError" in err


def test_fire_error_falls_back_to_stderr_without_hooks(capfd) -> None:
    hooks = LifecycleHooks()
    hooks.fire_error(RuntimeError("heartbeat-went-dark"))
    assert "heartbeat-went-dark" in capfd.readouterr().err


def test_fire_error_routes_to_registered_hooks_only(capfd) -> None:
    hooks = LifecycleHooks()
    seen: list[BaseException] = []
    hooks.register_error(seen.append)
    hooks.fire_error(RuntimeError("routed"))
    assert [str(e) for e in seen] == ["routed"]
    assert capfd.readouterr().err == ""
