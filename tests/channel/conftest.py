"""Shared fixtures for the channel tests.

Runs against SQLite by default; also against live Postgres/MySQL when ``FIRM_TEST_PG_URL``
/ ``FIRM_TEST_MYSQL_URL`` are set (fresh schema per test).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import func, select

from firm._core.database import create_engine_for
from firm.channel import Channel, schema


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
        return f"sqlite:///{tmp_path / 'channel.db'}"
    eng = create_engine_for(backend)
    schema.drop_all(eng)
    schema.create_all(eng)
    eng.dispose()
    return backend


@pytest.fixture
def channel(db_url: str) -> Iterator[Channel]:
    # Fast polling + no auto_trim keeps delivery tests quick and deterministic.
    ps = Channel(database_url=db_url, polling_interval=0.01, auto_trim=False)
    try:
        yield ps
    finally:
        ps.close()


@pytest.fixture
def wait_for() -> Callable[..., bool]:
    def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    return _wait_for


@pytest.fixture
def stored(channel: Channel) -> Callable[[], int]:
    """How many messages are durably committed to the backing table right now. Delivery tests
    that broadcast to an *unsubscribed* channel poll this instead of sleeping a fixed duration:
    once the row is visible the write has committed, so a later subscription genuinely exercises
    the "no replay of pre-subscribe messages" path rather than racing an uncommitted insert."""

    def _count() -> int:
        with channel.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(schema.messages)).scalar() or 0

    return _count
