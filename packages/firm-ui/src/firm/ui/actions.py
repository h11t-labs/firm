"""Write actions for the dashboard.

Every mutation delegates to the owning module's public API (``firm.queue.queues`` /
``maintenance``, ``Cache.clear``, ``Channel.trim``) so the UI never re-implements — and can
never drift from — those semantics. Only the read-side queries touch the modules' schema
tables directly.
"""

from __future__ import annotations

from sqlalchemy import Engine

from firm._core.config import Runtime
from firm.queue import maintenance, queues


def pause(runtime: Runtime, queue: str) -> None:
    queues.pause(runtime, queue)


def resume(runtime: Runtime, queue: str) -> None:
    queues.resume(runtime, queue)


def retry(runtime: Runtime, job_id: int) -> bool:
    return maintenance.retry_failed(runtime, job_id)


def retry_all(runtime: Runtime) -> int:
    return maintenance.retry_all_failed(runtime)


def discard(runtime: Runtime, job_id: int) -> bool:
    """Delete a job and everything attached to it; refused while the job is running."""
    return maintenance.discard_job(runtime, job_id)


def clear_cache(engine: Engine) -> int:
    """Delete every cache entry. Returns how many rows were removed."""
    from firm.cache import Cache

    cache = Cache(engine=engine, create_schema=False, auto_expire=False)
    try:
        return cache.clear()
    finally:
        cache.close()


def trim_channel(engine: Engine) -> int:
    """Delete *all* channel messages older than the default retention (1 day) and return the total.

    ``Channel.trim()`` removes a single batch (``trim_batch_size``), so one dashboard click loops
    until everything expired is gone — matching the button's "older than 1 day" promise.
    """
    from firm.channel import Channel

    channel = Channel(engine=engine, create_schema=False)
    try:
        total = 0
        while True:
            removed = channel.trim()
            total += removed
            if removed < channel.trim_batch_size:
                break
        return total
    finally:
        channel.close()
