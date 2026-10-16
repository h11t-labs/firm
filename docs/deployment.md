# Deployment

firm is **database-backed** — the SQL database *is* the coordination substrate, so there is no
broker (no Redis, no RabbitMQ) and no new stateful component to run. The database you already have
is the only stateful piece; everything else is stateless and coordinates through it.

Only the **queue** runs a long-lived process. `cache`, `channel`, and `audit` are in-process
libraries your app calls directly — they add tables, not daemons.

| Module | Runs as | Periodic housekeeping |
|---|---|---|
| **queue** | a worker process (embedded thread **or** a separate `firm-queue start`) | — |
| **cache** | in-process library calls | eviction is opportunistic (on write, or an in-process timer); optional [`firm-cache trim`](cache/cli.md) |
| **channel** | in-process publish/subscribe | optional [`firm-channel trim`](channel/cli.md) |
| **audit** | in-process writes | [`firm-audit prune`](audit/cli.md) on a schedule (never automatic) |

!!! note "The database is the only stateful component"
    Adding firm to an app you already run costs, at most, **one extra worker deployment** plus a
    run-once migration — no broker, no cache server, no new datastore.

So there are two ways to run the queue. Pick by scale.

## Option A — Embedded (in your app process)

The [`ThreadSupervisor`](queue/workers-and-supervisor.md) runs every role (worker, dispatcher) as a
background thread inside your existing process. It's a context manager:

```python
from firm.queue import current_runtime
from firm.queue.supervisor import (
    DispatcherConfig,
    SupervisorConfig,
    ThreadSupervisor,
    WorkerConfig,
)

config = SupervisorConfig(
    workers=[WorkerConfig(queues=("*",), threads=3, poll_interval=0.1)],
    dispatchers=[DispatcherConfig()],
)

with ThreadSupervisor(current_runtime(), config):
    run_my_app()  # requests are served here; jobs drain in the background
```

**Use it when** you run a single service at low-to-moderate job volume and want the simplest
possible operations — one image, one deployment, nothing extra to schedule.

**The trade-offs:** web and job work share the same pods (and the GIL), so they compete for CPU and
can't scale independently, and a misbehaving job shares a process with request serving. When those
bite, move to Option B.

→ Runnable: [`examples/embedded_worker.py`](../examples/embedded_worker.py)

## Option B — A separate worker deployment

The [`ForkSupervisor`](queue/workers-and-supervisor.md) forks one child process per role and keeps
them alive; it's what the [`firm-queue start`](queue/cli.md) CLI runs. Run it as its own
deployment, using the **same image** as your app with a different command:

```bash
# your web pods enqueue:
uvicorn myapp:app
# your worker pods process (same image, different command):
firm-queue start --mode fork --import myapp.jobs --queues '*' --threads 5
```

Workers coordinate entirely through the database, so you scale throughput by **running more
replicas**. There's no leader to elect: the recurring scheduler deduplicates each cron tick across
every replica (a unique index on `(task_key, run_at)`), so it's safe to run the full stack —
including recurring tasks — on all of them.

**Use it when** you want to scale jobs independently of web traffic, isolate job crashes from
request serving, or saturate more than one core (CPU-bound work scales by *processes*, not threads,
because of the GIL).

See [Running on Kubernetes](#running-on-kubernetes) below for the deployment shape.

## Running on Kubernetes

The DB-backed model keeps the topology small: no StatefulSets or PVCs on the app side, no broker.

| Component | k8s object | Notes |
|---|---|---|
| Database | managed service or an operator (CloudNativePG, …) | the only stateful piece; **SQLite can't be shared across pods** — use PostgreSQL or MySQL |
| Schema migration | run-once `Job` (or a Helm/Argo pre-upgrade hook) | creates + stamps the schema before workers roll out |
| Workers | `Deployment` | `firm-queue start`; scale with `replicas` |
| Dashboard (optional) | `Deployment` + `Service` + `Ingress` | `firm-ui`; one replica |
| Trim / prune (optional) | `CronJob`s | `firm-cache trim`, `firm-channel trim`, `firm-audit prune` |

A worker Deployment is your app's image running a different command — the fields the subsections
below explain are the load-bearing ones:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: firm-workers }
spec:
  replicas: 3                                    # scale throughput here
  template:
    spec:
      terminationGracePeriodSeconds: 120         # >= your longest job (see below)
      containers:
        - name: worker
          image: your-registry/yourapp:tag       # your app image, with firm installed
          # exec form -> the supervisor is PID 1 and receives SIGTERM for a graceful drain
          command: ["firm-queue", "start", "--mode", "fork",
                    "--import", "yourapp.jobs", "--queues", "*", "--threads", "5"]
          env:
            - name: FIRM_QUEUE_DATABASE_URL
              valueFrom: { secretKeyRef: { name: firm-db, key: url } }
          livenessProbe:                          # no HTTP port -> probe the process
            exec: { command: ["pgrep", "-f", "firm-queue"] }
