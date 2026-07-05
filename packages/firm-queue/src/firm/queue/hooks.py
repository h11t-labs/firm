"""Lifecycle hooks (``on_(worker|dispatcher|scheduler)_(start|stop|exit)``).

Register callbacks by event name; the supervisor/processes fire them at the right moments. A
hook that raises never breaks the lifecycle — the error is routed to any ``thread_error``
handlers instead.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

Hook = Callable[[], None]
ErrorHook = Callable[[BaseException], None]

# Process kinds that emit start/stop/exit events.
KINDS = ("supervisor", "worker", "dispatcher", "scheduler")


class LifecycleHooks:
    def __init__(self) -> None:
        self._hooks: dict[str, list[Hook]] = {}
        self._error_hooks: list[ErrorHook] = []

    def register(self, event: str, fn: Hook) -> None:
        self._hooks.setdefault(event, []).append(fn)

    def register_error(self, fn: ErrorHook) -> None:
        self._error_hooks.append(fn)

    def fire(self, event: str) -> None:
        for fn in self._hooks.get(event, []):
            try:
                fn()
            except Exception as exc:
                self.fire_error(exc)

    def fire_error(self, exc: BaseException) -> None:
        for fn in self._error_hooks:
            with contextlib.suppress(Exception):  # error hooks are best-effort
                fn(exc)

    def clear(self) -> None:
        self._hooks.clear()
        self._error_hooks.clear()


HOOKS = LifecycleHooks()


def on(event: str) -> Callable[[Hook], Hook]:
    """Register a hook for an arbitrary ``"{kind}_{phase}"`` event (e.g. ``"worker_start"``)."""

    def decorator(fn: Hook) -> Hook:
        HOOKS.register(event, fn)
        return fn

    return decorator


def on_thread_error(fn: ErrorHook) -> ErrorHook:
    HOOKS.register_error(fn)
    return fn
