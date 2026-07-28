"""Django integration — build firm's SQLAlchemy URL from a Django ``DATABASES`` entry.

    from django.db import connection

    import firm.queue as bq
    from firm._core.contrib.django import sqlalchemy_url_for

    bq.configure(database_url=sqlalchemy_url_for(connection.settings_dict))

Deriving the URL is what keeps firm pointed at the *same* database as Django. A hardcoded URL
works right up until ``manage.py test``, at which point Django swaps in a test database and firm
happily keeps writing to the real one.

This lives in firm-core because firm-queue and firm-cache both need it, and it is a pure
``dict -> str`` mapping — nothing here imports Django, so firm-core keeps its "no framework
dependencies" property. The input is anything shaped like ``settings.DATABASES["default"]``;
``connection.settings_dict`` is the same mapping with Django's defaults filled in, and is the
better source because it already reflects the test-database swap.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import URL

from ..database import normalize_url

# Django ENGINE -> SQLAlchemy backend name. The driver suffix (+psycopg / +pymysql) is left to
# normalize_url, so firm has exactly one place that decides which drivers it ships.
_BACKENDS = {
    "django.db.backends.postgresql": "postgresql",
    "django.db.backends.postgresql_psycopg2": "postgresql",  # pre-4.0 spelling, still common
    "django.db.backends.mysql": "mysql",
    "django.db.backends.sqlite3": "sqlite",
}

# OPTIONS keys Django's own backend pops in get_connection_params() and never passes to the
# driver. Forwarding them would turn into bogus connect() kwargs, so they are dropped here too.
_DJANGO_ONLY_OPTIONS = {
    "postgresql": frozenset({"assume_role", "isolation_level", "pool", "server_side_binding"}),
    "mysql": frozenset({"isolation_level"}),
}


def sqlalchemy_url_for(db_settings: Mapping[str, Any]) -> str:
    """Translate one Django database config into a URL that :func:`~.database.create_engine_for`
    accepts.

    Handles the parts that bite: an empty ``HOST`` (connect over the local unix socket), a
    ``HOST`` that *is* a socket path, an empty ``PORT``, passwords containing characters that
    have to be percent-encoded, a ``NAME`` given as a :class:`~pathlib.Path`, and ``OPTIONS``.

    ``OPTIONS`` are the driver's ``connect()`` keyword arguments, and SQLAlchemy hands a URL's
    query string to the driver the same way, so scalar entries map across one for one
    (``{"sslmode": "require"}`` becomes ``?sslmode=require``). Two exceptions: MySQL's nested
    ``ssl`` dict is flattened to the ``ssl_<name>`` arguments SQLAlchemy's MySQL dialect
    re-assembles it from, and SQLite's ``OPTIONS`` are ignored entirely — firm configures the
    SQLite connection itself (WAL, ``busy_timeout``, transaction control) in ``create_engine_for``.

    Raises :class:`ValueError` for anything that has no faithful URL spelling, rather than
    silently connecting somewhere else.
    """
    engine = db_settings.get("ENGINE") or ""
    backend = _BACKENDS.get(engine)
    if backend is None:
        supported = ", ".join(sorted(_BACKENDS))
        raise ValueError(
            f"firm has no URL mapping for the Django database backend {engine!r}. "
            f"Supported ENGINE values are: {supported}. For any other backend, configure firm "
            f"with an explicit database_url=, or hand it a pre-built engine=."
        )
    if backend == "sqlite":
        return _sqlite_url(db_settings)
    return _server_url(backend, db_settings)


def _sqlite_url(db_settings: Mapping[str, Any]) -> str:
    # NAME is usually a Path (BASE_DIR / "db.sqlite3"), and SQLAlchemy renders an absolute path
    # as the four-slash form for us.
    name = str(db_settings.get("NAME") or "")
    if not name:
        raise ValueError("This Django database config has no NAME; firm needs a SQLite path.")
    if name == ":memory:":
        # Every connection to an anonymous in-memory database gets its *own* database, so this
        # is only useful for an engine firm owns end to end — it can never see Django's rows.
        return "sqlite://"
    if "mode=memory" in name:
        # Django's SQLite test database (file:memorydb_default?mode=memory&cache=shared) is
        # shared per-process, so a worker process could not reach it and the URI's query string
        # has no faithful spelling here either.
        raise ValueError(
            "firm cannot share Django's in-memory SQLite test database. Give it a file: set "
            "DATABASES['default']['TEST'] = {'NAME': BASE_DIR / 'test.db'}."
        )
    return normalize_url(URL.create("sqlite", database=name).render_as_string(hide_password=False))


def _server_url(backend: str, db_settings: Mapping[str, Any]) -> str:
    name = str(db_settings.get("NAME") or "")
    host = str(db_settings.get("HOST") or "")
    port = str(db_settings.get("PORT") or "").strip()
    query = _query_from_options(backend, db_settings.get("OPTIONS") or {})

    if not name and not (backend == "postgresql" and "service" in query):
        raise ValueError(
            "This Django database config has no NAME, so firm cannot tell which database to "
            "connect to. Set NAME (or, on PostgreSQL, OPTIONS['service'])."
        )

    # An empty HOST means "the driver's default local connection" — a unix socket for both
    # backends — and a HOST that is a filesystem path names that socket explicitly. Neither
    # belongs in a URL netloc, so the socket travels as a driver argument instead.
    if host.startswith("/"):
        query["host" if backend == "postgresql" else "unix_socket"] = host
        host = ""

    url = URL.create(
        backend,
        # URL.create percent-encodes these, so passwords with @ : / # survive the round trip.
        # A PASSWORD without a USER cannot be spelled in a URL and is dropped.
        username=db_settings.get("USER") or None,
        password=db_settings.get("PASSWORD") or None,
        host=host or None,
        port=int(port) if port else None,
        database=name or None,
        query=query,
    )
    return normalize_url(url.render_as_string(hide_password=False))


def _query_from_options(backend: str, options: Mapping[str, Any]) -> dict[str, str]:
    query: dict[str, str] = {}
    for key, value in options.items():
        if key in _DJANGO_ONLY_OPTIONS[backend]:
            continue
        if backend == "mysql" and key == "ssl" and isinstance(value, Mapping):
            # Django spells MySQL's TLS settings as a nested dict; SQLAlchemy's MySQL dialect
            # rebuilds exactly that dict from flat ssl_<name> query arguments.
            for ssl_key, ssl_value in value.items():
                query[f"ssl_{ssl_key}"] = _as_query_value(f"ssl.{ssl_key}", ssl_value)
            continue
        query[key] = _as_query_value(key, value)
    return query


def _as_query_value(key: str, value: Any) -> str:
    if isinstance(value, bool):  # before int: bool is an int, and drivers want true/false
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    raise ValueError(
        f"Django OPTIONS[{key!r}] is a {type(value).__name__}, which has no representation in a "
        f"database URL. Remove it and configure firm with an explicit database_url=, or build "
        f"the connection yourself and hand firm the resulting engine=."
    )
