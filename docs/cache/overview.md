# firm-cache — overview

A database-backed cache store for Python. Keep a large, durable cache in SQLite, PostgreSQL, or
MySQL/MariaDB, with no Redis or Memcached.

## Why cache in the database?

Modern SSDs are fast enough that a database-backed cache can be **much larger** than an in-memory
one — you're bounded by disk, not RAM. You also get durability (the cache survives restarts),
encryption at rest, and one fewer service to run. The trade-off is higher per-op latency than Redis;
for most read-through caches that's a fine deal.

## The model

One table, `firm_entries`:

| Column | Purpose |
|---|---|
| `id` | Autoincrement — also the FIFO eviction order (oldest = smallest id). |
| `key` | The raw key bytes (truncated past `max_key_bytesize`). |
| `value` | The serialized (optionally encrypted) value. |
| `key_hash` | A signed 64-bit hash of the key — the **unique lookup** column. |
| `byte_size` | Estimated row size, used by the eviction estimator. |
| `created_at` | Insertion time, used for age-based expiry. |

Lookups go through `key_hash` (so there's no index on the up-to-1 KiB key), and writes are a single
atomic upsert.

## What it does

- `get` / `set` / `fetch` / `delete` / `exist`, plus `get_multi` / `set_multi`, `increment` /
  `decrement`, and `clear` — see [Operations](operations.md).
- **Eviction** by age (`max_age`), total size (`max_size`), or entry count (`max_entries`), FIFO
  (oldest first) — see [Eviction & expiry](eviction.md).
- **Pluggable serialization** (pickle / JSON / your own) and optional **at-rest encryption** —
  see [Encryption & coders](encryption-and-coders.md).

```python
from firm.cache import Cache

cache = Cache(database_url="postgresql://localhost/myapp")
cache.set("user:42", {"name": "Ada"})
cache.fetch("homepage", lambda: render_homepage())   # read-through
```

Read on: **[Getting started](getting-started.md)**.
