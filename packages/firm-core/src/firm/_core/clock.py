"""The single time source for the package.

Everything that writes a database timestamp (``scheduled_at``, ``expires_at``,
``finished_at``, heartbeats) or compares against ``now`` goes through :func:`now_utc`, so
there is exactly one definition of "now". We store timezone-naive UTC everywhere, so a
comparison never mixes aware and naive datetimes. Sleeps elsewhere use ``monotonic``;
this module is only about wall-clock database time.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_utc() -> datetime:
    """Return the current time as a timezone-naive UTC ``datetime``."""
    return datetime.now(UTC).replace(tzinfo=None)
