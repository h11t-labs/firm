# API cheatsheet

A flat, greppable reference of firm's public API — return types are shown in `# ->` comments, and
keyword-only parameters are simply listed by name. See the [Cookbook](cookbook.md) for worked,
runnable examples.

## firm.queue

```python
import firm.queue as bq
from firm.queue import current_runtime

# bq.configure(database_url, ...) -> Runtime (process-global). All but database_url are keyword-only;
# pass engine=<your SQLAlchemy engine> instead of a URL to share your app's engine/pool:
bq.configure("postgresql://localhost/app", busy_timeout_ms=5000,
             pool_size=20, max_overflow=40,
             default_queue="default", preserve_finished_jobs=True)
current_runtime()        # -> Runtime


@bq.job(queue="default", priority=0, attempts=1, backoff=3.0, concurrency=None)
def my_job(x):           # the function stays directly callable
    ...


my_job.enqueue(x)                    # -> int | None  (job id, or None if discarded)
my_job.enqueue_at(when, x)           # when: datetime
my_job.enqueue_in(delta, x)          # delta: timedelta
```

Concurrency (the `concurrency=` dict on `@bq.job`):

```python
concurrency = {
    "key": lambda *a, **k: a[0],  # the variable part of the concurrency key
    "to": 1,                      # max simultaneous executions per key
    "duration": 300,              # seconds before the semaphore expires (failsafe)
    "group": None,                # share a key namespace across jobs (default: this job)
    "on_conflict": "block",       # "block" (queue it) or "discard" (drop it)
}
```

Management (`from firm.queue import queues`), each takes a `Runtime`:

```python
from firm.queue import queues

queues.all_queues(rt)    # -> list[str]
queues.size(rt, q)       # -> int
queues.pause(rt, q)
queues.resume(rt, q)
queues.is_paused(rt, q)  # -> bool
queues.clear(rt, q)      # -> int  (jobs removed)
queues.latency(rt, q)    # -> float  (seconds since the oldest ready job)
```

Maintenance (`from firm.queue import maintenance`):

```python
from datetime import timedelta
from firm.queue import maintenance

maintenance.retry_failed(rt, job_id)                          # -> bool
maintenance.retry_all_failed(rt)                              # -> int
maintenance.clear_finished(rt, older_than=timedelta(days=7), batch_size=500)  # -> int
```

Running workers:

```python
# CLI (production):
#   firm-queue start --import myapp.jobs [--mode fork|thread] [--threads N] [--queues a,b]

from firm.queue import current_runtime
from firm.queue.supervisor import (ThreadSupervisor, ForkSupervisor, SupervisorConfig,
                                     WorkerConfig, DispatcherConfig)

config = SupervisorConfig(workers=[WorkerConfig(queues=("*",), threads=3)],
                          dispatchers=[DispatcherConfig()], recurring=[])
sup = ThreadSupervisor(current_runtime(), config)   # in-process; .start() is non-blocking
sup.start()
sup.stop()                                          # or:  with ThreadSupervisor(rt, config): ...
ForkSupervisor(current_runtime(), config).start()   # production default (POSIX); blocks
```

Single steps (no long-lived process):

```python
from firm.queue.worker import run_ready
from firm.queue.dispatcher import dispatch_once, run_maintenance

run_ready(rt, queues=("*",), limit=100)   # -> int  (claim + run one batch)
dispatch_once(rt)                          # -> int  (promote due scheduled jobs)
run_maintenance(rt)                         # -> int  (release blocked jobs with capacity)
```

Recurring (`from firm.queue.scheduler import RecurringTask, Scheduler`):

```python
from firm.queue.scheduler import RecurringTask, Scheduler

# 5-field cron; pass a list as SupervisorConfig(recurring=[...]) to start a scheduler automatically
task = RecurringTask(key="nightly", schedule="0 3 * * *", job=my_job, args=(), kwargs={})
Scheduler(rt, [task]).tick()   # manual: enqueue anything due this period
```

## firm.cache

