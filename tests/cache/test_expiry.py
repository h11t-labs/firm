"""Eviction specs — FIFO over-limit eviction and max-age expiry."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.cache import Cache, schema
from firm.cache.estimate import entry_count
from firm.cache.keys import key_hash, normalize_key


def _remaining_ids(cache: Cache) -> set[int]:
    with transaction(cache.engine) as conn:
        return {int(row.id) for row in conn.execute(select(schema.entries.c.id))}


def test_evicts_oldest_when_over_max_entries(db_url: str) -> None:
    cache = Cache(
        database_url=db_url,
        max_entries=5,
        max_size=None,
        max_age=None,
        expiry_batch_size=2,
        auto_expire=False,
    )
    try:
        for i in range(10):
            cache.set(f"k{i}", i)

        assert cache.expiry.run_once() == 2
        with transaction(cache.engine) as conn:
            assert entry_count(conn) == 8
        assert {7, 8, 9, 10}.issubset(_remaining_ids(cache))
    finally:
        cache.close()


def test_evicts_entries_older_than_max_age(db_url: str) -> None:
    cache = Cache(
        database_url=db_url,
        max_entries=None,
        max_size=None,
        max_age=3600.0,
        auto_expire=False,
    )
    try:
        cache.set("old", 1)
        cache.set("new", 2)
        with transaction(cache.engine) as conn:
            conn.execute(
                update(schema.entries)
                .where(schema.entries.c.key_hash == key_hash(normalize_key("old")))
                .values(created_at=now_utc() - timedelta(hours=2))
            )

        assert cache.expiry.run_once() == 1
        assert cache.get("old") is None
        assert cache.get("new") == 2
    finally:
        cache.close()


def test_no_eviction_when_under_limits(db_url: str) -> None:
    cache = Cache(
        database_url=db_url, max_entries=100, max_size=None, max_age=None, auto_expire=False
    )
    try:
        cache.set("a", 1)
        assert cache.expiry.run_once() == 0
    finally:
        cache.close()


def test_write_after_close_does_not_raise(db_url) -> None:
    """C-3: Expiry.shutdown() used shutdown(wait=False) with no closed-guard, so a straggler
    set() after close() raised 'cannot schedule new futures after shutdown' from
    maybe_trigger, and a queued eviction could run against the disposed engine."""
    cache = Cache(database_url=db_url, auto_expire=True)
    cache.set("a", 1)
    cache.close()
    # The write itself succeeds (the engine pool is rebuilt); the expiry trigger must be a
    # silent no-op instead of an error.
    cache.set("b", 2)


def test_estimate_survives_id_holes_without_collapsing(db_url) -> None:
    """C-4: the sample window used to be sized by id *span*; after churn (deletes leave
    holes) the window caught ~no rows and the estimator fell back to min_outlier * count —
    a systematic worst-case overestimate driving needless eviction."""
    from sqlalchemy import insert

    from firm._core.clock import now_utc
    from firm.cache.estimate import estimate_size
    from firm.cache.keys import key_hash

    cache = Cache(database_url=db_url, auto_expire=False)
    try:
        with cache.engine.begin() as conn:
            for i in range(30):
                key = f"k{i}".encode()
                # Huge id gaps simulate a heavily churned table (span >> count).
                conn.execute(
                    insert(schema.entries).values(
                        id=1 + i * 1_000_000,
                        key=key,
                        value=b"x",
                        key_hash=key_hash(key),
                        byte_size=10_000 if i < 10 else 100,
                        created_at=now_utc(),
                    )
                )
        true_total = 10 * 10_000 + 20 * 100  # 102_000
        with cache.engine.connect() as conn:
            estimate = estimate_size(conn, samples=10)
        # Span-based sizing produced ~300_000 here (outliers + min_outlier * the rest).
        # The count-based window catches ~half the non-outliers, so the estimate lands on
        # the true total (fallback probability ~1e-6).
        assert estimate < 2 * true_total, f"estimate collapsed to worst case: {estimate}"
    finally:
        cache.close()


def test_background_eviction_failure_reaches_on_error(db_url, monkeypatch) -> None:
    """X-1: eviction errors were swallowed by contextlib.suppress — a cache that silently
    stops evicting is a full-disk incident. They now route to Cache(on_error=...)."""
    import time

    seen: list[BaseException] = []
    cache = Cache(database_url=db_url, auto_expire=True, on_error=seen.append)
    try:
        monkeypatch.setattr(
            cache.expiry, "run_once", lambda: (_ for _ in ()).throw(RuntimeError("evict-fail"))
        )
        cache.expiry.maybe_trigger(10_000)  # expected runs >> 1, so at least one submits
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not seen:
            time.sleep(0.01)
        assert seen and "evict-fail" in str(seen[0])
    finally:
        cache.close()


def test_concurrent_writes_and_eviction_stay_consistent(db_url: str) -> None:
    """TL-1: writers inserting while an evictor deletes over-limit rows must not corrupt the store
    or wedge — the insert-vs-delete race around FIFO eviction. PG/MySQL run the threads genuinely
    concurrently; SQLite is single-writer, so lock contention is expected and retried here exactly
    as a real multi-threaded SQLite app must. Any *non-lock* error is a genuine failure."""
    import threading
    import time

    from sqlalchemy.exc import OperationalError

    def _retry_on_lock(op: Callable[[], object]) -> None:
        # A deferred write that loses the single-writer race returns "database is locked"
        # immediately (SQLite skips the busy handler to avoid deadlock), so retries are cheap.
        for _ in range(500):
            try:
                op()
                return
            except OperationalError as exc:
                if "lock" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                time.sleep(0.005)
        raise AssertionError("still lock-contended after 500 retries — likely a real wedge")

    cache = Cache(
        database_url=db_url,
        max_entries=20,
        max_size=None,
        max_age=None,
        expiry_batch_size=10,
        auto_expire=False,
    )
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer(worker: int) -> None:
        try:
            for i in range(100):
                _retry_on_lock(lambda i=i, worker=worker: cache.set(f"w{worker}-{i}", i))
        except Exception as exc:  # record so the assertion can report the actual failure
            errors.append(exc)

    def evictor() -> None:
        try:
            while not stop.is_set():
                _retry_on_lock(cache.expiry.run_once)
        except Exception as exc:
            errors.append(exc)

    try:
        writers = [threading.Thread(target=writer, args=(w,)) for w in range(3)]
        evictor_thread = threading.Thread(target=evictor)
        evictor_thread.start()
        for t in writers:
            t.start()
        for t in writers:
            t.join()
        stop.set()
        evictor_thread.join()
        # Writes have stopped; settle to the configured limit.
        while cache.expiry.run_once() > 0:
            pass

        assert not errors, f"concurrent write/evict raised: {errors[:3]}"
        with transaction(cache.engine) as conn:
            assert entry_count(conn) <= 20
        # The store is still fully usable after the storm.
        cache.set("after", 1)
        assert cache.get("after") == 1
    finally:
        cache.close()
