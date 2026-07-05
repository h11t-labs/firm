"""Key normalization and hashing.

``key_hash`` is ``SHA256(key)`` reinterpreted as a signed 64-bit integer (big-endian). Keys
longer than ``max_bytesize`` are truncated with a hash suffix so they stay
unique while fitting the column.
"""

from __future__ import annotations

import hashlib
import struct


def key_hash(key_bytes: bytes) -> int:
    return struct.unpack(">q", hashlib.sha256(key_bytes).digest()[:8])[0]


def normalize_key(key: str | bytes, max_bytesize: int = 1024) -> bytes:
    raw = key.encode("utf-8") if isinstance(key, str) else bytes(key)
    if len(raw) <= max_bytesize:
        return raw
    suffix = b":hash:" + hashlib.sha256(raw).hexdigest().encode("ascii")
    keep = max(max_bytesize - len(suffix), 0)
    return raw[:keep] + suffix
