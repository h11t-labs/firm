# Defining jobs

## The `@job` decorator

```python
@bq.job(
    queue="default",     # which queue this job lands in
    priority=0,          # lower numbers are claimed first
    attempts=1,          # total attempts before it's marked failed (1 = no retry)
    backoff=3.0,         # exponential backoff base, in seconds
    concurrency=None,    # optional concurrency-control dict (see below)
)
def my_job() -> None: ...
```

| Option | Default | Meaning |
|---|---|---|
| `queue` | `"default"` | Queue name. Workers select queues by exact name, `prefix*`, or `*`. |
| `priority` | `0` | Claim order within a queue — **lower is first**. |
| `attempts` | `1` | Total attempts. `1` means run once, no retry. See [Retries & failures](retries-and-failures.md). |
| `backoff` | `3.0` | Base for exponential backoff: attempt _n_ waits `backoff**n` seconds. |
| `concurrency` | `None` | Limit simultaneous runs — see [Concurrency controls](concurrency.md). |

The decorated object is a `Job`. It is **still directly callable**, so unit tests don't need a
database:

```python
@bq.job()
def add(a: int, b: int) -> int:
    return a + b

assert add(2, 3) == 5          # calls the function directly
add.enqueue(2, 3)              # schedules it to run in a worker
```

## Enqueuing

```python
from datetime import datetime, timedelta

my_job.enqueue(42)                                    # run now
my_job.enqueue_in(timedelta(minutes=5), 42)           # run after a delay
my_job.enqueue_at(datetime(2026, 7, 1, 9, 0), 42)     # run at a specific time (naive UTC)
```

- `enqueue` puts the job straight into `ready_executions` (or `blocked_executions` if a concurrency
  limit applies).
- `enqueue_at` / `enqueue_in` put it into `scheduled_executions`; the **dispatcher** promotes it to
  ready when its time comes.
- All three return the new `job_id`, or `None` if the job was **discarded** by a concurrency rule
  (`on_conflict="discard"`).

> **Note:** datetimes are naive UTC throughout. Pass UTC times to `enqueue_at`.

## How a job is identified

Each job is registered under its `module.qualname` (e.g. `myapp.jobs.send_welcome`). That string is
stored in `jobs.class_name`, and the worker looks it up in the registry to find the callable. Two
consequences:

- **Import your job modules** before running a worker (the CLI's `--import` flag does this), so the
  registry is populated.
- A job enqueued by an old deploy that no longer defines the function fails cleanly with an
  "unregistered job" error (recorded as a failed execution), rather than crashing the worker.

## Argument serialization

Arguments are serialized to compact JSON and stored in `jobs.arguments`. Supported types:

- JSON natives: `str`, `int`, `float`, `bool`, `None`, `list`, `dict`.
- Tagged extras that round-trip exactly: `datetime`, `date`, `Decimal`, `UUID`.

```python
from datetime import datetime
from decimal import Decimal

@bq.job()
def charge(amount: Decimal, at: datetime) -> None: ...

charge.enqueue(Decimal("9.99"), datetime(2026, 1, 1, 12, 0))   # fine
```

Anything else raises `TypeError` **at enqueue time** — a deliberate fail-fast, so the bug is the
caller's, not a worker's hours later:

```python
charge.enqueue(object())   # TypeError: ... is not a serializable job argument
```

> **Tip:** pass IDs, not objects. There is no equivalent of Rails' GlobalID; serialize a `user_id`
> and reload the record inside the job.
