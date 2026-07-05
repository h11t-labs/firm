"""The ``Channel`` — database-backed publish/subscribe.

``broadcast`` inserts a message row. ``subscribe`` registers an in-process callback and starts a
background :class:`Listener` that polls for new rows on the subscribed channels and hands each
payload to the matching callbacks. A subscriber only receives messages broadcast *after* it
subscribed; delivery is per-process (every process running a ``Channel`` sees every broadcast).
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable

from sqlalchemy import Engine

from .._core.database import create_engine_for, dispose_engine, transaction
from .._core.dialects import get_dialect
from .._core.poller import InterruptiblePoller
from . import messages, schema
from .keys import channel_hash, normalize_channel
from .trim import Trimmer

Callback = Callable[[bytes], None]

DEFAULT_POLLING_INTERVAL = 0.1
ONE_DAY_SECONDS = 24 * 3600.0
DEFAULT_TRIM_BATCH_SIZE = 100


class Channel:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        polling_interval: float = DEFAULT_POLLING_INTERVAL,
        message_retention: float = ONE_DAY_SECONDS,
        autotrim: bool = True,
        trim_batch_size: int = DEFAULT_TRIM_BATCH_SIZE,
        create_schema: bool = True,
    ) -> None:
        if engine is not None:
            self.engine = engine
            self._owns_engine = False
        elif database_url is not None:
            self.engine = create_engine_for(database_url)
            self._owns_engine = True
        else:
            raise ValueError("Channel requires either database_url or engine")

        self.dialect = get_dialect(self.engine)
        self.polling_interval = polling_interval
        self.message_retention = message_retention
        self.autotrim = autotrim
        self.trim_batch_size = trim_batch_size

        if create_schema:
            schema.create_all(self.engine)

        self._lock = threading.Lock()
        self._subscribers: dict[bytes, list[Callback]] = {}
        self._channel_last_id: dict[bytes, int] = {}
        self._global_last_id = 0
        self._listener: Listener | None = None
        self.trimmer = Trimmer(self)

    def broadcast(self, channel: str | bytes, payload: str | bytes) -> None:
        """Publish ``payload`` to ``channel``. Subscribers in any process receive it on their next
        poll. Payloads are opaque bytes (``str`` is UTF-8 encoded)."""
        ch = normalize_channel(channel)
        data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        with transaction(self.engine) as conn:
            messages.insert_message(conn, ch, data)
        if self.autotrim:
            self.trimmer.maybe_trigger(1)

    def subscribe(self, channel: str | bytes, callback: Callback) -> None:
        """Call ``callback(payload)`` for each future message on ``channel``. Starts the background
        listener on the first subscription."""
        ch = normalize_channel(channel)
        with self._lock:
            self._ensure_listener_locked()
            if ch not in self._subscribers:
                self._subscribers[ch] = []
                # A fresh channel only sees messages from now on, not the existing backlog. Anchor
                # its cursor to the *current* max id rather than the global cursor — the global
                # cursor only tracks subscribed channels, so it lags behind broadcasts made to a
                # channel while nobody was listening to it yet (which would otherwise replay).
                with transaction(self.engine) as conn:
                    self._channel_last_id[ch] = messages.current_max_id(conn)
            self._subscribers[ch].append(callback)

    def unsubscribe(self, channel: str | bytes, callback: Callback) -> None:
        """Remove a previously registered ``callback``. The listener keeps running for the rest."""
        ch = normalize_channel(channel)
        with self._lock:
            callbacks = self._subscribers.get(ch)
            if not callbacks:
                return
            with contextlib.suppress(ValueError):
                callbacks.remove(callback)
            if not callbacks:
                del self._subscribers[ch]
                self._channel_last_id.pop(ch, None)

    def trim(self) -> int:
        """Delete a batch of messages older than ``message_retention``. Returns how many."""
        return self.trimmer.run_once()

    def close(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        self.trimmer.shutdown()
        if self._owns_engine:
            dispose_engine(self.engine)

    def __enter__(self) -> Channel:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------------------------

    def _ensure_listener_locked(self) -> None:
        """Start the listener thread once, anchoring ``_global_last_id`` at the current max id so
        no pre-existing message is delivered. Caller holds ``self._lock``."""
        if self._listener is None:
            with transaction(self.engine) as conn:
                self._global_last_id = messages.current_max_id(conn)
            self._listener = Listener(self, self.polling_interval)
            self._listener.start()

    def _dispatch_new(self) -> int:
        """One listener cycle: fetch messages newer than ``_global_last_id`` on subscribed channels
        and hand each to its callbacks. Returns the number of (callback) deliveries made."""
        with self._lock:
            if not self._subscribers:
                return 0
            channels = list(self._subscribers.keys())
            after = self._global_last_id
        hashes = [channel_hash(c) for c in channels]

        with transaction(self.engine) as conn:
            rows = messages.fetch_since(conn, hashes, after)

        delivered = 0
        for row in rows:
            ch = bytes(row.channel)
            payload = bytes(row.payload)
            message_id = int(row.id)
            with self._lock:
                # Advance the global cursor past every fetched row (including channel_hash
                # collisions for channels we don't actually subscribe to).
                self._global_last_id = max(self._global_last_id, message_id)
                callbacks = self._subscribers.get(ch)
                last = self._channel_last_id.get(ch)
                if callbacks is None or (last is not None and last >= message_id):
                    continue
                self._channel_last_id[ch] = message_id
                targets = list(callbacks)
            for callback in targets:
                # A subscriber error never breaks the listener or the other subscribers.
                with contextlib.suppress(Exception):
                    callback(payload)
                delivered += 1
        return delivered


class Listener(InterruptiblePoller):
    """Background thread that polls for new messages and dispatches them to subscribers."""

    def __init__(self, channel: Channel, interval: float) -> None:
        super().__init__(interval, name="channel-listener")
        self.channel = channel

    def poll(self) -> int:
        return self.channel._dispatch_new()
