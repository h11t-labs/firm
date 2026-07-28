"""Specs for the Django app: ``INSTALLED_APPS = [..., "firm.queue.contrib.django"]``.

The ``django_project`` fixture (see conftest) boots a real Django with this app installed, so
these exercise the actual hooks — ``AppConfig.ready()``, the ``post_migrate`` receiver,
``manage.py firm_worker``, and ``transaction.on_commit`` — rather than calling the functions
behind them.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from sqlalchemy import func, inspect, select

pytest.importorskip("django")

from typing import Any, cast

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connections, transaction
from django.test import override_settings

from firm._core.contrib.django import sqlalchemy_url_for
from firm._core.database import create_engine_for
from firm.queue import schema
from firm.queue.config import current_runtime, set_runtime
from firm.queue.contrib.django import enqueue_on_commit
from firm.queue.contrib.django.apps import _task_backend_modules
from firm.queue.contrib.django.conf import get_settings
from firm.queue.registry import REGISTRY


def _app_config():
    return apps.get_app_config("firm_queue")


def _ready_count() -> int:
    with current_runtime().engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(schema.ready_executions)).scalar() or 0


# --- AppConfig.ready() ---------------------------------------------------------------------


def test_ready_configures_firm_from_databases(django_env) -> None:
    expected = sqlalchemy_url_for(connections["default"].settings_dict)
    assert current_runtime().settings.database_url == expected


def test_database_alias_setting_selects_another_connection(django_project) -> None:
    with override_settings(FIRM_QUEUE={"DATABASE_ALIAS": "other"}):
        _app_config().ready()
        try:
            assert current_runtime().settings.database_url.endswith("other.db")
        finally:
            set_runtime(None)


def test_explicit_database_url_bypasses_databases(django_project, tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'own.db'}"
    with override_settings(FIRM_QUEUE={"DATABASE_URL": url}):
        _app_config().ready()
        try:
            assert current_runtime().settings.database_url == url
        finally:
            set_runtime(None)


def test_engine_knobs_are_passed_through(django_project) -> None:
    with override_settings(FIRM_QUEUE={"POOL_SIZE": 5, "MAX_OVERFLOW": 7, "DEFAULT_QUEUE": "bulk"}):
        _app_config().ready()
        try:
            settings = current_runtime().settings
            assert (settings.pool_size, settings.max_overflow) == (5, 7)
            assert settings.default_queue == "bulk"
        finally:
            set_runtime(None)


def test_unset_knobs_keep_firms_own_defaults(django_env) -> None:
    # The settings block stores None for these, so configure()'s signature stays the one
    # place that defines them.
    assert current_runtime().settings.pool_size == 20


def test_ready_is_idempotent_and_reuses_the_runtime(django_env) -> None:
    """post_migrate re-runs this on every migrate *and* flush; rebuilding the engine each time
    would leak a connection pool."""
    first = current_runtime()
    _app_config().ready()
    assert current_runtime() is first


def test_unknown_settings_key_is_rejected(django_project) -> None:
    with (
        override_settings(FIRM_QUEUE={"DATABASE_ALIS": "default"}),
        pytest.raises(ImproperlyConfigured, match="DATABASE_ALIS"),
    ):
        _app_config().ready()


def test_unknown_database_alias_is_rejected(django_project) -> None:
    with (
        override_settings(FIRM_QUEUE={"DATABASE_ALIAS": "nope"}),
        pytest.raises(ImproperlyConfigured, match="nope"),
    ):
        _app_config().ready()


def test_unmappable_backend_becomes_improperly_configured(django_project, monkeypatch) -> None:
    from firm.queue.contrib.django import conf

    def _boom(_settings_dict):
        raise ValueError("firm has no URL mapping for 'django.db.backends.exotic'.")

    monkeypatch.setattr(conf, "sqlalchemy_url_for", _boom)
    with pytest.raises(ImproperlyConfigured, match="DATABASE_URL"):
        conf.database_url(get_settings())


# --- Finding the jobs ----------------------------------------------------------------------


def test_app_jobs_modules_are_autodiscovered(django_env) -> None:
    # firm_demo/jobs.py is never imported by the test suite; the app config found it.
    assert "firm_demo.jobs.demo_job" in REGISTRY


def test_imports_setting_loads_modules_outside_any_app(django_project) -> None:
    with override_settings(FIRM_QUEUE={"IMPORTS": ["firm_extra_jobs"]}):
        _app_config().ready()
        try:
            assert "firm_extra_jobs.extra_job" in REGISTRY
        finally:
            set_runtime(None)


def test_our_tasks_backend_is_imported_without_being_declared(django_project) -> None:
    """A worker never reads TASKS, so the backend's `run_task` has to reach the registry some
    other way. Making the user restate it under IMPORTS is exactly the boilerplate this app
    exists to remove — so the app config derives it from TASKS."""
    with override_settings(
        TASKS={"default": {"BACKEND": "firm.queue.contrib.django.backend.FirmBackend"}}
    ):
        assert _task_backend_modules() == {"firm.queue.contrib.django.backend"}
        _app_config().ready()
        try:
            assert "firm.queue.contrib.django.backend.run_task" in REGISTRY
        finally:
            set_runtime(None)


def test_a_foreign_tasks_backend_is_left_alone(django_project) -> None:
    """Only `firm.` backends are ours to import; someone else's backend is their business."""
    with override_settings(TASKS={"default": {"BACKEND": "django.tasks.backends.dummy.Dummy"}}):
        assert _task_backend_modules() == set()
        _app_config().ready()  # must not raise
        set_runtime(None)


