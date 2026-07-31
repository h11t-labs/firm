# Roadmap

The six things we intend to build next, in order. The ordering is by demand and by what unblocks
what — not by size. Nothing here carries a version or a date, because nothing here is committed to
one; when something is, it ships and leaves this file.

This file is deliberately short. It is not the backlog and not the bug tracker:

- **Bugs.** A known correctness defect gets an issue and a fix, never a bullet here.
- **Backlog.** Smaller gaps, chores, and hardening live in
  [issues](https://github.com/h11t-labs/firm/issues).
- **Won't build.** Parity gaps we have decided against are recorded as divergences in
  [`docs/comparison-to-rails.md`](docs/comparison-to-rails.md), so a reader finds the answer where
  the question comes up.

An item describing a problem that no longer exists is a bug in this file.

---

## 1. Per-entry cache TTL

`expires_in` / `expires_at` on each `set`/`fetch`, the way ActiveSupport does it. firm's cache
expiry is global only today (`max_age` plus a `max_size` FIFO trim), and it is the one divergence
our own documentation already promises to close. Pack the expiry into the stored value envelope and
filter it out on read — no schema change, no migration.

`firm-cache`: `store.py`, `entries.py`, `serialization.py`.

## 2. Per-exception retry and discard

`retry_on(TimeoutError, attempts=5)` / `discard_on(ValueError)`, like Active Job. A job's retry
policy is uniform today — one `attempts` count and one backoff for every way it can fail — so a
transient timeout and a permanent `ValueError` are retried identically, and giving up early on the
second means catching it inside the job.

`firm-queue`: `job.py`, `results.py`.

## 3. Bulk enqueue

One multi-row insert for enqueuing many jobs at once instead of a transaction per job — the
`perform_all_later` equivalent. Small, self-contained, and the prerequisite for any throughput
number worth publishing.

`firm-queue`: `enqueue.py`.

## 4. Type-checked enqueue signatures

`ParamSpec`, so `my_job.enqueue(...)` is checked against the job's own parameters. Every module
ships `py.typed`; an untyped `enqueue(*args, **kwargs)` on the primary API is the largest remaining
hole in that promise.

`firm-queue`: `job.py`.

## 5. Metrics and tracing

A pluggable metrics interface — queue depth, claim latency, job duration, failures — with a
Prometheus exporter, plus optional OpenTelemetry spans around execution. `firm.queue.queries`
already supplies the numbers and the lifecycle hooks the events; what is missing is a
machine-readable surface over them, since the dashboard renders HTML only.

`firm-queue`: `worker.py`, `hooks.py`.

## 6. Postgres `LISTEN`/`NOTIFY`

`NOTIFY` on enqueue and `LISTEN` in the worker: near-zero pickup latency and no idle polling on
Postgres, the way Procrastinate and PgQueuer do it. Polling stays as the portable fallback for
SQLite and MySQL. The biggest latency win available and the most invasive change on this list —
last because it should be measured rather than assumed, and the benchmark harness that would prove
it is itself still backlog.

`firm-queue`: `worker.py`, `enqueue.py`, and the dialect seam.
