"""The ``@job`` decorator and the ``Job`` wrapper.

``@job`` turns a plain function into something that is still directly callable (so unit-testing
the body is trivial) but also knows how to enqueue itself. The function's ``module.qualname``
becomes the stored ``class_name``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .._core.clock import now_utc
from . import enqueue as _enqueue
from .concurrency import ConcurrencySpec
from .registry import REGISTRY


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry a failing job, and the backoff between attempts."""

    max_attempts: int = 1
    backoff_base: float = 3.0

    def retry_delay(self, attempt: int) -> float | None:
        """Seconds to wait before ``attempt`` (1-based), or ``None`` once retries are exhausted."""
        if attempt >= self.max_attempts:
            return None
        return self.backoff_base**attempt


class Job:
    """A registered, enqueueable callable."""

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        class_name: str,
        queue_name: str,
        priority: int,
        retry_policy: RetryPolicy,
        concurrency: ConcurrencySpec | None = None,
    ) -> None:
        self.func = func
        self.class_name = class_name
        self.queue_name = queue_name
        self.priority = priority
        self.retry_policy = retry_policy
        self.concurrency = concurrency
        functools.update_wrapper(self, func)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)

    # `perform` is the name the worker uses; identical to calling the job directly.
    def perform(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)

    def enqueue(self, *args: Any, **kwargs: Any) -> int | None:
        """Enqueue the job. Returns the new ``job_id``, or ``None`` if discarded on conflict."""
        return _enqueue.enqueue(self, args, kwargs)

    def enqueue_at(self, when: datetime, /, *args: Any, **kwargs: Any) -> int | None:
        return _enqueue.enqueue(self, args, kwargs, scheduled_at=when)

    def enqueue_in(self, delta: timedelta, /, *args: Any, **kwargs: Any) -> int | None:
        return _enqueue.enqueue(self, args, kwargs, scheduled_at=now_utc() + delta)


def job(
    *,
    queue: str = "default",
    priority: int = 0,
    attempts: int = 1,
    backoff: float = 3.0,
    concurrency: dict[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], Job]:
    """Decorate a function to make it an enqueueable :class:`Job`."""

    def decorator(func: Callable[..., Any]) -> Job:
        qualname = getattr(func, "__qualname__", None) or getattr(func, "__name__", "job")
        class_name = f"{func.__module__}.{qualname}"
        wrapped = Job(
            func,
            class_name=class_name,
            queue_name=queue,
            priority=priority,
            retry_policy=RetryPolicy(max_attempts=attempts, backoff_base=backoff),
            concurrency=ConcurrencySpec.parse(concurrency, class_name=class_name),
        )
        REGISTRY.register(class_name, wrapped)
        return wrapped

    return decorator
