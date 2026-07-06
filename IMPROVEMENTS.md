# Improvements & roadmap

A prioritized, honest list of what would make `firm-queue` / `firm-cache` better. Each
item notes rough effort (**S**mall / **M**edium / **L**arge) and where it lives. Nothing here is a
known correctness bug in the current code — the test suite passes on SQLite, PostgreSQL, and
MySQL/MariaDB — these are hardening, performance, features, and polish.

## 1. Correctness & robustness

- **(M) Lock the semaphore row during acquire/release.** Today `acquire` uses an atomic decrement
  and `release` an atomic increment, which are individually safe, but the acquire-returns-full vs.
  concurrent-release interleaving can briefly park a job in `blocked` while a slot is actually free.
  It self-heals (the next release promotes it, and `run_maintenance` now promotes any blocked key
  with capacity), but a `SELECT … FOR UPDATE` on the semaphore row would make acquire/release
  strictly serialized per key. `semaphore.py`.
- **(S) Scope recovery's row lock to `claimed_executions`.** `recover_orphaned_claims` selects a join
  of claimed+jobs `FOR UPDATE SKIP LOCKED`; on Postgres that also locks the joined `jobs` rows. Using
  `with_for_update(of=claimed_executions, skip_locked=True)` would avoid over-locking. Correct today
  (the jobs locked are disjoint from the dispatcher's), just tidier. `recovery.py`, dialect seam.
- **(M) First-write race for `cache.increment` on a brand-new key.** Concurrent first increments of a
  key that doesn't exist yet can still race on PG/MySQL (there's no row to `FOR UPDATE` until one is
  created). An advisory lock keyed by `key_hash`, or an integer-only fast path that does the
  arithmetic in SQL, would close it. Existing keys are already safe. `store.py`, `entries.py`.
- **(S) Poison-job / dead-letter handling.** A job that always raises burns through its retries and
  lands in `failed_executions` — good — but there's no explicit dead-letter queue or alerting hook
  beyond the lifecycle error hook. A small "on final failure" hook + a queryable failed view would
  help. `results.py`, `hooks.py`.
- **(M) Verify the spawn supervisor end-to-end on Windows.** Fork is POSIX-only; the `spawn` fallback
  exists but the fork integration tests are SQLite/POSIX-only. A Windows CI job exercising
  `ThreadSupervisor` + spawn would lock in cross-platform support. `supervisor.py`, CI.

## 2. Performance & scale

- **(L) Postgres `LISTEN/NOTIFY` to wake workers instantly.** Workers currently poll (0.1s when
  busy). A `NOTIFY` on enqueue + `LISTEN` in the worker would cut latency to near-zero and reduce
  idle polling, the way Procrastinate/PgQueuer do. Keep polling as the portable fallback. `worker.py`,
  `enqueue.py`, dialect seam.
- **(S/M) Bulk enqueue (`perform_all_later` equivalent).** A single multi-row insert for enqueuing
  many jobs at once, instead of one transaction per job. `enqueue.py`.
- **(S) Tune/expose claim batch sizing.** The worker claims `threads` at a time; surfacing this and
  documenting sizing for high-throughput deployments would help operators. (Pool sizing is now
  exposed via `configure(pool_size=…, max_overflow=…)` / `engine=`.) `worker.py`.
- **(S) SQLite production hardening.** Periodic WAL checkpointing and an optional `VACUUM`/auto-vacuum
  guidance to keep the file from growing unbounded under churn. `_core/database.py`, docs.
- **(M) Cache value compression.** Optional zstd/gzip on the value coder to shrink large entries (and
  fit more under `max_size`). `serialization.py`.

## 3. Feature parity with the Solid stack — and beyond

- **(L) `firm-cable`.** The third Solid component (database-backed Action Cable pub/sub). A
  `solid_cable_messages` table + a poll/broadcast API + an async websocket adapter would complete the
  trifecta. New package.
- **(L) Cache sharding / consistent hashing.** solid_cache spreads entries across multiple databases
  via Maglev hashing. The `Cache` already isolates connection handling; a `Connections` seam +
  key→shard routing would enable it. `store.py`, `entries.py`.
- **(S) `expiry_method="job"` for the cache.** Run cache eviction as a `firm-queue` job instead
  of a background thread — a nice cross-package integration once both are installed. `expiry.py`.
