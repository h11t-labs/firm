"""Channel normalization and hashing.

``channel_hash`` is ``SHA256(channel)`` reinterpreted as a signed 64-bit integer (big-endian) — the
indexed column the listener filters on, so a subscription lookup never needs an index on the raw
channel bytes. Channels longer than ``max_bytesize`` are truncated with a hash suffix so they stay
unique while fitting the ``channel`` column (MySQL ``VARBINARY(1024)`` rejects longer values).
"""

from __future__ import annotations

import hashlib
import struct


def channel_hash(channel_bytes: bytes) -> int:
    return struct.unpack(">q", hashlib.sha256(channel_bytes).digest()[:8])[0]


def normalize_channel(channel: str | bytes, max_bytesize: int = 1024) -> bytes:
    raw = channel.encode("utf-8") if isinstance(channel, str) else bytes(channel)
    if len(raw) <= max_bytesize:
        return raw
    suffix = b":hash:" + hashlib.sha256(raw).hexdigest().encode("ascii")
    keep = max(max_bytesize - len(suffix), 0)
    return raw[:keep] + suffix
