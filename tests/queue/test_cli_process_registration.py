"""Standalone CLI commands must never claim with a NULL ``process_id``.

``recover_orphaned_claims`` only rescues claims whose ``process_id`` points at a process row
that is gone — a claim written with ``process_id NULL`` is invisible to recovery forever. So
``work`` and ``drain`` register a (heartbeated, for ``work``) process row and claim under it.
"""

from __future__ import annotations

import pytest
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


def test_drain_recovers_stale_predecessor_claims(runtime, engine, db_url) -> None:
    """Q-R1: with no supervisor around, nothing reaped stale-heartbeat processes, so a
    hard-killed predecessor's claim was stranded forever. Standalone commands now prune and
    recover at startup, and drain then processes the re-readied job like any other."""
    from datetime import timedelta

    from sqlalchemy import update

    from firm._core import process as pr
    from firm._core.clock import now_utc
    from firm.queue.claim import claim_ready

    _DRAIN_SEEN.clear()
    drain_probe_job.enqueue()
    dead_pid = pr.register(engine, pr.ProcessInfo(kind="Worker", name="crashed", pid=1))
    assert len(claim_ready(engine, runtime.dialect, ["*"], 5, dead_pid)) == 1
    with engine.begin() as conn:
        conn.execute(
            update(schema.processes)
            .where(schema.processes.c.id == dead_pid)
            .values(last_heartbeat_at=now_utc() - timedelta(seconds=600))
        )

    result = CliRunner().invoke(cli.main, ["drain", "--database-url", db_url])

    assert result.exit_code == 0, result.output
    assert "processed 1 job(s)" in result.output
    with engine.connect() as conn:
        assert conn.execute(select(schema.claimed_executions.c.id)).first() is None
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


# --- supervisor mode selection (upstream: cli_test.rb) ----------------------------------------


class _FakeSupervisor:
    """Stand-in so ``start`` records the chosen mode without forking or blocking on its run loop."""

    selected: str | None = None

    def __init__(self, runtime: object, config: object) -> None:
        self.runtime = runtime
        self.config = config

    def start(self) -> None:
        type(self)._record()

    def stop(self) -> None:  # used only by the thread branch
        pass

    @classmethod
    def _record(cls) -> None:  # pragma: no cover - overridden per subclass
        raise NotImplementedError


class _FakeForkSupervisor(_FakeSupervisor):
    @classmethod
    def _record(cls) -> None:
        _FakeSupervisor.selected = "fork"


class _FakeThreadSupervisor(_FakeSupervisor):
    @classmethod
    def _record(cls) -> None:
        _FakeSupervisor.selected = "thread"


@pytest.fixture
def patched_supervisors(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSupervisor]:
    """Replace the supervisor classes ``start`` instantiates, and stub the DB ``configure`` so no
    engine is created. The thread branch's ``while True: time.sleep(1)`` is interrupted at once."""
    _FakeSupervisor.selected = None
    monkeypatch.setattr(cli, "ForkSupervisor", _FakeForkSupervisor)
    monkeypatch.setattr(cli, "ThreadSupervisor", _FakeThreadSupervisor)
    monkeypatch.setattr(cli, "configure", lambda database_url: object())

    def _interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", _interrupt)
    return _FakeSupervisor


def test_mode_defaults_to_fork(patched_supervisors: type[_FakeSupervisor]) -> None:
    # upstream: cli_test.rb "mode defaults to fork when there is no env var or option".
    result = CliRunner().invoke(cli.main, ["start", "--database-url", "sqlite://"])
    assert result.exit_code == 0, result.output
    assert patched_supervisors.selected == "fork"


def test_mode_option_selects_thread(patched_supervisors: type[_FakeSupervisor]) -> None:
    # upstream: cli_test.rb mode-override variant: --mode thread selects the thread supervisor.
    result = CliRunner().invoke(
        cli.main, ["start", "--database-url", "sqlite://", "--mode", "thread"]
    )
    assert result.exit_code == 0, result.output
    assert patched_supervisors.selected == "thread"


def test_mode_option_selects_fork_explicitly(patched_supervisors: type[_FakeSupervisor]) -> None:
    # The explicit --mode fork form also selects the fork supervisor.
    result = CliRunner().invoke(
        cli.main, ["start", "--database-url", "sqlite://", "--mode", "fork"]
    )
    assert result.exit_code == 0, result.output
    assert patched_supervisors.selected == "fork"


def test_mode_env_var_override(
    patched_supervisors: type[_FakeSupervisor], monkeypatch: pytest.MonkeyPatch
) -> None:
    # upstream: cli_test.rb env-var variant: FIRM_QUEUE_MODE picks the mode without --mode.
    monkeypatch.setenv("FIRM_QUEUE_MODE", "thread")
    result = CliRunner().invoke(cli.main, ["start", "--database-url", "sqlite://"])
    assert result.exit_code == 0, result.output
    assert patched_supervisors.selected == "thread"