- **(M) Per-entry cache TTL (`expires_in` / `expires_at`).** firm's cache expiry is currently
  **global only** (`max_age` + `max_size` FIFO trim); ActiveSupport lets each `set`/`fetch` carry its
  own TTL. Implement Rails-style by packing the expiry into the stored entry (the value envelope) and
  filtering it out on read — no schema change. Until then, per-entry TTL is a documented divergence
  (see `docs/comparison-to-rails.md`). `store.py`, `entries.py`, `serialization.py`.
- **(M) Richer retry semantics.** Per-exception `retry_on` / `discard_on` (retry `TimeoutError` 5×,
  discard `ValueError`), like Active Job. Today retries are uniform. `job.py`, `results.py`.
- **(M) Job middleware / callbacks.** `before/after/around` hooks per job (or globally) and a
  `rescue_from`-style handler, for tracing, DB-session management, etc. `job.py`, `results.py`.
- **(M) Fugit-style schedules.** Natural-language (`"every 5 minutes"`) and timezone-aware recurring
  schedules, beyond plain cron. `scheduler.py`.
- **(M) `enqueue_after_commit` helper.** Tie an enqueue to the caller's SQLAlchemy session/transaction
  so a job is only enqueued if the surrounding work commits (avoids "job runs before its row
  exists"). `enqueue.py`.
- **(M) Unique jobs / idempotency keys.** Optionally dedupe enqueues of the same logical job within a
  window. `enqueue.py`, schema.
- **(L) Continuations (resumable jobs).** solid_queue's `ActiveJob::Continuable` — checkpoint within a
  job and resume after interruption. `results.py`, schema.

## 4. Developer experience

- **(M) Type-checked enqueue signatures.** Use `ParamSpec` so `my_job.enqueue(...)` is type-checked
  against the job's parameters (today `enqueue(*args, **kwargs)` is untyped). `job.py`.
- **(S) A first-class settings/config file.** Load workers/dispatchers/queues/recurring from
  `pyproject.toml` or a YAML file (solid_queue's `config/queue.yml`), instead of building
  `SupervisorConfig` in code. `cli.py`, `queue/config.py`.

## 5. Observability & operations

- **(M) Metrics & tracing.** A pluggable metrics interface (queue depth, claim latency, job
  duration, failures) with a Prometheus exporter, plus optional OpenTelemetry spans around job
  execution. Hooks exist; an events/metrics layer on top would round it out. `worker.py`, `hooks.py`.
- **(S) Structured logging** with consistent fields (job_id, queue, class_name, attempt) and a
  `silence_polling`-style toggle.
- **(M) Admin/inspection API.** Programmatic list/inspect/retry/discard for jobs and a queue-stats
  endpoint (partly covered by `queues` + `maintenance`); expand into a small ops module.
- **(S) Worker health/readiness probe** for container orchestration (is the supervisor up, are
  heartbeats fresh).

## 6. Testing & quality

- **(S) Concurrency stress tests on PG/MySQL.** A high-parallelism no-double-claim / no-lost-counter
  test against live Postgres/MySQL (beyond the 2-thread SQLite test) to harden the locking paths.
- **(S) CI matrix.** GitHub Actions across Python 3.11–3.14 (incl. a free-threaded job) and
  SQLite/Postgres/MySQL service containers; run ruff + ty + pytest.
- **(M) Benchmarks.** Throughput/latency numbers per backend, and vs. Procrastinate/PgQueuer, to
  guide tuning and set expectations.
- **(S) Property-based tests** (Hypothesis) for serialization round-trips and the size estimator.

## 7. Packaging & docs

- **(S) Publish to PyPI** with a changelog and semantic versioning.
- **(S) Build & publish the docs site** (the `zensical.toml` config is ready) to GitHub Pages.
- **(S) Docker-compose for Postgres/MySQL** to make the live-backend tests easy to run locally.

---

### Quick wins (do these first)

1. Scope recovery's lock to `claimed_executions` (S, robustness).
2. Bulk enqueue (S/M, perf).
3. Per-exception retry/discard (M, the most-requested parity gap).
4. CI matrix + PyPI publish (S, makes it usable by others).
5. Postgres `LISTEN/NOTIFY` (L, the biggest latency win).
