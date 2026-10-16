# Internals

## Schema

One table, `firm_cache_entries`:

```
id          autoincrement PK, FIFO eviction order
key         BLOB (≤ 1024 bytes), the raw key
value       large BLOB (LONGBLOB on MySQL), the serialized/encrypted value
key_hash    BIGINT, UNIQUE — the lookup column
byte_size   INT — estimated row size for the size estimator
created_at  timestamp — for age-based eviction
```

Indexes: unique on `key_hash`, plus `(key_hash, byte_size)` and `byte_size` to support the estimator.

## Key hashing

`key_hash = signed_int64(SHA256(key)[:8])` (big-endian). Lookups and the unique constraint use
`key_hash`, so there's no index on the up-to-1 KiB `key` column. Reads compare the stored `key`
bytes to guard against the (vanishingly unlikely) 64-bit hash collision. Keys longer than
`max_key_bytesize` are truncated with a `:hash:<hex>` suffix.

## Writes are atomic upserts

`write_entry` is a single dialect-native upsert keyed on `key_hash`, so concurrent writers to the
same key never collide on the unique index:

| Database | Statement |
|---|---|
| PostgreSQL | `INSERT … ON CONFLICT (key_hash) DO UPDATE …` |
| MySQL / MariaDB | `INSERT … ON DUPLICATE KEY UPDATE …` |
| SQLite | `INSERT … ON CONFLICT (key_hash) DO UPDATE …` |

`increment` needs a read-modify-write, so it runs inside a serialized transaction (`BEGIN IMMEDIATE`
on SQLite; `ensure_entry` to materialize the row, then `SELECT … FOR UPDATE` on Postgres/MySQL)
before upserting the new value.

## byte_size

`byte_size = len(key) + len(value) + 140` (plus 170 if encrypted) — a fixed per-row overhead
estimate. It's bookkeeping for the eviction estimator only; it never gates a write.

## The size estimator

`estimate_size` avoids scanning the whole table. When the row count is `≤ size_estimate_samples` it
returns an exact `SUM(byte_size)`. Above that it sums the `samples` largest rows exactly (outliers),
then samples a random `key_hash` window proportional to the table's id-span (since `key_hash` is
uniform) and scales the sampled sum up, adding the outliers back. Constant cost, good accuracy.

## Portability types

`value` is `LargeBinary().with_variant(LONGBLOB, "mysql")` (plain `BLOB` caps at 64 KiB on MySQL),
and `created_at` is `DATETIME(6)` on MySQL for sub-second precision. The id PK uses the `Integer`
variant on SQLite so it maps to `INTEGER PRIMARY KEY` (rowid) and autoincrements.
