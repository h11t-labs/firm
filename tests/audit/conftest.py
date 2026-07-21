"""Shared fixtures for the audit tests.

Runs against SQLite by default; also against live Postgres/MySQL when ``FIRM_TEST_PG_URL``
/ ``FIRM_TEST_MYSQL_URL`` are set (fresh schema per test).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from unittest.mock import patch

import pytest

from firm._core.database import create_engine_for
from firm.audit import AuditLog, schema


def _backend_params() -> list:
    params = [pytest.param("sqlite", id="sqlite")]
    if pg := os.environ.get("FIRM_TEST_PG_URL"):
        params.append(pytest.param(pg, id="postgres"))
    if my := os.environ.get("FIRM_TEST_MYSQL_URL"):
        params.append(pytest.param(my, id="mysql"))
    return params


@pytest.fixture(params=_backend_params())
def backend(request) -> str:
    return request.param


@pytest.fixture
def db_url(backend: str, tmp_path) -> str:
    if backend == "sqlite":
        return f"sqlite:///{tmp_path / 'audit.db'}"
    eng = create_engine_for(backend)
    schema.drop_all(eng)
    schema.create_all(eng)
    eng.dispose()
    return backend


@pytest.fixture
def is_sqlite(db_url: str) -> bool:
    return db_url.startswith("sqlite")


@pytest.fixture
def audit(db_url: str) -> Iterator[AuditLog]:
    log = AuditLog(database_url=db_url)
    try:
        yield log
    finally:
        log.close()


@pytest.fixture
def at_time() -> Callable[[datetime], AbstractContextManager[None]]:
    """Insert genuinely-old signed rows by controlling the *write* clock.

    Aging used to be faked by ``UPDATE``ing ``created_at`` after insert — but that is itself a
    MAC-invalidating edit, so now that retention re-verifies a range before pruning it ("retention
    only prunes what verifies") a mutated row reads as tampering and pruning refuses. The clean
    convention is to make the row old *at insert time*: ``events.append`` is the sole insert path
    and stamps ``created_at`` from :func:`now_utc`, feeding that same value into the row's MAC.
    Patching it makes the stored timestamp and the signed timestamp one past value, so the row is
    genuinely old and still verifies. The sealer and retention read the real clock, so a past-dated
    row is immediately eligible to seal (``created_at <= now - grace``) and expired against
    ``max_age``.

    Usage::

        with at_time(now_utc() - timedelta(hours=2)):
            audit.record("old")  # signed at, and stored with, the past timestamp
    """

    @contextmanager
    def _at(when: datetime) -> Iterator[None]:
        with patch("firm.audit.events.now_utc", lambda: when):
            yield

    return _at
