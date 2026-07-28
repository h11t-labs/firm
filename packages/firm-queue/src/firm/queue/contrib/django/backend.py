"""Django 6 Tasks backend — enqueue ``django.tasks`` tasks onto firm-queue.

    # settings.py
    TASKS = {
        "default": {
            "BACKEND": "firm.queue.contrib.django.backend.FirmBackend",
            "QUEUES": [],                       # [] = accept any queue name
            "OPTIONS": {"ENQUEUE_ON_COMMIT": True},
        }
    }

    # demo/tasks.py
    from django.tasks import task

    @task(queue_name="mailers", priority=10)
    def send_welcome(user_id): ...

    send_welcome.enqueue(1)     # a row in firm_queue_jobs; a firm worker runs it

The backend only *writes* the job; nothing here runs it. Run `manage.py firm_worker` (or any
other firm worker) against the same database, and configure firm-queue itself once in
``AppConfig.ready()`` — this backend resolves ``firm.queue.current_runtime()`` lazily, at enqueue
time, so the two are independent.

Every enqueued task becomes the same firm job — :func:`run_task` — parameterised with the task's
``module.qualname``. Like any ``@job``, that one is resolved through firm's registry, so **the
worker process has to import this module** or every task fails with ``UnknownJob``. Enqueueing
processes import it on their own (Django imports ``BACKEND`` to build the backend); a worker does
not, so name it explicitly:

    FIRM_QUEUE = {"IMPORTS": ("firm.queue.contrib.django.backend",)}   # manage.py firm_worker

Outside Django — a plain ``firm-queue start`` sharing the same database — that is
``--import firm.queue.contrib.django.backend``.

Needs the ``[django]`` extra, with Django 6.0 or newer (``django.tasks`` does not exist before
that).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import firm.queue as bq
from firm.queue.config import current_runtime
from firm.queue.enqueue import enqueue as enqueue_firm_job
from firm.queue.job import Job

try:
    from django.core.checks import Error as CheckError
    from django.db import DEFAULT_DB_ALIAS, transaction
    from django.tasks import Task, TaskResult, TaskResultStatus
    from django.tasks.backends.base import BaseTaskBackend
    from django.tasks.exceptions import InvalidTask
    from django.tasks.signals import task_enqueued
    from django.utils import timezone
    from django.utils.crypto import get_random_string
    from django.utils.module_loading import import_string
except ImportError as exc:  # pragma: no cover - exercised only without the 'django' extra
    raise ImportError(
        'The firm Django Tasks backend requires "django" 6.0 or newer (that is the release that '
        'added django.tasks). Install the django extra: pip install "firm-queue[django]"'
    ) from exc

logger = logging.getLogger("firm.queue.contrib.django")


@bq.job()
def run_task(result_id: str, module_path: str, args: list[Any], kwargs: dict[str, Any]) -> None:
    """The firm job every enqueued Django task becomes: resolve ``module_path``, then run it.

    ``@task`` replaces the decorated function with a :class:`~django.tasks.Task`, so importing
    ``module.qualname`` gives the Task back and :meth:`Task.call` handles the sync/coroutine
    split for us. The return value is dropped, because firm has nowhere to put it.

    ``result_id`` is the id of the :class:`~django.tasks.TaskResult` the caller got back. It is
    carried in the arguments blob purely so the ``Task id=...`` line that ``django.tasks`` logs at
    enqueue time can be traced to a row in ``firm_queue_jobs``.
    """
    logger.debug("running Django task id=%s path=%s", result_id, module_path)
    task = import_string(module_path)
    if not isinstance(task, Task):
        raise TypeError(
            f"{module_path!r} is not a django.tasks Task (got {type(task).__name__}). Only "
            f"functions decorated with @task can be enqueued through the firm backend."
        )
    task.call(*args, **kwargs)


class FirmBackend(BaseTaskBackend):
    """Stores ``django.tasks`` tasks in firm-queue's tables.

    Supported ``OPTIONS``:

    ``ENQUEUE_ON_COMMIT`` (default ``True``)
        Delay the insert until Django's transaction commits, via ``transaction.on_commit``.
        firm writes over its own SQLAlchemy connection and can never join Django's transaction,
        so a bare enqueue inside ``atomic()`` survives a rollback on PostgreSQL/MySQL and
        deadlocks on SQLite. Outside a transaction ``on_commit`` runs the callback immediately,
        so leaving this on costs nothing. Note that under ``TestCase`` (which rolls back) the
        callback never fires unless you use ``captureOnCommitCallbacks``.

        This defaults the other way round from a plain ``@bq.job``, where deferring is opt-in
        through :func:`firm.queue.contrib.django.enqueue_on_commit`. The reason is the return value:
        ``on_commit`` discards its callback's, so a deferred ``Job.enqueue()`` cannot hand back
        the ``job_id``, while a ``TaskResult`` id is minted here before the insert and costs
        nothing to defer.
    ``DATABASE_ALIAS`` (default: whatever ``FIRM_QUEUE["DATABASE_ALIAS"]`` says)
        Which Django connection's commit ``ENQUEUE_ON_COMMIT`` waits for. Following the alias
        firm itself mirrors is right by default: that connection holds the rows the task will
        read.

    ``QUEUES`` (a sibling of ``OPTIONS``, handled by Django's base class) restricts which
    ``queue_name`` values are accepted; the default is ``["default"]`` and ``[]`` means "any".

    There is deliberately no retry option. firm looks a job's retry policy up in its registry
    when the job fails, not on the row that was enqueued, so a per-backend setting could not
    reach it — a failing task is recorded in ``firm_queue_failed_executions`` and stays there.
    """

    # firm stores a scheduled_at; the dispatcher promotes the row once it is due.
    supports_defer = True
    # run_task goes through Task.call(), which runs a coroutine function under async_to_sync.
    supports_async_task = True
    # firm's worker throws the return value away and keeps no row keyed by TaskResult id.
    supports_get_result = False
    # firm claims ready rows in priority order — see _firm_priority for the sign.
    supports_priority = True

    def __init__(self, alias: str, params: dict[str, Any]) -> None:
        super().__init__(alias, params)
        self.enqueue_on_commit = bool(self.options.get("ENQUEUE_ON_COMMIT", True))

    def validate_task(self, task: Task) -> None:
        super().validate_task(task)
        if task.takes_context:
            raise InvalidTask(
                "The firm backend does not support takes_context=True. A TaskContext exposes the "
                "TaskResult (its id, its attempt count, its errors), and firm persists none of "
                "that — the context it received would be fiction. Pass what the task needs as "
                "arguments instead."
            )

    def enqueue(self, task: Task, args: tuple[Any, ...], kwargs: dict[str, Any]) -> TaskResult:
        self.validate_task(task)

        # TaskResult's __post_init__ runs args/kwargs through normalize_json, which raises for
        # anything that is not JSON — so this doubles as the argument check firm would otherwise
        # only make inside enqueue_firm_job.
        result = TaskResult(
            task=task,
            id=get_random_string(32),
            status=TaskResultStatus.READY,
            enqueued_at=None,
            started_at=None,
            last_attempted_at=None,
            finished_at=None,
            args=list(args),
            kwargs=kwargs,
            backend=self.alias,
            errors=[],
            worker_ids=[],
        )
        payload = (result.id, task.module_path, result.args, result.kwargs)
        firm_job = self._firm_job(task)
        scheduled_at = _run_after(task)

        def insert() -> None:
            enqueue_firm_job(firm_job, payload, {}, scheduled_at=scheduled_at)

        if self.enqueue_on_commit:
            transaction.on_commit(insert, using=self.database_alias())
        else:
            insert()

        object.__setattr__(result, "enqueued_at", timezone.now())
        task_enqueued.send(type(self), task_result=result)
        return result

    def get_result(self, result_id: str) -> TaskResult:
        raise NotImplementedError(
            "firm stores no task results: the worker discards the return value and keeps no row "
            "keyed by TaskResult id, which is why supports_get_result is False. Have the task "
            "write what you need where you can read it, or watch the job itself in firm-ui."
        )

    def check(self, **kwargs: Any) -> list[CheckError]:
        """Fail ``manage.py check`` when firm-queue was never configured in this process."""
        try:
            current_runtime()
        except RuntimeError:
            return [
                CheckError(
                    "firm-queue is not configured, so the firm Tasks backend cannot enqueue.",
                    hint=(
                        "Call firm.queue.configure(database_url=...) in your AppConfig.ready() — "
                        "see docs/django.md."
                    ),
                    id="firm.E001",
                )
            ]
        return []

    def database_alias(self) -> str:
        """The Django connection ``ENQUEUE_ON_COMMIT`` hangs the insert off.

        Read per enqueue rather than cached in ``__init__``, because the fallback lives in the
        ``FIRM_QUEUE`` settings dict and Django only rebuilds task backends when ``TASKS``
        changes — a cached copy would go stale under ``override_settings``.
        """
        alias = self.options.get("DATABASE_ALIAS")
        if alias is not None:
            return str(alias)
        from .conf import get_settings

        return str(get_settings()["DATABASE_ALIAS"] or DEFAULT_DB_ALIAS)

    def _firm_job(self, task: Task) -> Job:
        """A throwaway :class:`~firm.queue.job.Job` carrying this task's routing.

        firm reads the queue name and the priority off the Job object rather than off the enqueue
        call, so per-task routing means one Job per enqueue. It is deliberately not registered:
        the worker resolves ``run_task`` through the registration this module already did at
        import — which is also why the retry policy is copied from there rather than invented
        here. The registry's copy is the one a failure would consult; two policies for one
        ``class_name`` could only ever disagree.
        """
        return Job(
            run_task.func,
            class_name=run_task.class_name,
            queue_name=task.queue_name,
            priority=_firm_priority(task.priority),
            retry_policy=run_task.retry_policy,
        )


def _firm_priority(priority: int) -> int:
    """Django orders by descending priority (-100..100, higher first); firm claims ready rows in
    ascending ``priority`` order (lower first). Negating is the whole translation."""
    return -priority


def _run_after(task: Task) -> datetime | None:
    """``task.run_after`` as something firm can store.

    firm keeps naive UTC timestamps and converts aware ones for you. With ``USE_TZ = True``
    Django's base class already guarantees an aware datetime; with ``USE_TZ = False`` a naive one
    means the project's ``TIME_ZONE``, which is *not* what firm would assume, so attach the
    timezone here.
    """
    run_after = task.run_after
    if run_after is None or timezone.is_aware(run_after):
        return run_after
    return timezone.make_aware(run_after)
