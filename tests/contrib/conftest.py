"""Fixtures for the contrib (framework integration) tests."""

from __future__ import annotations

from collections.abc import Iterator

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
