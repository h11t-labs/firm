"""The ``FIRM_QUEUE`` settings block, and the runtime it builds.

    # settings.py — every key is optional; this is the whole vocabulary
    FIRM_QUEUE = {
        "DATABASE_ALIAS": "default",
        "CREATE_SCHEMA": True,
    }

One namespaced dict rather than a dozen ``FIRM_QUEUE_*`` module-level names, which is where
third-party Django apps have settled. A value of ``None`` means "don't pass this to
:func:`firm.queue.configure`", so firm's own defaults stay the single source of truth and
cannot drift from a copy kept here.
"""

from __future__ import annotations

from typing import Any

from firm._core.config import Runtime
from firm._core.contrib.django import sqlalchemy_url_for
from firm.queue.config import configure, current_runtime

DEFAULTS: dict[str, Any] = {
    # Which DATABASES entry firm follows. The URL is derived from that *connection's*
    # settings_dict, not from settings.DATABASES, so firm follows Django onto the test
    # database during `manage.py test` instead of writing jobs into the development one.
    "DATABASE_ALIAS": "default",
    # Skip that derivation and point firm at a database Django does not manage. The alias
    # above still decides which `migrate --database` run creates firm's schema.
    "DATABASE_URL": None,
    # Create (and Alembic-stamp) firm's tables from `manage.py migrate`. Idempotent.
    "CREATE_SCHEMA": True,
    # Import "<app>.jobs" from every installed app at startup. Workers resolve jobs by
    # "module.qualname", so every process has to have imported the modules that define them;
    # this is what removes that import from the user's AppConfig.
    "AUTODISCOVER": True,
    "JOBS_MODULE": "jobs",
    # Extra modules to import for their @job definitions (jobs that live outside <app>/jobs.py).
    "IMPORTS": (),
    # Close Django's ORM connections after every job, the way request_finished does after a
    # request. Without it a worker thread keeps a connection past CONN_MAX_AGE and eventually
    # hands a job one the server already dropped. Turn off only to manage them yourself.
    "CLOSE_CONNECTIONS": True,
    # Engine knobs. Passed to firm.queue.configure() only when set; see its signature for the
    # defaults.
    "BUSY_TIMEOUT_MS": None,
    "POOL_SIZE": None,
    "MAX_OVERFLOW": None,
    "DEFAULT_QUEUE": None,
    "PRESERVE_FINISHED_JOBS": None,
    # Defaults for `manage.py firm_worker`; its flags override them.
    "QUEUES": None,
    "THREADS": None,
    "MODE": None,
}

# FIRM_QUEUE key -> firm.queue.configure() keyword.
_CONFIGURE_KWARGS = {
    "BUSY_TIMEOUT_MS": "busy_timeout_ms",
    "POOL_SIZE": "pool_size",
    "MAX_OVERFLOW": "max_overflow",
    "DEFAULT_QUEUE": "default_queue",
    "PRESERVE_FINISHED_JOBS": "preserve_finished_jobs",
}


def get_settings() -> dict[str, Any]:
    """Return ``DEFAULTS`` merged with ``settings.FIRM_QUEUE``.

    A key that isn't in ``DEFAULTS`` is a typo, and a silently-ignored typo here means firm
    quietly runs on the wrong database — so it raises instead.
    """
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    overrides = getattr(settings, "FIRM_QUEUE", None) or {}
    unknown = sorted(set(overrides) - set(DEFAULTS))
    if unknown:
        raise ImproperlyConfigured(
            f"Unknown FIRM_QUEUE setting(s): {', '.join(unknown)}. "
            f"Valid keys are: {', '.join(sorted(DEFAULTS))}."
        )
    return {**DEFAULTS, **overrides}


def database_url(conf: dict[str, Any]) -> str:
    """The SQLAlchemy URL firm should use, from ``DATABASE_URL`` or the Django connection."""
    explicit = conf["DATABASE_URL"]
    if explicit:
        return str(explicit)

    from django.core.exceptions import ImproperlyConfigured
    from django.db import connections
    from django.db.utils import ConnectionDoesNotExist

    alias = conf["DATABASE_ALIAS"]
    try:
        settings_dict = connections[alias].settings_dict
    except ConnectionDoesNotExist as exc:
        raise ImproperlyConfigured(
            f"FIRM_QUEUE['DATABASE_ALIAS'] is {alias!r}, which is not in DATABASES."
        ) from exc
    try:
        return sqlalchemy_url_for(settings_dict)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"{exc} Alternatively set FIRM_QUEUE['DATABASE_URL'] to point firm at a database "
            f"of its own."
        ) from exc


def build_runtime(conf: dict[str, Any]) -> Runtime:
    """Configure firm-queue from ``conf`` and return the process-global runtime.

    Reuses the existing runtime when the URL is unchanged. That matters because this runs
    again on every ``post_migrate`` — which ``flush`` also emits, once per test method in a
    ``TransactionTestCase`` — and rebuilding the engine each time would leak a connection pool.
    """
    url = database_url(conf)
    try:
        existing: Runtime | None = current_runtime()
    except RuntimeError:
        existing = None
    if existing is not None and existing.settings.database_url == url:
        return existing

    kwargs: dict[str, Any] = {
        keyword: conf[key] for key, keyword in _CONFIGURE_KWARGS.items() if conf[key] is not None
    }
    return configure(database_url=url, **kwargs)
