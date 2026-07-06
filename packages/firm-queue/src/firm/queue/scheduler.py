"""Scheduler — enqueue recurring jobs on a cron schedule, deduplicated across schedulers.

A :class:`RecurringTask` pairs a cron ``schedule`` with a job. On each :meth:`Scheduler.tick`
we compute the current period's fire time and enqueue the job exactly once for that
``(task_key, run_at)`` — the unique index on ``recurring_executions`` makes the dedupe safe even
with several schedulers running. A run is only enqueued once its task is recorded in
``recurring_tasks`` via :meth:`Scheduler.sync_tasks` (the :class:`SchedulerLoop` runs it on
start), so ``tick`` on a scheduler that has not synced enqueues nothing.

Schedule syntax is standard 5-field cron (via ``croniter``); natural-language (Fugit-style)
forms are not supported.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError

from .._core.clock import now_utc
from .._core.config import Runtime
from .._core.poller import InterruptiblePoller
from . import schema
from .hooks import HOOKS

try:
    from croniter import croniter
except ImportError as exc:  # pragma: no cover - exercised only without the 'queue' extra
    raise ImportError(
        'Recurring tasks require "croniter". Install the queue extra: pip install "firm[queue]"'
    ) from exc
from .job import Job
from .serialization import serialize

_jobs = schema.jobs
_ready = schema.ready_executions
_rec = schema.recurring_executions
_tasks = schema.recurring_tasks


@dataclass(frozen=True)
class RecurringTask:
    key: str
    schedule: str
    job: Job
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)

    def current_period(self, now: datetime) -> datetime:
        """The most recent fire time at or before ``now``."""
        return croniter(self.schedule, now).get_prev(datetime)


class Scheduler:
    def __init__(self, runtime: Runtime, tasks: Sequence[RecurringTask]) -> None:
        self.runtime = runtime
        self.tasks = list(tasks)

    def sync_tasks(self) -> None:
        """Persist configured tasks to ``recurring_tasks``; drop any no longer configured.

        The static config is the source of truth, so a task removed from the config is removed
        from the table on the next sync (upstream-compatible behavior).
        """
        keys = [task.key for task in self.tasks]
        with self.runtime.engine.begin() as conn:
            stale = delete(_tasks)
            if keys:
                stale = stale.where(_tasks.c.key.not_in(keys))
            conn.execute(stale)
            for task in self.tasks:
                exists = conn.execute(select(_tasks.c.id).where(_tasks.c.key == task.key)).first()
                if exists is None:
                    conn.execute(
                        insert(_tasks).values(
                            key=task.key,
                            schedule=task.schedule,
                            class_name=task.job.class_name,
                            queue_name=task.job.queue_name,
                            priority=task.job.priority,
                            static=True,
                        )
                    )

    def tick(self, at: datetime | None = None) -> int:
        """Enqueue any tasks due for the current period; return how many were enqueued."""
        now = at or now_utc()
        enqueued = 0
        for task in self.tasks:
            if self._record_and_enqueue(task, task.current_period(now)):
                enqueued += 1
        return enqueued

    def _record_and_enqueue(self, task: RecurringTask, run_at: datetime) -> bool:
        rt = self.runtime
        with rt.engine.connect() as conn:
            recorded = conn.execute(select(_tasks.c.id).where(_tasks.c.key == task.key)).first()
            if recorded is None:
                # Ordering invariant: a recurring run is only enqueued once its task is recorded
                # in ``recurring_tasks`` (via ``sync_tasks``, which the SchedulerLoop runs on
                # start). Enqueuing before that would create a run with no owning task row.
                return False
            already = conn.execute(
                select(_rec.c.id).where(_rec.c.task_key == task.key, _rec.c.run_at == run_at)
            ).first()
        if already is not None:
            return False

        args_blob = serialize(task.args, task.kwargs)
        try:
            with rt.engine.begin() as conn:
                inserted = conn.execute(
                    insert(_jobs).values(
                        queue_name=task.job.queue_name,
                        class_name=task.job.class_name,
                        arguments=args_blob,
                        priority=task.job.priority,
                        scheduled_at=run_at,
                    )
                )
                primary_key = inserted.inserted_primary_key
                assert primary_key is not None
                job_id = primary_key[0]
                conn.execute(
                    insert(_ready).values(
                        job_id=job_id, queue_name=task.job.queue_name, priority=task.job.priority
                    )
                )
                conn.execute(insert(_rec).values(job_id=job_id, task_key=task.key, run_at=run_at))
            return True
        except IntegrityError:
            # Another scheduler recorded this (task_key, run_at) first.
            return False


class SchedulerLoop(InterruptiblePoller):
    """Background loop that enqueues due recurring tasks."""

    def __init__(self, scheduler: Scheduler, poll_interval: float = 5.0) -> None:
        super().__init__(poll_interval, name="scheduler", on_error=HOOKS.fire_error)
        self.scheduler = scheduler

    def on_start(self) -> None:
        self.scheduler.sync_tasks()

    def poll(self) -> int:
        return self.scheduler.tick()
