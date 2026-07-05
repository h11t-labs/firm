"""InterruptiblePoller specs."""

from __future__ import annotations

import threading
import time

from firm._core.poller import InterruptiblePoller


class _CountingPoller(InterruptiblePoller):
    def __init__(self) -> None:
        super().__init__(0.01, name="counter")
        self.count = 0
        self.ran = threading.Event()

    def poll(self) -> int:
        self.count += 1
        self.ran.set()
        return 0


def test_poller_runs_then_stops() -> None:
    poller = _CountingPoller()
    poller.start()
    assert poller.ran.wait(2.0)
    poller.stop()
    assert poller.stopping
    assert poller.count >= 1


def test_poll_error_is_routed_not_fatal() -> None:
    errors: list[BaseException] = []

    class _Boom(InterruptiblePoller):
        def __init__(self) -> None:
            super().__init__(0.01, name="boom", on_error=errors.append)

        def poll(self) -> int:
            raise ValueError("x")

    poller = _Boom()
    poller.start()
    time.sleep(0.1)
    poller.stop()
    assert any(isinstance(e, ValueError) for e in errors)


def test_base_exception_escape_is_surfaced_then_fatal() -> None:
    """A non-Exception escape from poll() (SystemExit, interpreter teardown) is not a
    per-cycle error: it must reach on_error and then end the thread, not loop silently."""
    errors: list[BaseException] = []
    stopped = threading.Event()

    class _Exiting(InterruptiblePoller):
        def __init__(self) -> None:
            super().__init__(0.01, name="exiting", on_error=errors.append)

        def poll(self) -> int:
            raise SystemExit(2)

        def on_stop(self) -> None:
            stopped.set()

    poller = _Exiting()
    poller.start()
    assert stopped.wait(2.0), "poll thread should have died through on_stop"
    assert any(isinstance(e, SystemExit) for e in errors)
