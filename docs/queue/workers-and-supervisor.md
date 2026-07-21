# Workers & the supervisor

The supervisor runs the worker(s), dispatcher(s), and (optionally) the scheduler, and keeps them
alive. There are two flavors.

## ThreadSupervisor (embedded / async)

Runs every role as a thread in the current process. Perfect for development, tests, and embedding
the queue inside another app. It's a context manager:

```python
from firm.queue.supervisor import (
    ThreadSupervisor, SupervisorConfig, WorkerConfig, DispatcherConfig,
)
from firm.queue import current_runtime

config = SupervisorConfig(
    workers=[WorkerConfig(queues=("*",), threads=3, poll_interval=0.1)],
    dispatchers=[DispatcherConfig()],
)

with ThreadSupervisor(current_runtime(), config):
    run_my_app()        # workers run in the background until the block exits
```

## ForkSupervisor (production)

Forks a separate child process per role. Each child gets process
isolation and its own thread pool; the supervisor reaps and restarts dead children. This is what the
[`firm-queue start`](cli.md) CLI uses.

```python
from firm.queue.supervisor import ForkSupervisor, SupervisorConfig, WorkerConfig

ForkSupervisor(current_runtime(), SupervisorConfig(workers=[WorkerConfig(threads=5)])).start()
# blocks, supervising, until it receives a shutdown signal
```

> Fork is POSIX-only. On Windows, use `ThreadSupervisor` (or the CLI `--mode thread`).

## Configuration objects

```python
SupervisorConfig(
    workers=[WorkerConfig()],            # one or more workers
    dispatchers=[DispatcherConfig()],    # usually one
    recurring=[],                        # RecurringTasks -> a scheduler is started if non-empty
    alive_threshold=300.0,               # seconds before a silent process is pruned
    shutdown_timeout=5.0,                # grace period for a TERM drain
    heartbeat_interval=60.0,             # how often each process refreshes its heartbeat
)

WorkerConfig(queues=("*",), threads=3, poll_interval=0.1)
DispatcherConfig(batch_size=500, poll_interval=1.0, maintenance_interval=600.0)
```

`queues` accepts `"*"` (all non-paused queues, global priority order), exact names, or `prefix*`
patterns. `threads` is the worker's pool size.

> **Parallelism note:** threads parallelize I/O-bound jobs. Because of the GIL, CPU-bound jobs scale
> by running **more worker processes** (more forked workers), not more threads. On a free-threaded
> (no-GIL) Python build, the thread pool parallelizes CPU work too.

## Signals & graceful shutdown

| Signal | Behavior |
|---|---|
| `TERM` / `INT` (Ctrl-C) | **Graceful.** Stop claiming, let in-flight jobs finish within `shutdown_timeout`, then exit. |
| `QUIT` | **Immediate.** Stop now. |

The fork supervisor forwards `TERM` to its children and reaps them; any that overrun the timeout are
killed.

## Heartbeats & crash recovery {#crash-recovery}

Every running process registers a row in `firm_queue_processes` and refreshes `last_heartbeat_at`
on a timer. The supervisor:

- **Prunes** processes whose heartbeat is older than `alive_threshold` (they're presumed dead), and
- **Recovers** their in-flight work: any claim held by a dead process is moved back to
  `ready_executions` so another worker finishes it (`recovery.recover_orphaned_claims`). This also
  runs on supervisor startup, to clean up after an earlier crash.

This gives at-least-once delivery — see the idempotency note in
[Retries & failures](retries-and-failures.md).

## Lifecycle hooks

Run code when a process starts/stops/exits, and capture background errors:

```python
from firm.queue.hooks import on, on_thread_error

@on("worker_start")
def warm_caches() -> None:
    ...

@on_thread_error
def report(exc: BaseException) -> None:
    sentry.capture_exception(exc)
```

Events follow `"{role}_{phase}"` where role ∈ `worker/dispatcher/scheduler/supervisor` and phase ∈
`start/stop/exit`. A hook that raises never breaks the lifecycle — its error is routed to the
`on_thread_error` handlers. Errors raised inside a poll loop (worker/dispatcher/scheduler) are routed
there too.
