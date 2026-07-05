"""Standalone CLI commands must never claim with a NULL ``process_id``.

``recover_orphaned_claims`` only rescues claims whose ``process_id`` points at a process row
that is gone — a claim written with ``process_id NULL`` is invisible to recovery forever. So
``work`` and ``drain`` register a (heartbeated, for ``work``) process row and claim under it.
"""

from __future__ import annotations

from click.testing import CliRunner
from sqlalchemy import select

import firm.queue as bq
from firm.queue import cli, schema

_DRAIN_SEEN: dict[str, object] = {}


@bq.job()
def drain_probe_job() -> None:
    # Runs inside `firm-queue drain`: record the claim row + live process rows as the
    # executing worker sees them, so the test can assert the claim carried a process_id.
    rt = bq.current_runtime()
    with rt.engine.connect() as conn:
        _DRAIN_SEEN["claim_process_id"] = conn.execute(
            select(schema.claimed_executions.c.process_id)
        ).scalar_one()
        _DRAIN_SEEN["live_process_ids"] = set(conn.execute(select(schema.processes.c.id)).scalars())


def test_drain_claims_under_a_registered_process(runtime, engine, db_url) -> None:
    _DRAIN_SEEN.clear()
    drain_probe_job.enqueue()

    result = CliRunner().invoke(cli.main, ["drain", "--database-url", db_url])

    assert result.exit_code == 0, result.output
    assert "processed 1 job(s)" in result.output
    assert _DRAIN_SEEN["claim_process_id"] is not None
    # ...and it pointed at a registered process row, so recovery semantics hold.
    assert _DRAIN_SEEN["claim_process_id"] in _DRAIN_SEEN["live_process_ids"]
    # The one-shot deregisters its row on the way out.
    with engine.connect() as conn:
        assert conn.execute(select(schema.processes.c.id)).first() is None


def test_work_registers_a_heartbeated_process(db_url, engine, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeWorker:
        def __init__(self, runtime, queues=("*",), threads=3, process_id=None):
            captured["process_id"] = process_id

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    def _interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "Worker", _FakeWorker)
    monkeypatch.setattr(cli.time, "sleep", _interrupt)

    result = CliRunner().invoke(cli.main, ["work", "--database-url", db_url])

    assert result.exit_code == 0, result.output
    assert isinstance(captured["process_id"], int)
    # The process row is deregistered on shutdown.
    with engine.connect() as conn:
        assert conn.execute(select(schema.processes.c.id)).first() is None
