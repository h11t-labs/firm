"""Specs for the Django 6 Tasks backend.

Django is booted once per session by the ``django_project`` fixture in conftest (there is no
pytest-django, so that fixture does ``settings.configure()`` + ``django.setup()`` by hand); these
specs only add a ``TASKS`` setting pointing at the backend under test. That means ``@task`` cannot
run at import — settings do not exist yet — so the task functions below are plain functions until
``django_tasks`` turns them into Tasks, which is also what a real ``<app>/tasks.py`` ends up with.

firm's own database is the ``queue_db`` fixture's, a real file, exactly as in a real project.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import select

from firm.queue import schema
from firm.queue.config import set_runtime
from firm.queue.registry import REGISTRY
from firm.queue.worker import run_ready

pytest.importorskip("django")
pytest.importorskip("django.tasks", reason="django.tasks arrived in Django 6.0")

from django.db import transaction
from django.tasks import Task, TaskResultStatus, task, task_backends
from django.tasks.exceptions import InvalidTask
from django.tasks.signals import task_enqueued
from django.test import override_settings
from django.utils import timezone

from firm.queue.contrib.django.backend import run_task

BACKEND = "firm.queue.contrib.django.backend.FirmBackend"
RUNNER = "firm.queue.contrib.django.backend.run_task"

# What the tasks below record when they run.
performed: list[str] = []


def record(label: str) -> str:
    performed.append(label)
    return label  # dropped by firm — see test_return_values_are_not_retrievable


async def record_async(label: str) -> None:
    performed.append(f"async:{label}")


def _needs_context(context, label: str) -> None:  # never enqueued; see the takes_context spec
    pass


@pytest.fixture(scope="session")
def django_tasks(django_project) -> Iterator[None]:
    """Point ``TASKS`` at the firm backend, then promote the functions above to real Tasks.

    Rebinding the module attributes is what ``@task`` would have done at import, and it matters
    for more than tidiness: the worker resolves a task by importing ``module.qualname``, so the
    name has to end up bound to the Task rather than to the bare function.
    """
    override = override_settings(
        TASKS={
            "default": {"BACKEND": BACKEND, "QUEUES": []},
            # A second alias, to prove OPTIONS are read per backend instance.
            "eager": {"BACKEND": BACKEND, "QUEUES": [], "OPTIONS": {"ENQUEUE_ON_COMMIT": False}},
        }
    )
    override.enable()
    global record, record_async
    record, record_async = task(record), task(record_async)
    try:
        yield
    finally:
        override.disable()


@pytest.fixture(autouse=True)
def _clean(django_tasks, queue_db) -> Iterator[None]:
    """Every spec gets a fresh firm database (from conftest) and no history."""
    performed.clear()
    yield
    performed.clear()


def _rows(runtime, table) -> list:
    with runtime.engine.connect() as conn:
        return conn.execute(select(table)).all()


def _jobs(runtime) -> list:
    return _rows(runtime, schema.jobs)


# --- enqueue reaches firm's tables -----------------------------------------------------


def test_enqueue_writes_a_firm_job_row(queue_db) -> None:
    result = record.enqueue("hello")

    (row,) = _jobs(queue_db)
    assert row.class_name == RUNNER  # every task is the same firm job, parameterised
    assert row.queue_name == "default"
    assert record.module_path in row.arguments
    assert "hello" in row.arguments
    assert result.id in row.arguments  # the id Django logged is findable in the row
    assert len(_rows(queue_db, schema.ready_executions)) == 1


def test_the_runner_is_registered_under_a_stable_class_name(queue_db) -> None:
    # Importing the module is the whole registration, and this name is stored in every job row:
    # renaming it would strand in-flight jobs, and failing to import it in the worker process
    # (FIRM_QUEUE["IMPORTS"]) is what turns every task into an UnknownJob.
    assert REGISTRY.lookup(RUNNER) is run_task


def test_enqueue_returns_a_ready_task_result(queue_db) -> None:
    result = record.enqueue("hello")

    assert result.status == TaskResultStatus.READY
    assert result.args == ["hello"]
    assert result.kwargs == {}
    assert result.backend == "default"
    assert result.enqueued_at is not None


def test_queue_name_routes_to_the_firm_queue(queue_db) -> None:
    record.using(queue_name="mailers").enqueue("hello")

    assert _jobs(queue_db)[0].queue_name == "mailers"
    assert _rows(queue_db, schema.ready_executions)[0].queue_name == "mailers"


def test_non_json_arguments_are_refused_at_enqueue(queue_db) -> None:
    with pytest.raises(TypeError):
        record.enqueue(object())

    assert _jobs(queue_db) == []


# --- a firm worker actually runs the task ----------------------------------------------


def test_a_firm_worker_runs_the_task(queue_db) -> None:
    record.enqueue("from-the-worker")

    assert run_ready(queue_db, limit=10) == 1

    assert performed == ["from-the-worker"]
    assert _rows(queue_db, schema.failed_executions) == []
    assert _jobs(queue_db)[0].finished_at is not None


def test_keyword_arguments_survive_the_round_trip(queue_db) -> None:
    record.enqueue(label="kw")

    assert run_ready(queue_db, limit=10) == 1
    assert performed == ["kw"]


def test_an_async_task_is_run_to_completion(queue_db) -> None:
    # This is what supports_async_task rests on: run_task calls Task.call(), which puts a
    # coroutine function through async_to_sync instead of leaving an un-awaited coroutine.
    record_async.enqueue("coro")

    assert run_ready(queue_db, limit=10) == 1

    assert performed == ["async:coro"]
    assert _rows(queue_db, schema.failed_executions) == []


def test_an_unresolvable_task_path_fails_the_job(queue_db) -> None:
    run_task.enqueue("some-id", "tests.contrib.nope.gone", [], {})

    run_ready(queue_db, limit=10)

    (failed,) = _rows(queue_db, schema.failed_executions)
    assert "ImportError" in failed.error or "ModuleNotFoundError" in failed.error


def test_a_path_that_is_not_a_task_fails_the_job(queue_db) -> None:
    run_task.enqueue("some-id", f"{__name__}._needs_context", [], {})

    run_ready(queue_db, limit=10)

    (failed,) = _rows(queue_db, schema.failed_executions)
    assert "is not a django.tasks Task" in failed.error


# --- the capability flags, checked against behaviour ------------------------------------


def test_supports_priority_and_djangos_highest_is_claimed_first(queue_db) -> None:
    assert task_backends["default"].supports_priority is True

    record.using(priority=-10).enqueue("low")
    record.using(priority=10).enqueue("high")

    # Django orders by descending priority, firm by ascending, so the backend flips the sign.
    assert [row.priority for row in _jobs(queue_db)] == [10, -10]
    assert run_ready(queue_db, limit=1) == 1
    assert performed == ["high"]


def test_supports_defer_and_run_after_lands_in_scheduled_executions(queue_db) -> None:
    assert task_backends["default"].supports_defer is True
    when = timezone.now() + timedelta(hours=1)

    record.using(run_after=when).enqueue("later")

    assert _rows(queue_db, schema.ready_executions) == []
    (scheduled,) = _rows(queue_db, schema.scheduled_executions)
    assert scheduled.scheduled_at == when.replace(tzinfo=None)  # firm keeps naive UTC
    assert run_ready(queue_db, limit=10) == 0  # not due, so no worker touches it


def test_a_past_run_after_is_enqueued_immediately(queue_db) -> None:
    record.using(run_after=timezone.now() - timedelta(hours=1)).enqueue("overdue")

    assert len(_rows(queue_db, schema.ready_executions)) == 1
    assert run_ready(queue_db, limit=10) == 1
    assert performed == ["overdue"]


def test_supports_get_result_is_false_and_get_result_says_why(queue_db) -> None:
    backend = task_backends["default"]
    assert backend.supports_get_result is False

    with pytest.raises(NotImplementedError, match="firm stores no task results"):
        backend.get_result("whatever")


def test_return_values_are_not_retrievable(queue_db) -> None:
    result = record.enqueue("hello")
    run_ready(queue_db, limit=10)

    # The task returned "hello"; firm threw it away, so refreshing the result cannot work.
    with pytest.raises(NotImplementedError):
        result.refresh()


def test_supports_async_task_is_true(queue_db) -> None:
    assert task_backends["default"].supports_async_task is True


def test_aenqueue_takes_the_same_path(queue_db) -> None:
    asyncio.run(record.aenqueue("async-enqueue"))

    assert len(_rows(queue_db, schema.ready_executions)) == 1
    assert run_ready(queue_db, limit=10) == 1
    assert performed == ["async-enqueue"]


# --- validate_task refuses what firm cannot do -------------------------------------------


def test_takes_context_is_refused(queue_db) -> None:
    # Task.__post_init__ validates, so constructing the Task is the thing that has to fail.
    with pytest.raises(InvalidTask, match="takes_context"):
        Task(
            priority=0,
            func=_needs_context,
            backend="default",
            queue_name="default",
            run_after=None,
            takes_context=True,
        )


def test_a_queue_outside_queues_is_refused(queue_db) -> None:
    backend = task_backends["default"]
    original, backend.queues = backend.queues, {"default"}
    try:
        with pytest.raises(InvalidTask, match="not valid for backend"):
            record.using(queue_name="nope").enqueue("x")
    finally:
        backend.queues = original


def test_a_priority_outside_djangos_range_is_refused(queue_db) -> None:
    with pytest.raises(InvalidTask, match="whole number"):
        record.using(priority=1000).enqueue("x")


# --- OPTIONS ------------------------------------------------------------------------------


def test_enqueue_on_commit_defaults_to_true(queue_db) -> None:
    assert task_backends["default"].enqueue_on_commit is True


def test_database_alias_follows_the_firm_queue_setting(queue_db) -> None:
    backend = task_backends["default"]
    assert backend.database_alias() == "default"

    with override_settings(FIRM_QUEUE={"DATABASE_ALIAS": "other"}):
        assert backend.database_alias() == "other"  # read per enqueue, so no stale copy


def test_enqueue_on_commit_waits_for_djangos_commit(queue_db) -> None:
    with pytest.raises(RuntimeError), transaction.atomic():
        record.enqueue("rolled-back")
        assert _jobs(queue_db) == []  # nothing written yet
        raise RuntimeError("boom")

    assert _jobs(queue_db) == []  # ... and the rollback took the enqueue with it


def test_enqueue_on_commit_inserts_when_django_commits(queue_db) -> None:
    with transaction.atomic():
        record.enqueue("committed")
        assert _jobs(queue_db) == []

    assert len(_jobs(queue_db)) == 1


def test_enqueue_on_commit_false_writes_immediately(queue_db) -> None:
    assert task_backends["eager"].enqueue_on_commit is False

    with pytest.raises(RuntimeError), transaction.atomic():
        record.using(backend="eager").enqueue("survives")
        assert len(_jobs(queue_db)) == 1  # already committed, on firm's own connection
        raise RuntimeError("boom")

    assert len(_jobs(queue_db)) == 1  # Django's rollback cannot reach it


# --- the rest of the backend contract -----------------------------------------------------


def test_task_enqueued_signal_is_sent(queue_db) -> None:
    seen: list = []

    def receiver(sender, task_result, **kwargs) -> None:
        seen.append(task_result)

    task_enqueued.connect(receiver)
    try:
        result = record.enqueue("signal")
    finally:
        task_enqueued.disconnect(receiver)

    assert [r.id for r in seen] == [result.id]


def test_check_reports_an_unconfigured_firm(queue_db) -> None:
    backend = task_backends["default"]
    assert backend.check() == []

    set_runtime(None)
    try:
        (error,) = backend.check()
        assert error.id == "firm.E001"
        assert "configure" in error.hint
    finally:
        set_runtime(queue_db)
