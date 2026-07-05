"""Trimming — probabilistic, age-based deletion of old messages.

Each broadcast *might* trigger a trim (~``2 / trim_batch_size`` of the time), so the buffer table
stays bounded without a dedicated sweeper. A trim deletes up to ``trim_batch_size`` rows older than
``message_retention``, using ``FOR UPDATE SKIP LOCKED`` so concurrent trims never fight.
"""

from __future__ import annotations

import contextlib
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import TYPE_CHECKING

from .._core.clock import now_utc
from . import messages

if TYPE_CHECKING:
    from .channel import Channel

TRIM_MULTIPLIER = 2


class Trimmer:
    def __init__(self, channel: Channel) -> None:
        self.channel = channel
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bch-trim")
        self._closed = False

    def maybe_trigger(self, writes: int = 1) -> None:
        if self._closed:  # a broadcast after close() never schedules onto the dead pool
            return
        per_write = (1.0 / self.channel.trim_batch_size) * TRIM_MULTIPLIER
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
        """Delete one batch of messages older than ``message_retention``. Returns how many."""
        ps = self.channel
        cutoff = now_utc() - timedelta(seconds=ps.message_retention)
        return messages.trim_old(ps.engine, ps.dialect, cutoff, ps.trim_batch_size)

    def shutdown(self) -> None:
        # Drain any in-flight/queued trim before the caller disposes the engine, so a late trim
        # never re-opens connections on an engine the caller believes is closed.
        self._closed = True
        self._pool.shutdown(wait=True, cancel_futures=True)