```

### Graceful shutdown & rolling deploys

The supervisor treats `SIGTERM`/`SIGINT` as a **graceful drain**: it stops claiming new work, lets
in-flight jobs finish within `shutdown_timeout` (default 5s), forwards `TERM` to its children, and
`SIGKILL`s any that overrun (see
[Signals](queue/workers-and-supervisor.md#signals-graceful-shutdown)). Kubernetes sends `SIGTERM`,
then `SIGKILL`s after `terminationGracePeriodSeconds` (default **30s**).

- Set **`terminationGracePeriodSeconds` above your longest job** (and raise `shutdown_timeout` to
  match) so k8s doesn't hard-kill mid-drain.
- **Make the supervisor PID 1** so it receives the signal — run the CLI as the container's command
  directly (exec form), not wrapped in a shell that swallows `SIGTERM`. If you must wrap it, use
  `exec firm-queue …`.

!!! tip "Hard kills are safe"
    Every process heartbeats into `firm_queue_processes`; when one dies, its in-flight claims are
    recovered to `firm_queue_ready_executions` and another worker finishes them (at-least-once — keep
    jobs idempotent). An ungraceful kill is never lost work; graceful shutdown just avoids
    re-running in-flight jobs. See [crash recovery](queue/workers-and-supervisor.md#crash-recovery).

### Database connections

The default engine pool is `pool_size=20` + `max_overflow=40` — **up to 60 connections per
process** — and fork mode runs worker + dispatcher (+ scheduler) as *separate* processes, each with
its own pool. Multiply by replicas and you can exhaust PostgreSQL's `max_connections` quickly.

- Right-size the pool to your worker's `threads` (a 5-thread worker needs ~5–8 connections, not 60)
  by passing `pool_size`/`max_overflow` to [`configure()`](queue/configuration.md) in your worker
  entrypoint, **or** front the database with **PgBouncer** (transaction pooling) and keep app pools
  small.
- `pool_pre_ping` is on for PostgreSQL/MySQL, so dropped connections (failovers, PgBouncer
  restarts) heal automatically.

### Health & liveness

Workers expose **no HTTP endpoint** — if the supervisor exits, the container exits and k8s restarts
it, so a simple process-liveness probe (`pgrep -f firm-queue`) is enough. For a deeper check,
assert the pod's row in `firm_queue_processes` has a fresh `last_heartbeat_at`.

The dashboard *does* serve HTTP, but with Basic auth every request returns **401**, which an
`httpGet` probe counts as a failure — use a **`tcpSocket`** probe on its port instead.

### Autoscaling (optional)

You often don't need it — idle workers back off their polling, so a right-sized fixed replica count
is a legitimate setup. If you do want it: the built-in **CPU `HorizontalPodAutoscaler`** works with
no extra components, and to scale on actual backlog instead, point [KEDA](https://keda.sh)'s
PostgreSQL scaler (or a Prometheus adapter feeding a standard HPA) at
`SELECT count(*) FROM firm_queue_ready_executions`. Either way, remember the GIL: scale CPU-bound work
with **more replicas (processes)**, not more `--threads`.

### Recurring tasks

Wire recurring tasks into your worker entrypoint and let firm's scheduler run them — it's
dedup-safe across replicas, so no singleton deployment is needed:

```python
from firm.queue.scheduler import RecurringTask
from firm.queue.supervisor import SupervisorConfig, WorkerConfig
from myapp.jobs import cleanup

config = SupervisorConfig(
    workers=[WorkerConfig()],
    recurring=[RecurringTask(key="nightly-cleanup", schedule="0 3 * * *", job=cleanup)],
)
```

### Migrations

Run migrations as a one-shot `Job` (or an init container / pre-upgrade hook) before rolling out
workers. Each module keeps its own version table (`firm_<module>_alembic_version`), so all four can
share one database and migrate independently — see
[Database backends](database-backends.md#migrations). In a source checkout that's
`alembic -c alembic.queue.ini upgrade head`. A pip-installed image doesn't ship the `.ini`, so
create and stamp the baseline from code instead — it's idempotent, so it's safe to run before every
rollout:

```python
import os

import firm.queue as bq
from firm.queue import current_runtime, schema

bq.configure(database_url=os.environ["FIRM_QUEUE_DATABASE_URL"])
schema.create_all(current_runtime().engine)  # create tables + stamp the Alembic baseline
```
