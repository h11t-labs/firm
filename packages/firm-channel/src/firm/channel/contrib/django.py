"""Django integration — a ready-made ``Channel`` bound to your Django database.

    # settings.py (optional; every key has a default)
    FIRM_CHANNEL = {"POLLING_INTERVAL": 0.1}

    # anywhere
    from firm.channel.contrib.django import channel

    channel.subscribe("orders", handle_order)
    channel.broadcast("orders", b'{"order_id": 1, "event": "charged"}')

``channel`` is a lazy handle, in the shape of ``django.core.cache.cache``: it builds its
``Channel`` the first time you touch it, from ``DATABASES``, and rebinds if that database
changes underneath it (which is what ``manage.py test`` does). If firm-queue is configured
against the same database, it shares that engine instead of opening a second connection pool.

There is nothing to add to ``INSTALLED_APPS`` — this module has no startup work to do, so an
app config would be ceremony. For a ``Channel`` with settings this handle doesn't expose,
construct one yourself and pass ``engine=``.

Needs the ``[django]`` extra.
"""

from __future__ import annotations

from typing import Any

try:
    import django  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised only without the 'django' extra
    raise ImportError(
        'The firm Django channel integration requires "django". Install the django extra: '
        'pip install "firm-channel[django]"'
    ) from exc

from sqlalchemy import Engine

from firm._core.contrib.django_handle import LazyHandle

from ..channel import (
    DEFAULT_POLLING_INTERVAL,
    DEFAULT_TRIM_BATCH_SIZE,
    ONE_DAY_SECONDS,
    Channel,
)

#: ``settings.FIRM_CHANNEL``. Every key optional; the values mirror ``Channel``'s own defaults.
DEFAULTS: dict[str, Any] = {
    # Which DATABASES entry to follow, and an escape hatch to a database Django doesn't manage.
    "DATABASE_ALIAS": "default",
    "DATABASE_URL": None,
    "POLLING_INTERVAL": DEFAULT_POLLING_INTERVAL,
    "MESSAGE_RETENTION": ONE_DAY_SECONDS,
    "AUTO_TRIM": True,
    "TRIM_BATCH_SIZE": DEFAULT_TRIM_BATCH_SIZE,
    "CREATE_SCHEMA": True,
}


def _build(engine: Engine, conf: dict[str, Any]) -> Channel:
    return Channel(
        engine=engine,
        polling_interval=conf["POLLING_INTERVAL"],
        message_retention=conf["MESSAGE_RETENTION"],
        auto_trim=conf["AUTO_TRIM"],
        trim_batch_size=conf["TRIM_BATCH_SIZE"],
        create_schema=conf["CREATE_SCHEMA"],
    )


#: The process-wide channel. Attribute access resolves it; see :class:`LazyHandle`.
channel = LazyHandle(setting_name="FIRM_CHANNEL", defaults=DEFAULTS, factory=_build)

__all__ = ["DEFAULTS", "channel"]
