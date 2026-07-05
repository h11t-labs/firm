"""Estimating total cache size without scanning every row.

When the table is small (``count <= samples``) we just sum ``byte_size`` exactly. Above that we
sum the ``samples`` largest rows exactly (the "outliers"), then sample a random ``key_hash``
window — ``key_hash`` is uniformly distributed — for the rest and scale it up.
"""

from __future__ import annotations

import random

from sqlalchemy import Connection, func, select

from . import schema

_entries = schema.entries

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def entry_count(conn: Connection) -> int:
    return conn.execute(select(func.count()).select_from(_entries)).scalar() or 0


def id_span(conn: Connection) -> int:
    row = conn.execute(select(func.min(_entries.c.id), func.max(_entries.c.id))).first()
    if row is None or row[0] is None:
        return 0
    return int(row[1]) - int(row[0]) + 1


def _exact_size(conn: Connection) -> int:
    return int(conn.execute(select(func.coalesce(func.sum(_entries.c.byte_size), 0))).scalar() or 0)


def estimate_size(conn: Connection, samples: int = 10000) -> int:
    count = entry_count(conn)
    if count == 0:
        return 0
    if count <= samples:
        return _exact_size(conn)

    span = id_span(conn)
    sampled_fraction = min(samples / (span - samples), 1.0) if span > samples else 1.0
    if sampled_fraction >= 1.0:
        # The id span is small enough that sampling buys nothing — sum exactly.
        return _exact_size(conn)

    outliers = conn.execute(
        select(_entries.c.byte_size).order_by(_entries.c.byte_size.desc()).limit(samples)
    ).all()
    outlier_sum = sum(int(r[0]) for r in outliers)
    min_outlier = int(outliers[-1][0])
    non_outlier_count = count - len(outliers)

    # Estimate the *average* non-outlier size from a random key_hash window (key_hash is
    # uniformly distributed) and scale by the exact non-outlier count. Averaging — rather than
    # extrapolating a sampled sum — keeps the estimate stable; ``<=`` (not ``<``) keeps it from
    # collapsing to zero when rows share a byte size; and an empty window falls back to
    # ``min_outlier`` (the most a non-outlier can weigh), so the estimate is never too low.
    width = int((_INT64_MAX - _INT64_MIN) * sampled_fraction)
    start = random.randint(_INT64_MIN, _INT64_MAX - width)
    sample = conn.execute(
        select(func.coalesce(func.sum(_entries.c.byte_size), 0), func.count()).where(
            _entries.c.key_hash.between(start, start + width),
            _entries.c.byte_size <= min_outlier,
        )
    ).one()
    sample_sum, sample_n = int(sample[0]), int(sample[1])
    avg_non_outlier = sample_sum / sample_n if sample_n else min_outlier
    return int(outlier_sum + avg_non_outlier * non_outlier_count)
