"""Channel hashing specs — signed big-endian SHA-256 truncation to a 64-bit integer."""

from __future__ import annotations

import hashlib
import struct

from firm.channel.keys import channel_hash, normalize_channel


def test_channel_hash_is_signed_be64_of_sha256() -> None:
    channel = b"room:42"
    expected = struct.unpack(">q", hashlib.sha256(channel).digest()[:8])[0]
    assert channel_hash(channel) == expected
    # Pin the wire value so a "wrong but self-consistent" formula can't pass (e.g. unsigned `>Q`,
    # little-endian, or the wrong digest slice would all change this number).
    assert channel_hash(b"room:42") == 4058071420173215221


def test_channel_hash_is_deterministic() -> None:
    assert channel_hash(b"a") == channel_hash(b"a")
    assert channel_hash(b"a") != channel_hash(b"b")


def test_channel_hash_can_be_negative() -> None:
    # Signed 64-bit means about half of all channels hash to a negative value; make sure the
    # column/round-trip tolerates it (this is why the column is a signed BigInteger).
    samples = [channel_hash(f"c{i}".encode()) for i in range(200)]
    assert any(h < 0 for h in samples)
    assert any(h > 0 for h in samples)


def test_normalize_channel_encodes_str() -> None:
    assert normalize_channel("x") == b"x"
    assert normalize_channel(b"x") == b"x"