```python
from firm.cache import Cache, JSONCoder, PickleCoder

# database_url is positional; everything after it is keyword-only (defaults shown):
cache = Cache("sqlite:///cache.db", engine=None, coder=None, encrypt_key=None,
              max_age=1209600.0, max_size=268435456, max_entries=None,   # 2 weeks / 256 MiB / off
              expiry_batch_size=100, max_key_bytesize=1024, size_estimate_samples=10000,
              create_schema=True, auto_expire=True, background_expiry=False, expiry_interval=60.0)

cache.get(key)                      # -> Any | None
cache.set(key, value)
cache.fetch(key, default_or_callable)   # -> Any  (compute + store on a miss)
cache.delete(key)                   # -> bool
cache.exist(key)                    # -> bool
cache.get_multi(keys)               # -> dict
cache.set_multi(mapping)
cache.increment(key, by=1)          # -> int
cache.decrement(key, by=1)          # -> int
cache.clear()
cache.close()                       # or:  with Cache(...) as cache: ...
# keys are str|bytes; values are arbitrary (pickle default; JSONCoder for interop).
# encrypt_key=<Fernet key> encrypts values at rest (needs the [encryption] extra).
```

## firm.channel

```python
from firm.channel import Channel

channel = Channel("sqlite:///cable.db", engine=None, polling_interval=0.1,
                  message_retention=86400.0, autotrim=True, trim_batch_size=100,
                  create_schema=True)

channel.broadcast(name, payload)    # name/payload: str|bytes (str payloads are UTF-8 encoded)
channel.subscribe(name, callback)   # callback(payload: bytes); only future messages; per-process
channel.unsubscribe(name, callback)
channel.trim()                      # -> int
channel.close()                     # or:  with Channel(...) as channel: ...
```

## firm.audit

```python
from firm.audit import AuditLog, Ref, record

# database_url is positional; everything after it is keyword-only (defaults shown):
audit = AuditLog("sqlite:///audit.db", engine=None, create_schema=True,
                 max_age=None, background_retention=False, retention_interval=3600.0)

audit.record(action, subject=None, actor=None, data=None, changes=None,
             correlation_id=None, context=None, conn=None)
audit.history(subject=None, subject_type=None, subject_id=None,
              actor=None, actor_type=None, actor_id=None, action=None,
              correlation_id=None, since=None, limit=100)   # -> list[dict]
# subject_type=/subject_id= filter independently (either alone, or both); same for actor_*.
# pass either subject= or subject_type=/subject_id= for a field, never both (raises ValueError).
# a bare-string filter (actor="cron") filters by type only.
audit.close()                       # or:  with AuditLog(...) as audit: ...

# shared-DB, same-transaction (atomic with the business change):
record(conn, action, subject=None, actor=None, data=None, changes=None,
       correlation_id=None, context=None)

# subject/actor accept the same forms — type and id are each optional:
#   invoice                     domain object with `.id`  -> (ClassName, id)
#   ("Invoice", 42)             tuple; either half may be None (empty/None id -> NULL)
#   "cron"                      bare string -> a role/kind label, stored as the type (no id)
#   Ref("User", 7, name="…")    explicit, with an optional human-readable display name
#   None                        no actor/subject (a system event)
# An object may define __firm_audit_ref__(self) -> Ref to customize its audit identity.
# Each event dict from history()/get() carries subject_label/actor_label (the display names).
# data/changes/context: dicts stored as JSON text (not native JSON/JSONB).
```

## firm.contrib (optional — `[flask]` / `[fastapi]` extras)

```python
# FastAPI: app = FastAPI(lifespan=lifespan(database_url=..., embed_workers=False,
#                                          queues=("*",), threads=3))
from firm.contrib.fastapi import lifespan

# Flask: Firm(app, database_url=None, embed_workers=False, queues=("*",), threads=3)
#   reads app.config["FIRM_DATABASE_URL"]; registers `flask firm worker`
from firm.contrib.flask import Firm

from firm.contrib.sqlalchemy import enqueue_after_commit
enqueue_after_commit(session, my_job, x)   # enqueues iff the session commits
```

## Command-line tools

```bash
firm-queue start|work|drain|dispatch|maintenance --database-url ... --import myapp.jobs
firm-cache stats|clear|trim --database-url ...
firm-channel stats|trim --database-url ...
firm-audit stats|history|prune --database-url ...
firm-ui --database-url ... [--queue-url ... --cache-url ... --channel-url ... --audit-url ...] [--host --port]
```

Database URLs come from `--database-url` or the `FIRM_*_DATABASE_URL` env vars. Bare
`postgresql://` / `mysql://` URLs auto-normalize to the shipped drivers (`[postgres]` / `[mysql]`).
