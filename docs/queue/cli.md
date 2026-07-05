# CLI

The `firm-queue` command runs and operates the queue. Every command takes the database URL
(via `--database-url` or the `FIRM_QUEUE_DATABASE_URL` env var) and `--import` to load the
modules that define your jobs.

```bash
firm-queue --help
firm-queue start --help
```

## Commands

### `start` — run the full stack

Runs a supervisor with a worker and a dispatcher (and a scheduler if you've wired recurring tasks
into your own entrypoint).

```bash
firm-queue start \
  --database-url postgresql://localhost/myapp \
  --import myapp.jobs \
  --queues mailers,default \
  --threads 5 \
  --mode fork              # 'fork' (default, production) or 'thread'
```

`Ctrl-C` triggers a graceful drain. `--mode fork` forks one process per role; `--mode thread` runs
everything as threads in one process (use this on Windows).

### `work` — a single worker

Runs just a worker (no dispatcher), polling until interrupted.

```bash
firm-queue work --import myapp.jobs --queues default --threads 3
```

### `drain` — process once and exit

Claims and runs all currently-ready jobs, then exits. Handy for cron-style "process the backlog"
runs or for tests.

```bash
firm-queue drain --import myapp.jobs --limit 100
# processed 7 job(s)
```

### `dispatch` — promote due scheduled jobs once

```bash
firm-queue dispatch --import myapp.jobs
# dispatched 3 job(s)
```

### `maintenance` — run concurrency maintenance once

Expires stale semaphores and promotes blocked jobs that now have capacity.

```bash
firm-queue maintenance
# promoted 1 blocked job(s)
```

## Tips

- Set `FIRM_QUEUE_DATABASE_URL` once in your environment to omit `--database-url` everywhere.
- `--import` is repeatable: `--import myapp.jobs --import myapp.mailers`.
- For a typical deployment, run **one `start`** (or several, for more workers) per host; they
  coordinate through the database.
