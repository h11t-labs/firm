# firm-queue — overview

Database-backed background jobs for Python. Run jobs out of SQLite, PostgreSQL, or MySQL/MariaDB
with no Redis or other broker.

## The model

A job is a registered function. Enqueuing it inserts a row into `firm_queue_jobs` (the source of
truth) plus one *execution* row that tracks where the job is in its lifecycle. The execution moves
between tables:

```
jobs (source of truth)
  ├─ scheduled_executions   future jobs  ──dispatcher──▶ ready (or blocked)
  ├─ ready_executions       claimable now ──worker────▶ claimed
  ├─ blocked_executions     waiting on a concurrency limit
  ├─ claimed_executions     running ───────────────────▶ finished (jobs.finished_at)
  └─ failed_executions      raised, retries exhausted        │  or rescheduled for a retry
```

Eleven tables in total, all namespaced `firm_*`. The supporting tables are `semaphores`
(concurrency), `pauses` (paused queues), `processes` (running workers/dispatchers + heartbeats),
and `recurring_tasks` / `recurring_executions`
(cron schedules + dedupe).

## The four roles

| Role | What it does |
|---|---|
| **Worker** | Polls `ready_executions`, claims a batch, runs each job on a thread pool. |
| **Dispatcher** | Moves due `scheduled_executions` to ready; runs concurrency maintenance. |
| **Scheduler** | Enqueues recurring tasks on their cron schedule. |
| **Supervisor** | Starts and supervises the others — as threads or as forked processes — handles signals, heartbeats, and crash recovery. |

You can run all of them with one command (`firm-queue start`), embed them in your own process
([`ThreadSupervisor`](workers-and-supervisor.md)), or drive individual steps yourself
(`worker.run_ready`, `dispatcher.dispatch_once`).

## How a claim stays correct

Two workers must never grab the same job. On PostgreSQL/MySQL that's `SELECT … FOR UPDATE SKIP
LOCKED`; on SQLite (which has no row locking) it's a `BEGIN IMMEDIATE` write lock. The choice is
hidden behind a small dialect seam, so the rest of the code is database-agnostic. See
[Internals](internals.md) and [Database backends](../database-backends.md).

## Read on

- **[Getting started](getting-started.md)** — install, configure, run your first job.
- **[Defining jobs](jobs.md)** — the `@job` decorator and enqueue API.
- **[Concurrency controls](concurrency.md)**, **[Recurring tasks](recurring.md)**,
  **[Retries & failures](retries-and-failures.md)**.
- **[Workers & the supervisor](workers-and-supervisor.md)** — how to actually run it in production.
