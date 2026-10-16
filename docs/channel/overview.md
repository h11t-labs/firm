# firm-channel — overview

Database-backed publish/subscribe for Python. Broadcast messages and subscribe to channels out of
SQLite, PostgreSQL, or MySQL/MariaDB — no Redis, no message broker, no websocket server required.

## The model

A broadcast inserts a row into `firm_channel_messages`. Subscribers register an in-process callback
per channel; a background **listener** polls the table for rows newer than the last id it has seen
on those channels and hands each payload to the matching callbacks.

```
broadcast("room:42", payload) ──▶ INSERT firm_channel_messages (channel, payload, channel_hash, …)

listener (every polling_interval):
  SELECT … WHERE channel_hash IN (subscribed) AND id > last_id  ──▶ callback(payload)
```

One table, `firm_channel_messages`:

| Column | Purpose |
|---|---|
| `id` | Autoincrement — the delivery cursor (subscribers track the last id they've seen). |
| `channel` | The channel the message was broadcast to (BLOB ≤ 1024 bytes). |
| `payload` | The opaque message bytes (large BLOB / `LONGBLOB` on MySQL). |
| `channel_hash` | A signed 64-bit hash of the channel — the indexed column the listener filters on. |
| `created_at` | Insertion time — drives age-based trimming. |

## Delivery semantics

- **Per-process fan-out.** Every process running a `Channel` against the same database sees every
  broadcast; each tracks its own cursor, so there's no competition for messages (unlike the queue,
  where exactly one worker claims a job).
- **From-now-on.** A subscription only receives messages broadcast *after* it was created — the
  backlog already in the table is skipped.
- **Opaque payloads.** Payloads are bytes in, bytes out (a `str` is UTF-8 encoded on broadcast).
  Serialize structured data yourself (e.g. JSON) before broadcasting.

## Trimming

Old messages are deleted automatically. Each broadcast has a small (~2%) chance of triggering a
**trim** that removes a batch of messages older than `message_retention` (default 1 day), using
`FOR UPDATE SKIP LOCKED` so concurrent trimmers never fight. You can also trim on demand
(`channel.trim()` or the [CLI](cli.md)).

## Read on

- **[Getting started](getting-started.md)** — install, broadcast, subscribe.
- **[Configuration](configuration.md)** — every `Channel(...)` option.
- **[CLI](cli.md)** — `firm-channel stats|trim`.
- **[Internals](internals.md)** — the listener, channel hashing, and trimming in detail.
