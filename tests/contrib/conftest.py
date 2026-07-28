"""Fixtures for the contrib (framework integration) tests."""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

import pytest

from firm._core.config import Runtime
from firm.queue import schema
from firm.queue.config import configure, set_runtime


@pytest.fixture
def queue_db(tmp_path) -> Iterator[Runtime]:
    """A configured queue runtime with the schema created (sets the process-global runtime)."""
    rt = configure(database_url=f"sqlite:///{tmp_path / 'q.db'}")
    schema.create_all(rt.engine)
    try:
        yield rt
    finally:
        set_runtime(None)


@pytest.fixture
def queue_url(tmp_path) -> Iterator[str]:
    """A URL for a queue DB whose schema already exists; leaves the global runtime unset so the
    integration under test does its own configure()."""
    url = f"sqlite:///{tmp_path / 'q.db'}"
    rt = configure(database_url=url)
    schema.create_all(rt.engine)
    set_runtime(None)
    try:
        yield url
    finally:
        set_runtime(None)


# --- Django -------------------------------------------------------------------------------
#
# There is no pytest-django here, so this does by hand what it would: settings.configure() plus
# django.setup(). Both are process-global and one-shot, hence session scope — and other Django
# spec modules in this directory have to configure settings at *import* time (a settings-less
# ``@task`` cannot even be declared), so this fixture extends an existing configuration rather
# than assuming it owns it.

_DEMO_APP_JOBS = '''\
"""A `<app>/jobs.py` for the app config to autodiscover."""

import firm.queue as bq


@bq.job(queue="demo")
def demo_job(x: int) -> int:
    return x
'''

_EXTRA_JOBS = '''\
"""Jobs outside any app, reachable only via FIRM_QUEUE["IMPORTS"]."""

import firm.queue as bq


@bq.job(queue="demo")
def extra_job() -> None:
    pass
'''


def _write_demo_project(base: Path) -> None:
    app = base / "firm_demo"
    app.mkdir()
    (app / "__init__.py").write_text('"""A minimal installed app."""\n')
    (app / "jobs.py").write_text(textwrap.dedent(_DEMO_APP_JOBS))
    (base / "firm_extra_jobs.py").write_text(textwrap.dedent(_EXTRA_JOBS))


def _install_databases(databases: dict) -> dict:
    """Point Django at ``databases`` and return the previous setting.

    Django has no ``setting_changed`` receiver for ``DATABASES`` (which is why
    ``override_settings(DATABASES=...)`` only warns), so the connection handler's cached copy
    has to be dropped by hand, along with any connection already opened from it.
    """
    from django.conf import settings
    from django.db import connections

    previous = settings.DATABASES
    settings.DATABASES = databases
    connections.close_all()
    for alias in list(connections.settings):
        with suppress(AttributeError):  # never opened
            del connections[alias]
    connections._settings = None
    connections.__dict__.pop("settings", None)  # the cached_property
    return previous


@pytest.fixture(scope="session")
def django_project(tmp_path_factory) -> Iterator[Path]:
    """A booted Django with two SQLite aliases, ``firm.queue.contrib.django`` in ``INSTALLED_APPS``,
    and a tiny ``firm_demo`` app whose ``jobs`` module the integration should discover by itself.

    Installing the app runs ``AppConfig.ready()``, which sets firm's process-global runtime; it
    is cleared again here so no unrelated test inherits it.
    """
    django = pytest.importorskip("django")
    from django.apps import apps
    from django.conf import settings

    base = tmp_path_factory.mktemp("django_project")
    _write_demo_project(base)
    sys.path.insert(0, str(base))
    databases = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(base / "django.db")},
        "other": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(base / "other.db")},
    }

    if settings.configured:
        previous_databases: dict | None = _install_databases(databases)
    else:
        previous_databases = None
        settings.configure(
            DEBUG=True,
            SECRET_KEY="test-only-not-a-secret",
            USE_TZ=True,
            INSTALLED_APPS=[],
            DATABASES=databases,
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        )
        django.setup()

    settings.FIRM_QUEUE = {}
    previous_apps = settings.INSTALLED_APPS
    settings.INSTALLED_APPS = [*previous_apps, "firm_demo", "firm.queue.contrib.django"]
    apps.set_installed_apps(settings.INSTALLED_APPS)  # repopulates the registry, runs ready()
    set_runtime(None)
    try:
        yield base
    finally:
        apps.unset_installed_apps()
        settings.INSTALLED_APPS = previous_apps
        if previous_databases is not None:
            _install_databases(previous_databases)
        sys.path.remove(str(base))
        set_runtime(None)


@pytest.fixture
def django_env(django_project) -> Iterator[Path]:
    """Re-run ``ready()`` the way ``django.setup()`` does, then drop the global runtime again."""
    from django.apps import apps

    apps.get_app_config("firm_queue").ready()
    try:
        yield django_project
    finally:
        set_runtime(None)


@pytest.fixture
def django_queue(django_env) -> Iterator[Path]:
    """``django_env`` plus a ``manage.py migrate``, so firm's tables exist."""
    from django.core.management import call_command

    call_command("migrate", verbosity=0)
    yield django_env
