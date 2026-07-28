"""Django integration — a ready-made ``AuditLog`` bound to your Django database.

    # settings.py (optional; every key has a default)
    FIRM_AUDIT = {"MAX_AGE": 90 * 24 * 3600}

    # anywhere
    from firm.audit.contrib.django import audit

    audit.record("invoice.paid", subject=invoice, actor=request.user, data={"cents": 4200})

``audit`` is a lazy handle, in the shape of ``django.core.cache.cache``: it builds its
``AuditLog`` the first time you touch it, from ``DATABASES``, and rebinds if that database
changes underneath it (which is what ``manage.py test`` does). If firm-queue is configured
against the same database, it shares that engine instead of opening a second connection pool.

**Background work belongs in one process, not in every web worker.** ``BACKGROUND_RETENTION``,
``BACKGROUND_SEALING`` and ``BACKGROUND_VERIFICATION`` each start a thread *per process* that
touches this handle — with four gunicorn workers that is four schedulers competing over the same
rows. Leave them off here (the default) and run them somewhere singular: a management command,
a recurring firm job, or cron. This handle deliberately exposes only the settings that are safe
to have in every process; for anything else, construct an ``AuditLog`` yourself and pass
``engine=``.

Needs the ``[django]`` extra.
"""

from __future__ import annotations

from typing import Any

try:
    import django  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised only without the 'django' extra
    raise ImportError(
        'The firm Django audit integration requires "django". Install the django extra: '
        'pip install "firm-audit[django]"'
    ) from exc

from sqlalchemy import Engine

from firm._core.contrib.django_handle import LazyHandle

from ..log import AuditLog

#: ``settings.FIRM_AUDIT``. Every key optional; values mirror ``AuditLog``'s own defaults.
DEFAULTS: dict[str, Any] = {
    # Which DATABASES entry to follow, and an escape hatch to a database Django doesn't manage.
    "DATABASE_ALIAS": "default",
    "DATABASE_URL": None,
    "CREATE_SCHEMA": True,
    # None keeps every event forever; a float is a retention window in seconds. Pruning still
    # has to be triggered — see the note about background work in the module docstring.
    "MAX_AGE": None,
    # Tamper evidence. Keep the keys out of the settings file itself (read them from the
    # environment or your secret store); a MAC key in version control evidences nothing.
    "MAC_KEY": None,
    "SEAL_KEY": None,
}


def _build(engine: Engine, conf: dict[str, Any]) -> AuditLog:
    return AuditLog(
        engine=engine,
        create_schema=conf["CREATE_SCHEMA"],
        max_age=conf["MAX_AGE"],
        mac_key=conf["MAC_KEY"],
        seal_key=conf["SEAL_KEY"],
    )


#: The process-wide audit log. Attribute access resolves it; see :class:`LazyHandle`.
audit = LazyHandle(setting_name="FIRM_AUDIT", defaults=DEFAULTS, factory=_build)

__all__ = ["DEFAULTS", "audit"]
