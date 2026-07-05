# Concurrency controls

Sometimes you need to cap how many copies of a job run at once — e.g. only one sync per account, or
at most two report builds per tenant. `concurrency` does that.

```python
@bq.job(concurrency={"key": lambda order_id: order_id, "to": 1, "duration": 300})
def charge(order_id: int) -> None:
    ...
```

Now at most **one** `charge` runs per `order_id` at a time. Extra enqueues are *blocked* until the
running one finishes.

## The `concurrency` dict

| Field | Default | Meaning |
|---|---|---|
| `key` | — | A callable `(*args, **kwargs) -> value`. The "variable" part of the concurrency key. |
| `to` | `1` | Max simultaneous executions for a given key. |
| `duration` | `180.0` | Seconds before the lock is considered stale (failsafe — see below). |
| `group` | job's name | Namespace for the key. Give two different jobs the same `group` to make them share a limit. |
| `on_conflict` | `"block"` | `"block"` to queue the job, or `"discard"` to drop it. |

The full key is `"{group}/{key(...)}"`. With the default group (the job's `module.qualname`), each
job's keys are isolated; set a shared `group` to coordinate across jobs.

## Block vs. discard

```python
# block (default): the 2nd job waits, then runs when the 1st finishes
@bq.job(concurrency={"key": lambda id: id, "to": 1})
def sync(id): ...

# discard: while one is running/queued for the key, new enqueues are dropped
@bq.job(concurrency={"key": lambda id: id, "to": 1, "on_conflict": "discard"})
def refresh(id): ...

refresh.enqueue(1)   # -> job_id
refresh.enqueue(1)   # -> None   (discarded; nothing inserted)
```

## Throttling with `to > 1`

```python
@bq.job(concurrency={"key": lambda tenant: tenant, "to": 2})
def build_report(tenant): ...
```

The first two enqueues per tenant become ready; the third is blocked until one finishes.

## How it works

A `semaphores` row holds the remaining capacity for a key. Enqueuing a concurrency-limited job
**acquires** (decrements) it: if capacity remains the job goes to `ready_executions`, otherwise to
`blocked_executions`. When a job finishes (or fails), the worker **releases** (increments) the
semaphore and **promotes** the next blocked job for that key to ready.

> **Note:** for jobs enqueued with `enqueue_at`/`enqueue_in`, the concurrency check happens when the
> **dispatcher** promotes them, not at enqueue time.

### The `duration` failsafe

If a worker dies after acquiring a semaphore but before releasing it, the slot would be stuck
forever. `duration` bounds that: the semaphore and the blocked entry carry an `expires_at`, and the
dispatcher's **maintenance** pass reclaims expired semaphores and promotes blocked jobs once a slot
is free. Set `duration` comfortably longer than the job's worst-case runtime.

> **Performance:** concurrency controls add work (a semaphore row per key, blocked/ready churn). Use
> them where you need correctness, not as a general throttle for everything.

## On SQLite

SQLite has no row-level locking, so concurrency control there relies on the `BEGIN IMMEDIATE` write
lock instead — and is fully exercised on SQLite, Postgres, and MySQL here. See [Internals](internals.md).
