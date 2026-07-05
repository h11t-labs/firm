# Internals

## Schema

One table, `firm_messages`:

```
id            autoincrement PK, the delivery cursor
channel       BLOB (≤ 1024 bytes), the channel name (VARBINARY(1024) on MySQL so it can be indexed)
payload       large BLOB (LONGBLOB on MySQL), the opaque message bytes
channel_hash  signed 64-bit hash of the channel — the listener's filter column
created_at    insertion time (DATETIME(6) on MySQL), drives trimming
```

Three indexes: on `channel`, on `channel_hash`, and on `created_at`.

## channel_hash

`channel_hash = SHA256(channel)[:8]` reinterpreted as a **signed** big-endian 64-bit integer — the
same scheme the cache uses for `key_hash`. Subscriptions and the listener query filter on this
indexed integer instead of the raw (up to 1 KiB) channel bytes. It's signed because Postgres and
SQLite have no unsigned 64-bit integer, so about half of all channels hash to a negative value (the
column is a signed `BigInteger` to match).

A 64-bit hash can in principle collide, so the listener filters candidate rows by `channel_hash` but
dispatches strictly by the exact `channel` — a colliding-but-different channel is fetched and then
skipped.

## The listener

`subscribe` starts one background **listener** thread (an `InterruptiblePoller`) the first time it's
called. Each cycle, every `polling_interval` seconds, it:

1. snapshots the subscribed channels and the global cursor `last_id`;
2. runs `SELECT id, channel, payload WHERE channel_hash IN (subscribed) AND id > last_id ORDER BY id`;
3. for each row, advances `last_id`, and — guarding against re-delivery with a **per-channel**
   cursor — calls every callback registered for that exact channel.

Two cursors are tracked: a **global** `last_id` (so the next query only fetches genuinely new rows)
and a **per-channel** last id (so a channel subscribed later never receives messages that predate
its subscription). A subscriber error is swallowed so it can't break the listener or the others.

The listener thread sleeps on a `threading.Event`, so `close()` interrupts it immediately rather
than waiting out the poll interval.

## Trimming

`firm_messages` is an ephemeral buffer, not a log, so old rows are deleted. Each broadcast has
a ~`2 / trim_batch_size` chance (≈ 2% at the default batch of 100) of submitting a **trim** to a
single background thread — the same probabilistic trigger the cache uses for eviction. A trim is:

```sql
SELECT id FROM firm_messages WHERE created_at < :cutoff
  FOR UPDATE SKIP LOCKED LIMIT :batch_size;   -- SKIP LOCKED on PG/MySQL; BEGIN IMMEDIATE on SQLite
DELETE FROM firm_messages WHERE id IN (...);
```

`SKIP LOCKED` (via the shared dialect seam) means several processes can trim concurrently without
deleting the same rows or blocking each other. Set `autotrim=False` to trim only via `trim()` / the
CLI / a cron.