# --- post_migrate --------------------------------------------------------------------------


def test_migrate_creates_firms_tables(django_env) -> None:
    call_command("migrate", verbosity=0)
    assert inspect(current_runtime().engine).has_table("firm_queue_jobs")


def test_migrate_stamps_alembic_so_upgrades_stay_no_ops(django_queue) -> None:
    assert inspect(current_runtime().engine).has_table(schema.VERSION_TABLE)


def test_migrate_is_idempotent(django_queue) -> None:
    call_command("migrate", verbosity=0)  # would raise "table already exists" if it weren't
    assert inspect(current_runtime().engine).has_table("firm_queue_jobs")


def test_create_schema_false_leaves_the_database_alone(django_project, tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'virgin.db'}"
    with override_settings(FIRM_QUEUE={"DATABASE_URL": url, "CREATE_SCHEMA": False}):
        _app_config().ready()
        try:
            call_command("migrate", verbosity=0)
            assert not inspect(current_runtime().engine).has_table("firm_queue_jobs")
        finally:
            set_runtime(None)


def test_migrate_database_flag_decides_which_database_gets_the_schema(django_project) -> None:
    """`migrate --database` arrives as the signal's ``using``; firm acts only on its own alias.

    Both halves live in one spec because they are one claim about the same database, and the
    order matters: "not created" is only meaningful before the run that creates it.
    """
    other_engine = create_engine_for(sqlalchemy_url_for(connections["other"].settings_dict))
    try:
        # firm follows "default" here, so a migrate aimed at "other" is not ours to act on.
        with override_settings(FIRM_QUEUE={}):
            _app_config().ready()
            try:
                call_command("migrate", database="other", verbosity=0)
                assert not inspect(other_engine).has_table("firm_queue_jobs")
            finally:
                set_runtime(None)

        with override_settings(FIRM_QUEUE={"DATABASE_ALIAS": "other"}):
            _app_config().ready()
            try:
                call_command("migrate", database="other", verbosity=0)
                assert inspect(other_engine).has_table("firm_queue_jobs")
            finally:
                set_runtime(None)
    finally:
        other_engine.dispose()


# --- manage.py firm_worker -----------------------------------------------------------------


class _FakeSupervisor:
    """Records what the command asked for, without starting anything."""

    last: _FakeSupervisor | None = None

    def __init__(self, runtime, config) -> None:
        self.runtime, self.config = runtime, config
        self.started = self.stopped = False
        _FakeSupervisor.last = self

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _interrupt(_seconds: float) -> None:
    raise KeyboardInterrupt


def test_firm_worker_thread_mode_starts_and_stops(django_queue, monkeypatch) -> None:
    from firm.queue.contrib.django.management.commands import firm_worker as command

    monkeypatch.setattr(command, "ThreadSupervisor", _FakeSupervisor)
    monkeypatch.setattr(command.time, "sleep", _interrupt)

    call_command("firm_worker", "--mode", "thread", "--queues", "billing,default", "--threads", "2")

    supervisor = _FakeSupervisor.last
    assert supervisor is not None
    assert supervisor.started and supervisor.stopped
    worker = supervisor.config.workers[0]
    assert worker.queues == ("billing", "default")
    assert worker.threads == 2


def test_firm_worker_really_runs_a_supervisor(django_queue, monkeypatch) -> None:
    """The same path with the real ThreadSupervisor: threads start, and stopping deregisters
    the process row rather than leaving a stale one behind."""
    from firm.queue.contrib.django.management.commands import firm_worker as command

    monkeypatch.setattr(command.time, "sleep", _interrupt)
    call_command("firm_worker", "--mode", "thread", "--threads", "1")

    with current_runtime().engine.connect() as conn:
        alive = conn.execute(select(func.count()).select_from(schema.processes)).scalar()
    assert alive == 0


def test_firm_worker_defaults_to_fork_and_closes_djangos_connections(
    django_queue, monkeypatch
) -> None:
    from firm.queue.contrib.django.management.commands import firm_worker as command

    closed: list[bool] = []
    monkeypatch.setattr(command, "ForkSupervisor", _FakeSupervisor)
    monkeypatch.setattr(
        command, "connections", type("_Stub", (), {"close_all": lambda self: closed.append(True)})()
    )

    call_command("firm_worker")  # no --mode: fork is the default, as in `firm-queue start`

    supervisor = _FakeSupervisor.last
    assert supervisor is not None and supervisor.started
    assert closed == [True]
    assert supervisor.config.workers[0].queues == ("*",)
    assert supervisor.config.workers[0].threads == 3


