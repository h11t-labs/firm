"""Shared fixtures for the cache tests.

Runs against SQLite by default; also against live Postgres/MySQL when ``FIRM_TEST_PG_URL``
/ ``FIRM_TEST_MYSQL_URL`` are set (fresh schema per test).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from firm._core.database import create_engine_for
from firm.cache import Cache, schema


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
        return f"sqlite:///{tmp_path / 'cache.db'}"
    eng = create_engine_for(backend)
    schema.drop_all(eng)
    schema.create_all(eng)
    eng.dispose()
    return backend


@pytest.fixture
def is_sqlite(db_url: str) -> bool:
    return db_url.startswith("sqlite")


@pytest.fixture
def cache(db_url: str) -> Iterator[Cache]:
    store = Cache(database_url=db_url, max_size=None, max_age=None, auto_expire=False)
    try:
        yield store
    finally:
        store.close()
