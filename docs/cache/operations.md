# Operations

All methods are on a `Cache` instance. Keys are `str` or `bytes`; values are any object your
[coder](encryption-and-coders.md) can serialize (pickle by default).

## Single key

```python
cache.set("k", value)          # store (last write wins)
cache.get("k")                 # -> value, or None if absent
cache.delete("k")              # -> True if a row was removed, else False
cache.exist("k")               # -> True/False, without deserializing the value
```

## Read-through with `fetch`

`fetch` returns the cached value on a hit, or computes it, stores it, and returns it on a miss:

```python
value = cache.fetch("user:42", lambda: load_user(42))
# you can also pass a plain (non-callable) default:
value = cache.fetch("flag", False)
```

> **Note:** a stored `None` is treated as a miss (there is no "cached None" sentinel), so `fetch`
> will recompute keys whose value is `None`.

## Many keys at once

```python
cache.set_multi({"a": 1, "b": 2})
cache.get_multi(["a", "b", "c"])    # -> {"a": 1, "b": 2, "c": None}
```

`get_multi` is a single round-trip (one `WHERE key_hash IN (...)` query); missing keys come back as
`None`.

## Counters

```python
cache.increment("hits")        # -> 1   (creates the key at 0 first)
cache.increment("hits", 5)     # -> 6
cache.decrement("hits", 2)     # -> 4
```

`increment`/`decrement` are **atomic**: they run the read-modify-write under a serialized
transaction (`BEGIN IMMEDIATE` on SQLite, `SELECT … FOR UPDATE` on Postgres/MySQL), so concurrent
counters don't lose updates.

## Clear everything

```python
cache.clear()                  # delete every entry
```

## Keys

Keys are normalized to bytes. A key longer than `max_key_bytesize` (default 1024) is truncated and
given a hash suffix so it stays unique — so very long keys are fine, just don't rely on the stored
`key` bytes being the full original for huge keys. Lookups always go through the 64-bit `key_hash`,
with a byte-for-byte key comparison to guard against the (astronomically rare) hash collision.
