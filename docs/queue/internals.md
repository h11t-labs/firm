# Internals

For contributors and the curious. The public API is small; most of the interesting work is in
keeping claims correct across three databases.

## The claim path

A worker claims jobs in one short transaction:

```sql
SELECT id, job_id FROM ready_executions
  WHERE queue_name = ? ORDER BY priority, job_id LIMIT <idle_threads>;   -- locked, see below
INSERT INTO claimed_executions (job_id, process_id) VALUES ...;
DELETE FROM ready_executions WHERE id IN (...);
```

Two workers must never select the same row. How that's guaranteed depends on the database, and is
the **only** thing the dialect layer abstracts:

| Database | Transaction | Row locking |
|---|---|---|
| SQLite | `BEGIN IMMEDIATE` (takes the single write lock up front) | none needed — writers are serialized |
| PostgreSQL | ordinary transaction | `SELECT … FOR UPDATE SKIP LOCKED` |
| MySQL / MariaDB | ordinary transaction | `SELECT … FOR UPDATE SKIP LOCKED` |

The seam is `_core/dialects/Dialect`:

```python
class Dialect(ABC):
    def begin_claim_tx(self, engine) -> ContextManager[Connection]: ...   # IMMEDIATE vs plain
    def with_skip_locked(self, stmt: Select) -> Select: ...               # SKIP LOCKED vs no-op
```

Every "select rows I'm about to mutate" path uses **both**: it runs inside `begin_claim_tx` and
wraps its `SELECT` in `with_skip_locked`. That covers the worker claim, the dispatcher's
scheduled→ready promotion, the semaphore's blocked-job promotion, the maintenance pass, and crash
recovery. On SQLite `with_skip_locked` is a no-op (the immediate write lock already serializes);
on PG/MySQL it skips rows another worker holds.

> This is why concurrency works on SQLite here: we serialize on the write lock instead of relying
> on row-level locking (which SQLite doesn't have).

## Queue selection

`"*"` polls every non-paused queue in a single global `(priority, job_id)` order. A list of exact
names / `prefix*` patterns polls the matched queues in order, each by `(priority, job_id)`. Paused
queues (rows in `firm_pauses`) are always excluded.

## Semaphores

`acquire` is an atomic `UPDATE … SET value = value - 1 WHERE key = ? AND value > 0` (the decrement is
row-safe on its own), falling back to an `INSERT … ON CONFLICT/SAVEPOINT` to create the row. `release`
is the symmetric increment capped at the limit. `promote_one` selects the next blocked job for a key
**`FOR UPDATE SKIP LOCKED`** so two concurrent releases can't promote the same job twice.

## Dispatcher & maintenance

- `dispatch_once` promotes due `scheduled_executions` (batched, priority order), applying concurrency
  rules as it goes.
- `run_maintenance` (the failsafe) deletes expired semaphores and then, for **every** blocked key
  with free capacity — not just expired ones — promotes one job. This closes the small window where
  a release could interleave with a dispatch and leave a job blocked while a slot is free.

## The poller primitive

Worker, dispatcher, scheduler, maintenance, and heartbeat are all `InterruptiblePoller`s: a thread
that runs `poll()` then sleeps on a `threading.Event` so it can be woken or stopped instantly. The
loop catches exceptions and routes them to the lifecycle error hooks, so one bad cycle never kills
the thread.

## The supervisor

`ForkSupervisor` forks a child per role **before** starting any threads (so the parent stays
single-threaded across `fork`), and each child calls `runtime.reset()` first to drop the engine/pool
it inherited. Children install minimal signal handlers that just flip a stop event; only the
supervisor reaps and restarts. `ThreadSupervisor` runs the same pollers as threads in one process.
