# Retries & failures

## Automatic retries

Set `attempts` to the total number of attempts (including the first):

```python
@bq.job(attempts=3, backoff=2.0)
def flaky() -> None:
    call_unreliable_api()
```

When the job raises:

1. If attempts remain, it's **rescheduled** for a retry at `now + backoff**attempt` seconds (so
   attempt 1 waits `2s`, attempt 2 waits `4s`, …). The dispatcher promotes it again when due.
2. When attempts are exhausted, the traceback is written to `failed_executions` and the job stops.

`attempts=1` (the default) means run once, no retry.

Attempt counting is tracked on the `jobs.attempts` column.

> **Note:** firm owns retry semantics itself, which is why there's an `attempts` column on
> the jobs table tracking each job's tries.

## Inspecting failures

A failed job has a row in `firm_failed_executions` with the full traceback in `error`:

```python
from sqlalchemy import select
from firm.queue import schema

with current_runtime().engine.connect() as conn:
    for row in conn.execute(select(schema.failed_executions.c.job_id, schema.failed_executions.c.error)):
        print(row.job_id, row.error.splitlines()[-1])
```

## Manual retry

Move failed jobs back to ready with the `maintenance` helpers:

```python
from firm.queue import maintenance

maintenance.retry_failed(current_runtime(), job_id)   # one job; returns True if it was failed
maintenance.retry_all_failed(current_runtime())       # everything; returns how many were retried
```

Retrying resets the attempt counter and clears `finished_at`, so the job runs fresh.

## Unregistered jobs

If a worker claims a job whose `class_name` isn't in the registry (e.g. the function was removed in
a deploy), it's recorded as a failed execution with a clear error rather than crashing the worker.
Make sure workers `--import` every module that defines jobs.

## Crash recovery

If a **worker process dies** mid-job (kill -9, OOM, machine reboot), its claimed job would otherwise
be stuck. The supervisor detects the dead process (stale heartbeat) and **re-readies** the orphaned
claim so another worker finishes it. See
[Workers & the supervisor](workers-and-supervisor.md#crash-recovery).

> **At-least-once:** because recovered jobs are retried, a job can run more than once if a worker
> dies after the work completed but before the claim was cleared. Make jobs idempotent — the same
> guidance any at-least-once queue gives.
