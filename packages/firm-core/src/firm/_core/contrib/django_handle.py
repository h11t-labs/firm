"""A module-level firm object that binds itself to Django's database on first use.

``firm-queue`` gets its wiring from an ``AppConfig``, because a queue needs one at startup
anyway (autodiscovery, ``post_migrate``, the worker command). ``firm-channel`` and
``firm-audit`` have no such startup work — asking a user to add a second and third entry to
``INSTALLED_APPS`` just to construct an object would be ceremony for its own sake.

So they follow the other Django idiom instead, the one ``django.core.cache.cache`` uses: a
module-level handle that resolves on first attribute access.

    from firm.channel.contrib.django import channel

    channel.broadcast("orders", b'{"id": 1}')

Two things this has to get right, and both are why it isn't a plain module-level global:

* **The test database.** ``manage.py test`` swaps ``DATABASES`` after import time. A global
  built at import would keep writing to the development database for the whole test run — the
  exact failure the URL derivation exists to prevent. The handle re-checks the URL on every
  access and rebinds when it changed.
* **One connection pool.** If firm-queue is already configured against the same URL, the handle
  reuses *its* engine rather than opening a second pool against the same database.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import Engine

from ..database import create_engine_for
from .django import sqlalchemy_url_for


def settings_block(name: str, defaults: Mapping[str, Any]) -> dict[str, Any]:
    """``defaults`` merged with ``settings.<name>``, rejecting keys that aren't in ``defaults``.

    A silently-ignored typo here means firm quietly runs with the wrong configuration — often
    against the wrong database — so an unknown key raises instead.
    """
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    overrides = getattr(settings, name, None) or {}
    unknown = sorted(set(overrides) - set(defaults))
    if unknown:
        raise ImproperlyConfigured(
            f"Unknown {name} setting(s): {', '.join(unknown)}. "
            f"Valid keys are: {', '.join(sorted(defaults))}."
        )
    return {**defaults, **overrides}


def database_url_for(conf: Mapping[str, Any], *, setting_name: str) -> str:
    """The URL from ``conf["DATABASE_URL"]``, or derived from the named Django connection."""
    explicit = conf.get("DATABASE_URL")
    if explicit:
        return str(explicit)

    from django.core.exceptions import ImproperlyConfigured
    from django.db import connections

    alias = conf.get("DATABASE_ALIAS", "default")
    try:
        connection = connections[alias]
    except Exception as exc:
        raise ImproperlyConfigured(
            f"{setting_name}['DATABASE_ALIAS'] is {alias!r}, which is not in DATABASES."
        ) from exc
    try:
        # settings_dict, not settings.DATABASES: it already reflects the test-database swap.
        return sqlalchemy_url_for(connection.settings_dict)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"{exc} Set {setting_name}['DATABASE_URL'] to point firm at it directly."
        ) from exc


def shared_engine(url: str) -> Engine | None:
    """firm-queue's engine, if it is configured against this same URL.

    Sharing one pool is the point: a Django process that runs jobs, pub/sub and an audit log
    should hold one set of connections to its database, not three. Returns ``None`` when
    firm-queue isn't installed or is pointed somewhere else, and the caller opens its own.
    """
    try:
        from firm.queue.config import current_runtime
    except ImportError:  # firm-queue isn't installed; nothing to share
        return None
    try:
        runtime = current_runtime()
    except RuntimeError:  # installed but not configured in this process
        return None
    return runtime.engine if runtime.settings.database_url == url else None


class LazyHandle:
    """Proxies attribute access to an object built on demand, rebuilt when the URL changes.

    ``factory`` receives ``(engine, conf)`` and returns the object to proxy to. It is given an
    engine rather than a URL so every module goes through :func:`shared_engine` identically.
    """

    def __init__(
        self,
        *,
        setting_name: str,
        defaults: Mapping[str, Any],
        factory: Callable[[Engine, dict[str, Any]], Any],
    ) -> None:
        # Set through __dict__ before anything else: __getattr__ below resolves the handle, so
        # an attribute missing here would recurse instead of raising.
        self.__dict__["_setting_name"] = setting_name
        self.__dict__["_defaults"] = dict(defaults)
        self.__dict__["_factory"] = factory
        self.__dict__["_instance"] = None
        self.__dict__["_url"] = None

    def _resolve(self) -> Any:
        conf = settings_block(self._setting_name, self._defaults)
        url = database_url_for(conf, setting_name=self._setting_name)
        if self._instance is None or self._url != url:
            engine = shared_engine(url) or create_engine_for(url)
            self.__dict__["_instance"] = self._factory(engine, conf)
            self.__dict__["_url"] = url
        return self._instance

    def __getattr__(self, item: str) -> Any:
        return getattr(self._resolve(), item)

    def __repr__(self) -> str:
        bound = self.__dict__["_url"]
        state = f"bound to {bound}" if bound else "unbound"
        return f"<LazyHandle {self._setting_name} ({state})>"
