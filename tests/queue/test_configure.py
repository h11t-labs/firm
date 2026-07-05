"""Tests for firm.queue.config — the process-global configure() singleton."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, func, select

import firm.queue as bq
from firm.queue import config, schema
from firm.queue.worker import run_ready


@pytest.fixture(autouse=True)
def _clean_runtime() -> Iterator[None]:
    try:
        yield
    finally:
        config.set_runtime(None)


def test_current_runtime_requires_configure() -> None:
    config.set_runtime(None)
    with pytest.raises(RuntimeError, match="not configured"):
        bq.current_runtime()


def test_configure_requires_url_or_engine() -> None:
    with pytest.raises(ValueError):
        bq.configure()


def test_configure_with_shared_engine(engine: Engine) -> None:
    """configure(engine=...) reuses the caller's engine instead of building its own."""
    rt = bq.configure(engine=engine)
    assert rt.engine is engine
    assert bq.current_runtime() is rt

    @bq.job()
    def noop() -> None:
        pass

    job_id = noop.enqueue()
    assert job_id is not None
    assert run_ready(rt, queues=("*",), limit=10) == 1

    with engine.connect() as conn:
        finished = conn.execute(
            select(func.count())
            .select_from(schema.jobs)
            .where(schema.jobs.c.finished_at.is_not(None))
        ).scalar()
    assert finished == 1


def test_reset_keeps_shared_engine_usable(engine: Engine) -> None:
    """reset() (the post-fork hook) disposes the pool but keeps a caller-provided engine."""
    rt = bq.configure(engine=engine)
    rt.reset()
    assert rt.engine is engine
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(schema.jobs)).scalar() == 0


def test_configure_carries_queue_settings(db_url: str) -> None:
    rt = bq.configure(database_url=db_url, preserve_finished_jobs=False, pool_size=5)
    settings = rt.settings
    assert isinstance(settings, config.QueueSettings)
    assert settings.preserve_finished_jobs is False
    assert settings.pool_size == 5
    rt.reset()


def test_reset_close_false_drops_but_does_not_close_inherited_pool(monkeypatch, tmp_path) -> None:
    """A forked child must dispose the inherited pool with close=False: the pooled
    connections are the parent's live sockets (SQLAlchemy's post-fork recipe, Q-F5)."""
    from firm._core.config import Runtime, Settings

    url = f"sqlite:///{tmp_path / 'reset.db'}"
    rt = Runtime(Settings(database_url=url))
    engine = rt.engine  # build the lazy engine
    seen: dict[str, bool] = {}

    def _recording_dispose(close: bool = True) -> None:
        seen["close"] = close

    monkeypatch.setattr(engine, "dispose", _recording_dispose)
    rt.reset(close=False)
    assert seen["close"] is False

    # Genuine shutdown still closes: the connections are ours then.
    rt2 = Runtime(Settings(database_url=url))
    engine2 = rt2.engine
    monkeypatch.setattr(engine2, "dispose", _recording_dispose)
    rt2.reset()
    assert seen["close"] is True
