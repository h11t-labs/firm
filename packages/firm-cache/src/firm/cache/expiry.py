"""Expiry — probabilistic trigger + FIFO eviction.

Each write *might* trigger an expiry run (~``2 / expiry_batch_size`` of the time), so expiry keeps
pace with writes without running on every one. A run evicts the oldest entries (by ``id``) when
the cache is over ``max_size``/``max_entries``, or any entry older than ``max_age``. It pulls 3x
the batch as candidates and randomly samples down to the batch, so concurrent runs rarely fight
over the same rows.
"""

from __future__ import annotations

import contextlib
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from .._core.clock import now_utc
from .._core.database import transaction
from .._core.poller import InterruptiblePoller
from . import schema
from .estimate import entry_count, estimate_size

if TYPE_CHECKING:
    from .store import Cache

_entries = schema.entries

EXPIRY_MULTIPLIER = 2


class Expiry:
    def __init__(self, cache: Cache) -> None:
        self.cache = cache
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bc-expiry")
        self._closed = False

    def maybe_trigger(self, writes: int = 1) -> None:
        if self._closed:  # a write after close() never schedules onto the dead pool
            return
        per_write = (1.0 / self.cache.expiry_batch_size) * EXPIRY_MULTIPLIER
        expected = writes * per_write
        runs = int(expected)
        if random.random() < (expected - runs):
            runs += 1
        for _ in range(runs):
            self._pool.submit(self._safe_run)

    def _safe_run(self) -> None:
        with contextlib.suppress(Exception):
            self.run_once()

    def run_once(self) -> int:
        """Evict one batch if the cache is over a limit or holding aged-out entries."""
        cache = self.cache
        with transaction(cache.engine) as conn:
            count = entry_count(conn)
            if count == 0:
                return 0

            full = cache.max_entries is not None and count > cache.max_entries
            if not full and cache.max_size is not None:
                full = estimate_size(conn, cache.size_estimate_samples) > cache.max_size
            if not full and cache.max_age is None:
                return 0

            candidates = conn.execute(
                select(_entries.c.id, _entries.c.created_at)
                .order_by(_entries.c.id)
                .limit(cache.expiry_batch_size * 3)
            ).all()
            if full:
                ids = [int(row.id) for row in candidates]
            else:
                cutoff = now_utc() - timedelta(seconds=cache.max_age or 0)
                ids = [int(row.id) for row in candidates if row.created_at < cutoff]

            if not ids:
                return 0
            chosen = random.sample(ids, min(cache.expiry_batch_size, len(ids)))
            conn.execute(delete(_entries).where(_entries.c.id.in_(chosen)))
            return len(chosen)

    def shutdown(self) -> None:
        # Drain any in-flight/queued eviction before the caller disposes the engine, so a
        # late run never re-opens connections on an engine the caller believes is closed
        # (mirrors Trimmer.shutdown in firm-channel).
        self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)


class ExpiryLoop(InterruptiblePoller):
    """Optional background loop that runs eviction on a timer."""

    def __init__(self, expiry: Expiry, interval: float = 60.0) -> None:
        super().__init__(interval, name="cache-expiry")
        self.expiry = expiry

    def poll(self) -> int:
        return self.expiry.run_once()
