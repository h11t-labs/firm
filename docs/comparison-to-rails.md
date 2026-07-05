# Comparison to Rails

firm is a faithful port of the Rails Solid stack — queue, cache, and pub/sub — not a
reinterpretation. Where it differs, it's deliberate and documented.

## What's the same

- **Schema.** Every column, index, and the execution-table lifecycle match the gems one-for-one.
  The tables carry firm's own names (`firm_*`).
- **Behavior.** The job lifecycle, the four process roles, concurrency semaphores with the
  `expires_at` failsafe, recurring `(task_key, run_at)` dedupe, FIFO cache eviction with sampling
  size estimation, the `key_hash` lookup, the probabilistic expiry trigger, and the pub/sub polling
  listener with per-channel cursors, `channel_hash` routing, and probabilistic message trimming —
  all match.
- **Locking.** `FOR UPDATE SKIP LOCKED` on PostgreSQL/MySQL, exactly as the gems (the queue's
  claim path, and the pub/sub trim).

## Deliberate divergences

| Area                | solid_queue / solid_cache / solid_cable | firm                                                                         |
|---------------------|-----------------------------------------|------------------------------------------------------------------------------------|
| Retry counting      | tracked by Active Job                   | a `jobs.attempts` column (we own retries)                                          |
| Crash recovery      | orphaned claims are marked **failed**   | orphaned claims are **re-readied** (at-least-once; another worker finishes them)   |
| SQLite concurrency  | row-level locking tests are skipped     | `BEGIN IMMEDIATE` gives the same guarantee, so concurrency is fully tested on SQLite |
| Recurring schedules | Fugit (cron **and** natural language)   | cron only (`croniter`)                                                            |
| Job arguments       | Active Job + GlobalID (pass records)    | JSON + datetime/date/Decimal/UUID (pass IDs)                                       |
| Pub/sub trimming    | inline `TrimJob` per broadcast          | async probabilistic trim on a background thread (+ manual `trim()` / CLI)          |
| Pub/sub delivery    | Action Cable adapter                    | a standalone `Channel` with `broadcast`/`subscribe`/`unsubscribe` (no Action Cable) |

The at-least-once recovery choice means **jobs should be idempotent** — see
[Retries & failures](queue/retries-and-failures.md).

## Beyond the port: firm-audit

`firm.audit` is **not** a port — there is no `solid_audit` gem in Rails. It's an original firm
module: an append-only, database-backed audit log that shares the ported modules' "you already
have a database" thesis, but isn't a reproduction of anything in Solid. See
[firm-audit overview](audit/overview.md).

## Not (yet) ported

firm stands alone — there's no Rails — so the Active Job / Action Cable ecosystems aren't
reproduced, and a few pieces are still future work:

- **Active Job integration** (callbacks, `perform_later`, GlobalID, middleware) — replaced by the
  standalone `@job` decorator.
- **Action Cable integration** (channels, connections, the websocket server) — `firm-channel`
  ports the database-backed pub/sub broker, not the websocket layer on top of it.
- **Active Support / Notifications instrumentation** — replaced by lifecycle hooks + your own
  logging.
- **Cache sharding** (multi-database consistent hashing) and the **Mission Control** dashboard.

See the [roadmap](#) — `IMPROVEMENTS.md` in the repo root — for the full list of planned work.
