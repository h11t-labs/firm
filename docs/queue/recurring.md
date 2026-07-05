# Recurring tasks

Run a job on a cron schedule — daily cleanups, hourly syncs, periodic reports.

```python
from firm.queue.scheduler import RecurringTask, Scheduler

@bq.job()
def cleanup() -> None:
    ...

tasks = [
    RecurringTask(key="nightly-cleanup", schedule="0 3 * * *", job=cleanup),
    RecurringTask(key="hourly-sync", schedule="0 * * * *", job=sync_job, args=(tenant_id,)),
]
```

## `RecurringTask`

| Field | Default | Meaning |
|---|---|---|
| `key` | — | Stable unique identifier for the task. |
| `schedule` | — | A 5-field cron expression (parsed by [`croniter`](https://github.com/kiorky/croniter)). |
| `job` | — | The `@job`-decorated function to enqueue. |
| `args` | `()` | Positional arguments passed to the job. |
| `kwargs` | `{}` | Keyword arguments passed to the job. |

> **Note:** schedule syntax is standard cron (`"*/5 * * * *"`, `"0 3 * * *"`, …). Natural-language
> forms (`"every 5 minutes"`, Fugit-style) are not supported.

## Running the scheduler

In production, hand your tasks to the supervisor and it runs the scheduler for you:

```python
from firm.queue.supervisor import ThreadSupervisor, SupervisorConfig, WorkerConfig, DispatcherConfig

config = SupervisorConfig(
    workers=[WorkerConfig()],
    dispatchers=[DispatcherConfig()],
    recurring=tasks,            # <- scheduler is started automatically when this is non-empty
)
with ThreadSupervisor(current_runtime(), config):
    ...  # runs until the block exits
```

(The `firm-queue start` CLI builds a similar config; pass recurring tasks by configuring the
supervisor in your own entrypoint.)

To drive it manually — e.g. in a test — a `Scheduler` has a `tick()`:

```python
scheduler = Scheduler(current_runtime(), tasks)
scheduler.sync_tasks()      # persist task definitions to recurring_tasks (for visibility)
scheduler.tick()            # enqueue anything due for the current period
```

## Exactly-once per period, even with several schedulers

Each `(task_key, run_at)` pair is recorded in `recurring_executions`, which has a **unique index**
on `(task_key, run_at)`. If you run several schedulers (for redundancy), only one of them wins the
insert for a given fire time — the rest skip it. So a recurring job is enqueued exactly once per
period across the whole fleet.

```python
# two schedulers, same task, same minute:
assert Scheduler(rt, tasks).tick(at=noon) == 1   # this one enqueued it
assert Scheduler(rt, tasks).tick(at=noon) == 0   # deduped
```
