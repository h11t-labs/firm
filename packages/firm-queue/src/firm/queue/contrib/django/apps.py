"""The app config — the whole integration's entry point.

``AppConfig.ready()`` is the one hook Django guarantees runs exactly once per process, after
settings are loaded and before any view, management command or worker. That makes it the right
place to configure firm: ``runserver``, gunicorn/uwsgi workers, every ``manage.py`` command and
``manage.py firm_worker`` are covered by a single code path.

``post_migrate`` then re-points firm at whatever database Django actually ended up on and
creates firm's tables there. Re-pointing is what makes ``manage.py test`` correct: ``ready()``
runs before the test database exists, so without it the suite would write jobs into the
development database.
"""

from __future__ import annotations

from collections.abc import Iterator
from importlib import import_module
from typing import Any

from django.apps import AppConfig
from django.db.models.signals import post_migrate

from .conf import build_runtime, get_settings


class FirmQueueConfig(AppConfig):
    """``INSTALLED_APPS = [..., "firm.queue.contrib.django"]``."""

    name = "firm.queue.contrib.django"
    # Without this the label would be "django" (the last dotted component), which reads like
    # Django's own apps and would collide with any other third-party "<something>.django" app.
    label = "firm_queue"
    verbose_name = "firm queue"

    def ready(self) -> None:
        conf = get_settings()
        build_runtime(conf)
        _register_connection_middleware(conf)
        _import_job_modules(conf)
        # sender=self makes this fire exactly once per migrate/flush rather than once per
        # installed app; see models.py for why this app carries a models module at all.
        # dispatch_uid keeps a second ready() (django.setup() in tests, autoreloader) from
        # connecting a duplicate receiver.
        post_migrate.connect(
            _on_post_migrate, sender=self, dispatch_uid="firm.queue.contrib.django"
        )


def _import_job_modules(conf: dict[str, Any]) -> None:
    """Import the modules that hold ``@job`` definitions, so they land in the registry.

    Workers look jobs up by ``"module.qualname"``, so the enqueueing process and the worker
    process must both have imported them. Autodiscovery is the same mechanism (and the same
    helper) ``django.contrib.admin`` uses for ``admin.py``.
    """
    if conf["AUTODISCOVER"]:
        from django.utils.module_loading import autodiscover_modules

        autodiscover_modules(conf["JOBS_MODULE"])
    for module in _task_backend_modules():
        import_module(module)
    for module in conf["IMPORTS"] or ():
        import_module(module)


_MIDDLEWARE_REGISTERED = False


def _register_connection_middleware(conf: dict[str, Any]) -> None:
    """Close Django's ORM connections after every job, the way a request does.

    Django closes connections at request boundaries (``request_finished``). A worker has no
    requests, so without this a long-lived job thread keeps a connection open past
    ``CONN_MAX_AGE`` and eventually hands the job body one the server has already dropped.
    Doing it per job in user code works but is a ``try/finally`` in every job that touches the
    ORM — this is exactly the boilerplate the app config exists to remove.

    Registered once per process: ``ready()`` can run more than once (``django.setup()`` in
    tests, the autoreloader), and middleware has no de-duplication of its own.
    """
    global _MIDDLEWARE_REGISTERED
    if _MIDDLEWARE_REGISTERED or not conf["CLOSE_CONNECTIONS"]:
        return

    from django.db import close_old_connections

    from firm.queue.hooks import around_perform

    @around_perform
    def _close_django_connections(_execution: Any) -> Iterator[None]:
        try:
            yield
        finally:
            close_old_connections()

    _MIDDLEWARE_REGISTERED = True


def _task_backend_modules() -> set[str]:
    """Modules of any ``TASKS`` backend that is ours, so the worker can resolve those tasks.

    Every ``django.tasks`` task runs as one firm job (``run_task``) registered by the backend
    module. An enqueueing process imports it for free — Django instantiates the backend — but
    ``firm_worker`` never touches ``TASKS``, so without this it would fail to resolve them. The
    alternative is making the user restate the backend under ``FIRM_QUEUE["IMPORTS"]``, which
    is exactly the boilerplate this app exists to remove.
    """
    from django.conf import settings

    modules = set()
    for config in getattr(settings, "TASKS", {}).values():
        path = config.get("BACKEND", "")
        if path.startswith("firm.") and "." in path:
            modules.add(path.rsplit(".", 1)[0])
    return modules


def _on_post_migrate(*, using: str, **_: Any) -> None:
    """Create firm's schema in the database ``manage.py migrate`` just touched.

    ``using`` is the ``--database`` alias, so a migrate aimed at another connection is left
    alone. ``create_all`` is idempotent (it creates only missing tables) and stamps firm's
    Alembic version table at head, so a later ``alembic upgrade head`` is still a clean no-op.
    """
    conf = get_settings()
    if using != conf["DATABASE_ALIAS"]:
        return
    runtime = build_runtime(conf)
    if conf["CREATE_SCHEMA"]:
        from firm.queue import schema

        schema.create_all(runtime.engine)
