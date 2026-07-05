"""Channel normalization and hashing.

``channel_hash`` is ``SHA256(channel)`` reinterpreted as a signed 64-bit integer (big-endian) — the
indexed column the listener filters on, so a subscription lookup never needs an index on the raw
channel bytes.
"""

from __future__ import annotations

import hashlib
import struct


def channel_hash(channel_bytes: bytes) -> int:
    return struct.unpack(">q", hashlib.sha256(channel_bytes).digest()[:8])[0]


def normalize_channel(channel: str | bytes) -> bytes:
    return channel.encode("utf-8") if isinstance(channel, str) else bytes(channel)
