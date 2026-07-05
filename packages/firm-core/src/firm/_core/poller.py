"""InterruptiblePoller — the one looping primitive the queue's processes are built on.

A poller runs ``poll()`` on a background thread, then sleeps on a ``threading.Event`` so it can
be woken or stopped immediately instead of waiting out the whole interval. ``interval`` is used
after a cycle that did work; ``idle_interval`` after an empty one (so a busy worker polls fast
and an idle one backs off). Worker, dispatcher, scheduler, maintenance, and heartbeat are all
just pollers with different ``poll()`` bodies.
"""

from __future__ import annotations

import sys
import threading
import traceback
from collections.abc import Callable


def default_on_error(exc: BaseException) -> None:
    """Last-resort error route for background loops: core can't import the queue's hooks
    (layering) and the project bans stdlib logging, so an unrouted poll error is written to
    stderr instead of vanishing. Callers override via ``on_error=``."""
    traceback.print_exception(exc, file=sys.stderr)


class InterruptiblePoller:
    def __init__(
        self,
        interval: float,
        *,
        name: str = "poller",
        idle_interval: float | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._interval = interval
        self._idle_interval = idle_interval if idle_interval is not None else interval
        self.name = name
        self._on_error = on_error if on_error is not None else default_on_error
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def poll(self) -> int:
        """One cycle; return the number of items processed (0 means idle)."""
        raise NotImplementedError

    def on_start(self) -> None:
        pass

    def on_stop(self) -> None:
        pass

    def _loop(self) -> None:
        self.on_start()
        try:
            while not self._stop.is_set():
                try:
                    did = self.poll()
                except Exception as exc:
                    did = 0
                    self._on_error(exc)
                except BaseException as exc:
                    # Not a per-cycle error (SystemExit, interpreter teardown): surface it,
                    # then let the thread die loudly through on_stop instead of vanishing.
                    self._on_error(exc)
                    raise
                wait = self._interval if did else self._idle_interval
                if self._wake.wait(timeout=wait):
                    self._wake.clear()
        finally:
            self.on_stop()

    def start(self) -> None:
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def wake(self) -> None:
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()
