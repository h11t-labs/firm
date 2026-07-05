"""Specs for enqueue_after_commit."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import firm.queue as bq
from firm.contrib.sqlalchemy import enqueue_after_commit
from firm.queue import schema


@bq.job()
def _demo(x: int) -> None:  # registered at import; enqueued by the tests
    pass


@bq.job()
def _demo2(x: int) -> None:
    pass


def _ready(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(schema.ready_executions)).scalar() or 0


def test_enqueues_on_commit(queue_db) -> None:
    with Session(queue_db.engine) as session:
        session.execute(select(1))  # the request does some DB work
        enqueue_after_commit(session, _demo, 1)
        assert _ready(queue_db.engine) == 0  # deferred — nothing enqueued yet
        session.commit()
    assert _ready(queue_db.engine) == 1


def test_discarded_on_rollback(queue_db) -> None:
    with Session(queue_db.engine) as session:
        session.execute(select(1))
        enqueue_after_commit(session, _demo, 1)
        session.rollback()
    assert _ready(queue_db.engine) == 0


def test_multiple_enqueues_and_session_reuse(queue_db) -> None:
    with Session(queue_db.engine) as session:
        session.execute(select(1))
        enqueue_after_commit(session, _demo, 1)
        enqueue_after_commit(session, _demo, 2)
        session.commit()
        assert _ready(queue_db.engine) == 2
        # reuse the same session for a second transaction
        session.execute(select(1))
        enqueue_after_commit(session, _demo, 3)
        session.commit()
    assert _ready(queue_db.engine) == 3


def test_failed_enqueue_attempts_the_rest_then_propagates(queue_db, monkeypatch) -> None:
    def _boom(*_args, **_kwargs) -> None:
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(_demo, "enqueue", _boom)  # first job's enqueue blows up
    with Session(queue_db.engine) as session:
        session.execute(select(1))
        enqueue_after_commit(session, _demo, 1)  # fails
        enqueue_after_commit(session, _demo2, 2)  # must still be attempted
        with pytest.raises(RuntimeError, match="enqueue failed"):
            session.commit()
    assert _ready(queue_db.engine) == 1  # _demo2 got enqueued despite _demo failing