def test_firm_worker_flags_fall_back_to_settings(django_queue, monkeypatch) -> None:
    from firm.queue.contrib.django.management.commands import firm_worker as command

    monkeypatch.setattr(command, "ThreadSupervisor", _FakeSupervisor)
    monkeypatch.setattr(command.time, "sleep", _interrupt)

    with override_settings(FIRM_QUEUE={"QUEUES": "reports", "THREADS": 8, "MODE": "thread"}):
        call_command("firm_worker")

    supervisor = _FakeSupervisor.last
    assert supervisor is not None
    assert supervisor.config.workers[0].queues == ("reports",)
    assert supervisor.config.workers[0].threads == 8


def test_firm_worker_import_flag_loads_a_module(django_queue, monkeypatch) -> None:
    from firm.queue.contrib.django.management.commands import firm_worker as command

    imported: list[str] = []
    monkeypatch.setattr(command, "ThreadSupervisor", _FakeSupervisor)
    monkeypatch.setattr(command.time, "sleep", _interrupt)
    monkeypatch.setattr(command, "import_module", lambda name: imported.append(name))

    call_command("firm_worker", "--mode", "thread", "--import", "firm_extra_jobs")
    assert imported == ["firm_extra_jobs"]


# --- Enqueueing inside a Django transaction ------------------------------------------------


def test_enqueue_on_commit_waits_for_the_commit(django_queue) -> None:
    from firm_demo.jobs import demo_job

    before = _ready_count()
    with transaction.atomic():
        enqueue_on_commit(demo_job, 1)
        assert _ready_count() == before  # nothing written yet
    assert _ready_count() == before + 1


def test_enqueue_on_commit_is_dropped_on_rollback(django_queue) -> None:
    from firm_demo.jobs import demo_job

    before = _ready_count()
    with pytest.raises(RuntimeError), transaction.atomic():
        enqueue_on_commit(demo_job, 2)
        raise RuntimeError("boom")
    assert _ready_count() == before


def test_enqueue_on_commit_outside_a_transaction_runs_immediately(django_queue) -> None:
    """Django runs an on_commit callback straight away when there is no open transaction, so
    the helper is safe to use unconditionally."""
    from firm_demo.jobs import demo_job

    before = _ready_count()
    enqueue_on_commit(demo_job, 3)
    assert _ready_count() == before + 1


def test_nested_atomic_still_waits_for_the_outermost_commit(django_queue) -> None:
    from firm_demo.jobs import demo_job

    before = _ready_count()
    with transaction.atomic():
        with transaction.atomic():
            enqueue_on_commit(demo_job, 4)
        assert _ready_count() == before
    assert _ready_count() == before + 1


# --- Importing this package must never require Django ---------------------------------------


def test_the_package_imports_without_django_installed() -> None:
    """``import firm.queue.contrib.django`` has to survive in a process that has no Django — it
    shares the ``firm.queue.contrib`` namespace with the Flask and FastAPI helpers. Every Django
    import lives inside a function, or in the modules only Django itself imports (apps.py, the
    management command).

    Run out-of-process with ``django`` poisoned, because this session has it imported already.
    """
    script = (
        "import sys; sys.modules['django'] = None\n"
        "import firm.queue.contrib.django as m\n"
        "assert m.enqueue_on_commit and m.sqlalchemy_url_for and m.DEFAULTS\n"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no external input
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


# --- connection middleware -------------------------------------------------------------------


def _fake_execution():
    from firm.queue.hooks import Execution

    return Execution(job=cast(Any, object()), job_id=1, attempts=1)


def test_django_connections_are_closed_after_every_job(django_queue) -> None:
    """The app config registers the middleware, so a job body needs no try/finally of its own.

    Asserts the effect rather than the call: with the default CONN_MAX_AGE of 0,
    ``close_old_connections()`` drops the connection outright, so an open one becoming None is
    the observable behaviour a worker depends on.
    """
    from firm.queue.hooks import HOOKS

    connections["default"].ensure_connection()
    assert connections["default"].connection is not None

    with HOOKS.around_perform(_fake_execution()):
        pass

    assert connections["default"].connection is None, (
        "app config did not register the connection middleware"
    )


def test_connections_are_closed_even_when_the_job_raised(django_queue) -> None:
    """A failing job leaks a connection per failure without this."""
    from firm.queue.hooks import HOOKS

    connections["default"].ensure_connection()
    with pytest.raises(ValueError), HOOKS.around_perform(_fake_execution()):
        raise ValueError("job body blew up")

    assert connections["default"].connection is None


def test_close_connections_false_leaves_them_alone(django_project) -> None:
    import firm.queue.contrib.django.apps as apps_module
    from firm.queue.hooks import HOOKS

    HOOKS.clear()
    apps_module._MIDDLEWARE_REGISTERED = False
    try:
        with override_settings(FIRM_QUEUE={"CLOSE_CONNECTIONS": False}):
            _app_config().ready()
            connections["default"].ensure_connection()
            with HOOKS.around_perform(_fake_execution()):
                pass
            assert connections["default"].connection is not None
    finally:
        connections["default"].close()
        set_runtime(None)
        HOOKS.clear()
        apps_module._MIDDLEWARE_REGISTERED = False
