# Queues & retention

## Inspecting and managing queues

The `queues` module is the management API:

```python
from firm.queue import queues
rt = current_runtime()

queues.all_queues(rt)          # -> ["default", "mailers", ...] (queues with ready jobs)
queues.size(rt, "mailers")     # -> number of ready jobs in the queue
queues.latency(rt, "mailers")  # -> seconds since the oldest ready job was enqueued
```

## Pausing a queue

A paused queue is skipped by workers — its jobs stay ready but nothing claims them until you resume.

```python
queues.pause(rt, "mailers")        # stop processing 'mailers'
queues.is_paused(rt, "mailers")    # -> True
queues.resume(rt, "mailers")       # start again
```

Pausing is durable (a row in `firm_pauses`), so it survives restarts.

## Clearing a queue

```python
queues.clear(rt, "mailers")   # discard all ready jobs in the queue; returns how many were removed
```

This deletes the jobs (and cascades to their execution rows). Use it to drain a queue you no longer
want processed.

## Finished-job retention

By default, finished jobs are **kept** (their `jobs.finished_at` is stamped) so you can inspect what
ran. Control this with the `preserve_finished_jobs` setting:

```python
bq.configure(database_url=..., preserve_finished_jobs=False)   # delete jobs as they finish
```

When preserving (the default), prune old finished jobs periodically with `maintenance.clear_finished`:

```python
from datetime import timedelta
from firm.queue import maintenance

maintenance.clear_finished(rt)                                  # delete all finished jobs
maintenance.clear_finished(rt, older_than=timedelta(days=7))    # only those finished > 7 days ago
```

It works in batches (`batch_size`, default 500) and returns how many rows it removed; call it in a
loop or on a schedule (a [recurring task](recurring.md) is a natural fit) until it returns 0.
