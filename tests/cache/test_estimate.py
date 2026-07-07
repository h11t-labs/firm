"""Size-estimation specs."""

from __future__ import annotations

import random

from sqlalchemy import delete, func, insert, select

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.cache import Cache, schema
from firm.cache.entries import compute_byte_size
from firm.cache.estimate import entry_count, estimate_size
from firm.cache.keys import key_hash, normalize_key

_entries = schema.entries


def test_exact_size_for_small_table(cache: Cache) -> None:
    cache.set("a", "x")
    cache.set("b", "yy")
    with transaction(cache.engine) as conn:
        real = conn.execute(select(func.sum(schema.entries.c.byte_size))).scalar()
        estimate = estimate_size(conn, samples=10000)
    assert estimate == real
    assert estimate > 0


def test_entry_count(cache: Cache) -> None:
    cache.set("a", 1)
    cache.set("b", 2)
    with transaction(cache.engine) as conn:
        assert entry_count(conn) == 2


def test_empty_cache_estimates_zero(cache: Cache) -> None:
    with transaction(cache.engine) as conn:
        assert entry_count(conn) == 0
        assert estimate_size(conn) == 0


# Sampling-branch specs ported from solid_cache's estimate_test.rb: drive the sampled estimator
# with small size_estimate_samples over many rows.


def _insert_uniform_entries(cache: Cache, count: int, value_len: int = 50) -> int:
    """Insert ``count`` rows of identical byte size directly, returning the per-row byte_size."""
    value = b"v" * value_len
    per_row = None
    with transaction(cache.engine) as conn:
        for i in range(count):
            kb = normalize_key(f"sample-key-{i:06d}")
            bs = compute_byte_size(kb, value, encrypted=False)
            per_row = bs
            conn.execute(
                insert(_entries).values(
                    key=kb,
                    value=value,
                    key_hash=key_hash(kb),
                    byte_size=bs,
                    created_at=now_utc(),
                )
            )
    assert per_row is not None
    return per_row


def _insert_varied_entries(cache: Cache, count: int, seed: int) -> int:
    """Insert ``count`` rows of *varied* byte size, returning the exact true total byte_size.

    Varied sizes are what the sampling branch is designed for: the algorithm sums the top
    ``samples`` rows exactly and statistically samples the strictly-smaller remainder.
    """
    rng = random.Random(seed)
    true_total = 0
    with transaction(cache.engine) as conn:
        for i in range(count):
            kb = normalize_key(f"varied-key-{i:06d}")
            value = b"v" * rng.randint(10, 500)
            bs = compute_byte_size(kb, value, encrypted=False)
            true_total += bs
            conn.execute(
                insert(_entries).values(
                    key=kb,
                    value=value,
                    key_hash=key_hash(kb),
                    byte_size=bs,
                    created_at=now_utc(),
                )
            )
    return true_total


def test_larger_sample_estimates_never_underestimate_uniform(db_url: str) -> None:
    """Upstream: estimate_test.rb "larger sample estimates" + "overestimate when all samples same".

    With every row the same byte size the estimator must land in a sane band and must never
    underestimate the true total (the ``<=`` non-outlier window keeps it from collapsing to just
    the outlier sum when all sizes are equal)."""
    cache = Cache(
        database_url=db_url,
        size_estimate_samples=10,
        max_size=None,
        max_age=None,
        auto_expire=False,
    )
    try:
        n = 200
        per_row = _insert_uniform_entries(cache, n)
        true_total = n * per_row

        with transaction(cache.engine) as conn:
            assert entry_count(conn) == n
            estimates = [estimate_size(conn, samples=10) for _ in range(25)]

        # The estimator must never come in low for a uniform distribution.
        for est in estimates:
            assert est >= true_total, f"estimate {est} underestimated true total {true_total}"
        # ...and it must stay within a sane band (not wildly inflated).
        assert max(estimates) <= true_total * 3
    finally:
        cache.close()


def test_sample_estimate_with_id_gaps_is_plausible(db_url: str) -> None:
    """Upstream: estimate_test.rb "gaps"/"more gaps" — punching id gaps must not break the sampled
    estimate. Uses varied sizes (the sampling branch's intended input) and deletes ~1/3 of the
    rows; the estimate must remain a plausible, same-order value of the true remainder."""
    cache = Cache(
        database_url=db_url,
        size_estimate_samples=10,
        max_size=None,
        max_age=None,
        auto_expire=False,
    )
    try:
        n = 200
        _insert_varied_entries(cache, n, seed=7)

        # Delete every 3rd row to punch id gaps.
        with transaction(cache.engine) as conn:
            all_ids = [int(r.id) for r in conn.execute(select(_entries.c.id))]
            to_delete = all_ids[::3]
            conn.execute(delete(_entries).where(_entries.c.id.in_(to_delete)))
            remaining = entry_count(conn)
            true_total = int(
                conn.execute(select(func.sum(_entries.c.byte_size))).scalar()  # exact remainder
            )

        assert remaining == n - len(to_delete)

        with transaction(cache.engine) as conn:
            estimates = [estimate_size(conn, samples=10) for _ in range(25)]

        # Same order of magnitude as the true remainder despite the gaps.
        for est in estimates:
            assert est > 0
            assert est <= true_total * 3
        avg = sum(estimates) / len(estimates)
        assert true_total * 0.5 <= avg <= true_total * 2.0
    finally:
        cache.close()
