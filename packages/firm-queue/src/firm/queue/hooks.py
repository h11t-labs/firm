"""Lifecycle hooks (``on_(worker|dispatcher|scheduler)_(start|stop|exit)``) and per-job
middleware (:func:`around_perform`).

Register callbacks by event name; the supervisor/processes fire them at the right moments. A
lifecycle hook that raises never breaks the lifecycle — the error is routed to any
``thread_error`` handlers instead.

Per-job middleware is deliberately *not* best-effort: it wraps the job body, so an error in it
fails the job (and retries it) rather than letting a job run half-instrumented or, worse, look
successful when its cleanup never happened.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .._core.poller import default_on_error

if TYPE_CHECKING:
    from .job import Job

Hook = Callable[[], None]
ErrorHook = Callable[[BaseException], None]

# Process kinds that emit start/stop/exit events.
KINDS = ("supervisor", "worker", "dispatcher", "scheduler")


@dataclass(frozen=True)
class Execution:
    """One about-to-run job, as handed to :func:`around_perform` middleware.

    A record rather than loose arguments so fields can be added later without breaking every
    middleware that already exists. The job's ``args``/``kwargs`` are deliberately absent: they
    routinely carry user data, and a middleware that logs its input should have to ask for it.
    """

    job: Job
    job_id: int
    attempts: int


#: A middleware is a generator function: set up, ``yield`` once, clean up in a ``finally``.
Middleware = Callable[[Execution], Iterator[None]]


class LifecycleHooks:
    def __init__(self) -> None:
        self._hooks: dict[str, list[Hook]] = {}
        self._error_hooks: list[ErrorHook] = []
        self._middleware: list[Middleware] = []

    def register(self, event: str, fn: Hook) -> None:
        self._hooks.setdefault(event, []).append(fn)

    def register_error(self, fn: ErrorHook) -> None:
        self._error_hooks.append(fn)

    def register_middleware(self, fn: Middleware) -> None:
        self._middleware.append(fn)

    @contextlib.contextmanager
    def around_perform(self, execution: Execution) -> Iterator[None]:
        """Wrap one job body in every registered middleware.

        Registration order is outermost-first, so the first middleware registered sees the
        widest window — the same nesting a stack of decorators would give. Unlike
        :meth:`fire`, an exception here propagates: middleware wraps the unit of work, so a
        failure in it means the job did not run cleanly and must be treated as a failure.
        """
        with contextlib.ExitStack() as stack:
            for fn in self._middleware:
                stack.enter_context(contextlib.contextmanager(fn)(execution))
            yield

    def fire(self, event: str) -> None:
        for fn in self._hooks.get(event, []):
            try:
                fn()
            except Exception as exc:
                self.fire_error(exc)

    def fire_error(self, exc: BaseException) -> None:
        if not self._error_hooks:
            # Nobody listening: fall back to stderr rather than dropping the error — a
            # heartbeat or worker failure with zero diagnostics is how duplicate execution
            # gets "impossible to debug".
            default_on_error(exc)
            return
        for fn in self._error_hooks:
            with contextlib.suppress(Exception):  # error hooks are best-effort
                fn(exc)

    def clear(self) -> None:
        self._hooks.clear()
        self._error_hooks.clear()
        self._middleware.clear()


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


def around_perform(fn: Middleware) -> Middleware:
    """Register middleware that wraps every job body this process runs.

        from django.db import close_old_connections
        from firm.queue.hooks import around_perform

        @around_perform
        def close_django_connections(execution):
            try:
                yield
            finally:
                close_old_connections()

    The function must ``yield`` exactly once; firm wraps it with
    :func:`contextlib.contextmanager`. Anything before the ``yield`` runs before the job body,
    anything after runs when it returns, and a ``finally`` also runs when it raises.

    This is per *process*, not per job definition — register it at import time, in the module
    the worker loads (``--import``, or an app config under Django). It runs on the worker's job
    thread, so keep it cheap: it is on the path of every single job.

    An exception raised here fails the job like any other error, retries included.
    """
    HOOKS.register_middleware(fn)
    return fn
