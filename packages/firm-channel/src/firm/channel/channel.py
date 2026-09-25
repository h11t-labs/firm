"""The ``Channel`` — database-backed publish/subscribe.

``broadcast`` inserts a message row. ``subscribe`` registers an in-process callback and starts a
background :class:`Listener` that polls for new rows on the subscribed channels and hands each
payload to the matching callbacks. A subscriber only receives messages broadcast *after* it
subscribed; delivery is per-process (every process running a ``Channel`` sees every broadcast).

Delivery guarantee: on Postgres/MySQL concurrent broadcasters can commit out of id order, so
the listener re-scans a bounded window instead of trusting a max-id watermark — a message is
delivered as long as its transaction commits within ``commit_grace`` seconds of executing the
insert (and broadcaster/listener clocks agree within the same margin). Re-scanned rows are
de-duplicated, so callbacks still see each message once per subscription.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import Engine

from .._core.clock import now_utc
from .._core.database import create_engine_for, dispose_engine, transaction
from .._core.dialects import get_dialect
from .._core.poller import InterruptiblePoller, default_on_error
from . import messages, schema
from .keys import channel_hash, normalize_channel
from .trim import Trimmer

Callback = Callable[[bytes], None]

DEFAULT_POLLING_INTERVAL = 0.1
ONE_DAY_SECONDS = 24 * 3600.0
DEFAULT_TRIM_BATCH_SIZE = 100
DEFAULT_COMMIT_GRACE_SECONDS = 5.0


class Channel:
    def __init__(
        self,
        database_url: str | None = None,
        *,
        engine: Engine | None = None,
        polling_interval: float = DEFAULT_POLLING_INTERVAL,
        message_retention: float = ONE_DAY_SECONDS,
        auto_trim: bool = True,  # named like the cache's auto_expire
        trim_batch_size: int = DEFAULT_TRIM_BATCH_SIZE,
        create_schema: bool = True,
        commit_grace: float = DEFAULT_COMMIT_GRACE_SECONDS,
        on_error: Callable[[BaseException], None] | None = None,
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
        self.auto_trim = auto_trim
        self.trim_batch_size = trim_batch_size
        self.commit_grace = commit_grace
        # Listener/trim failures are routed here (default: traceback to stderr). Subscriber
        # callback errors stay suppressed by design — one bad subscriber can't break the rest.
        self.on_error = on_error if on_error is not None else default_on_error

        if create_schema:
            schema.create_all(self.engine)

        self._lock = threading.Lock()
        self._subscribers: dict[bytes, list[Callback]] = {}
        # Per-channel subscription anchor: only ids above it are delivered, so a fresh
        # subscription never replays the pre-existing backlog.
        self._channel_anchor: dict[bytes, int] = {}
        # Scan floor: every id <= floor was either delivered or has out-waited commit_grace.
        # The listener re-queries everything above it and de-duplicates via _delivered, which
        # holds the already-dispatched ids still inside the re-scan window.
        self._scan_floor = 0
        self._delivered: set[int] = set()
        self._listener: Listener | None = None
        self.trimmer = Trimmer(self)

    def broadcast(self, channel: str | bytes, payload: str | bytes) -> None:
        """Publish ``payload`` to ``channel``. Subscribers in any process receive it on their next
        poll. Payloads are opaque bytes (``str`` is UTF-8 encoded)."""
        ch = normalize_channel(channel)
        data = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
        with transaction(self.engine) as conn:
            messages.insert_message(conn, ch, data)
        if self.auto_trim:
            self.trimmer.maybe_trigger(1)

    def subscribe(self, channel: str | bytes, callback: Callback) -> None:
        """Call ``callback(payload)`` for each future message on ``channel``. Starts the background
        listener on the first subscription."""
        ch = normalize_channel(channel)
        with self._lock:
            self._ensure_listener_locked()
            if ch not in self._subscribers:
                self._subscribers[ch] = []
                # A fresh channel only sees messages from now on, not the existing backlog.
                # Anchor at the current max id: broadcasts committed by subscribe time are
                # excluded, but ``current_max_id`` sees only committed ids, so a broadcast whose
                # insert is still in flight (commits after this) has a higher id and will be
                # delivered.
                with transaction(self.engine) as conn:
                    self._channel_anchor[ch] = messages.current_max_id(conn)
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
                self._channel_anchor.pop(ch, None)

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
        """Start the listener thread once, anchoring the scan floor at the current max id so
        no pre-existing message is delivered. Caller holds ``self._lock``."""
        if self._listener is None:
            with transaction(self.engine) as conn:
                self._scan_floor = messages.current_max_id(conn)
            self._listener = Listener(self, self.polling_interval)
            self._listener.start()

    def _dispatch_new(self) -> int:
        """One listener cycle: fetch messages above the scan floor on subscribed channels and
        hand each undelivered one to its callbacks. Returns the number of deliveries made.

        Concurrent broadcasters can commit out of id order (Postgres/MySQL), so a plain
        max-id watermark would skip a lower id that commits after a higher one was seen.
        Instead the floor only advances past rows older than ``commit_grace`` — id order
        tracks insert order, so everything below such a row has either committed (and been
        fetched) or sat uncommitted longer than the grace period — and the window above the
        floor is re-scanned each cycle with ``_delivered`` preventing duplicate dispatch.
        """
        with self._lock:
            if not self._subscribers:
                return 0
            channels = list(self._subscribers.keys())
            floor = self._scan_floor
        hashes = [channel_hash(c) for c in channels]
        cutoff = now_utc() - timedelta(seconds=self.commit_grace)

        with transaction(self.engine) as conn:
            rows = messages.fetch_since(conn, hashes, floor)

        delivered = 0
        confirmed_floor = floor
        for row in rows:
            ch = bytes(row.channel)
            payload = bytes(row.payload)
            message_id = int(row.id)
            if row.created_at <= cutoff:
                confirmed_floor = max(confirmed_floor, message_id)
            with self._lock:
                callbacks = self._subscribers.get(ch)
                anchor = self._channel_anchor.get(ch, 0)
                if callbacks is None or message_id <= anchor or message_id in self._delivered:
                    continue
                self._delivered.add(message_id)
                targets = list(callbacks)
            for callback in targets:
                # A subscriber error never breaks the listener or the other subscribers.
                with contextlib.suppress(Exception):
                    callback(payload)
                delivered += 1
        with self._lock:
            if confirmed_floor > self._scan_floor:
                self._scan_floor = confirmed_floor
                self._delivered = {i for i in self._delivered if i > confirmed_floor}
        return delivered


class Listener(InterruptiblePoller):
    """Background thread that polls for new messages and dispatches them to subscribers."""

    def __init__(self, channel: Channel, interval: float) -> None:
        super().__init__(interval, name="channel-listener", on_error=channel.on_error)
        self.channel = channel

    def poll(self) -> int:
        return self.channel._dispatch_new()
