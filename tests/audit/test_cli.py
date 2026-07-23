"""CLI specs for ``firm-audit``."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from click.testing import CliRunner
from sqlalchemy import update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, Ref, schema
from firm.audit.cli import main


def test_stats_reports_event_count(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    audit.record("a")
    audit.close()
    result = CliRunner().invoke(main, ["stats", "--database-url", db_url])
    assert result.exit_code == 0
    assert "events: 1" in result.output


def test_history_lists_recorded_events(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    audit.record("invoice.paid", subject=("Invoice", "1"))
    audit.close()
    result = CliRunner().invoke(main, ["history", "--database-url", db_url])
    assert result.exit_code == 0
    assert "invoice.paid" in result.output
    assert "Invoice:1" in result.output


def test_history_filters_by_action(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    audit.record("a")
    audit.record("b")
    audit.close()
    result = CliRunner().invoke(main, ["history", "--database-url", db_url, "--action", "a"])
    assert result.exit_code == 0
    assert "  a  subject=" in result.output
    assert "  b  subject=" not in result.output


def test_history_filters_by_subject_type_alone(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    audit.record("kept", subject=("Invoice", "1"))
    audit.record("dropped", subject=("Rule", "1"))
    audit.close()
    result = CliRunner().invoke(
        main, ["history", "--database-url", db_url, "--subject-type", "Invoice"]
    )
    assert result.exit_code == 0
    assert "kept" in result.output
    assert "dropped" not in result.output


def test_history_filters_by_actor_type_alone(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    audit.record("kept", actor=("Model", "9"))
    audit.record("dropped", actor=("User", "9"))
    audit.close()
    result = CliRunner().invoke(
        main, ["history", "--database-url", db_url, "--actor-type", "Model"]
    )
    assert result.exit_code == 0
    assert "kept" in result.output
    assert "dropped" not in result.output


def test_history_renders_label_actor_without_none(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    audit.record("sync.ran", actor="cron")
    audit.close()
    result = CliRunner().invoke(main, ["history", "--database-url", db_url])
    assert result.exit_code == 0
    assert "actor=cron" in result.output
    assert "cron:None" not in result.output


def test_history_renders_display_name(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    audit.record("invoice.paid", actor=Ref("User", 7, name="alice@example.com"))
    audit.close()
    result = CliRunner().invoke(main, ["history", "--database-url", db_url])
    assert result.exit_code == 0
    assert "User:7 (alice@example.com)" in result.output


def test_prune_reports_deleted_count(db_url: str) -> None:
    AuditLog(database_url=db_url).close()  # create the (empty) schema
    result = CliRunner().invoke(main, ["prune", "--database-url", db_url])
    assert result.exit_code == 0
    assert "pruned 0 events" in result.output


def test_prune_with_max_age_flag_deletes_old_rows(db_url: str) -> None:
    audit = AuditLog(database_url=db_url)
    audit.record("old")
    with transaction(audit.engine) as conn:
        conn.execute(
            update(schema.audit_events)
            .where(schema.audit_events.c.action == "old")
            .values(created_at=now_utc() - timedelta(hours=2))
        )
    audit.close()

    result = CliRunner().invoke(main, ["prune", "--database-url", db_url, "--max-age", "3600"])
    assert result.exit_code == 0
    assert "pruned 1 events" in result.output


def test_prune_prints_skipped_unsealed_count(db_url: str, monkeypatch) -> None:
    secret = "cli-prune-test-key-padding-0123456789"  # noqa: S105  (throwaway)
    audit = AuditLog(database_url=db_url, mac_key=secret, grace=0.0)
    audit.sealer.run_once()  # explicit activation on an empty log
    audit.record("sealed")
    audit.sealer.run_once()
    audit.record("old.a")
    audit.record("old.b")
    with transaction(audit.engine) as conn:
        conn.execute(
            update(schema.audit_events)
            .where(schema.audit_events.c.action.in_(["old.a", "old.b"]))
            .values(created_at=now_utc() - timedelta(hours=2))
        )
    audit.close()
    monkeypatch.setenv("FIRM_AUDIT_KEY", secret)

    result = CliRunner().invoke(main, ["prune", "--database-url", db_url, "--max-age", "3600"])
    assert result.exit_code == 0
    assert "skipped" in result.output
    assert "UNSEALED" in result.output


def test_prune_reports_refused_tampered_range(db_url: str, monkeypatch, at_time) -> None:
    secret = "cli-refuse-test-key-padding-0123456789"  # noqa: S105  (throwaway)
    audit = AuditLog(database_url=db_url, mac_key=secret, grace=0.0)
    audit.sealer.run_once()
    old = now_utc() - timedelta(hours=2)
    with at_time(old):
        audit.record("sealed")
    with patch("firm.audit.sealing.now_utc", lambda: old):
        audit.sealer.run_once()
    # Tamper the sealed row's content with a plain UPDATE (row_mac column left untouched).
    with transaction(audit.engine) as conn:
        conn.execute(
            update(schema.audit_events)
            .where(schema.audit_events.c.action == "sealed")
            .values(action="sealed.TAMPERED")
        )
    audit.close()
    monkeypatch.setenv("FIRM_AUDIT_KEY", secret)

    result = CliRunner().invoke(main, ["prune", "--database-url", db_url, "--max-age", "3600"])
    assert result.exit_code == 0
    assert "pruned 0 events" in result.output
    assert "REFUSED" in result.output


def test_env_var_supplies_url(db_url: str, monkeypatch) -> None:
    AuditLog(database_url=db_url).close()
    monkeypatch.setenv("FIRM_AUDIT_DATABASE_URL", db_url)
    result = CliRunner().invoke(main, ["stats"])
    assert result.exit_code == 0
    assert "events: 0" in result.output


def test_missing_url_is_a_usage_error(monkeypatch) -> None:
    monkeypatch.delenv("FIRM_AUDIT_DATABASE_URL", raising=False)
    result = CliRunner().invoke(main, ["stats"])
    assert result.exit_code != 0


def test_anchor_compact_writes_signed_checkpoint_and_preserves_verification(
    db_url: str, tmp_path, monkeypatch
) -> None:
    secret = "cli-anchor-compact-key-padding-0123456789"  # noqa: S105
    anchor = tmp_path / "anchor.log"
    audit = AuditLog(
        database_url=db_url,
        mac_key=secret,
        grace=0.0,
        anchor_path=str(anchor),
        anchor_max_age=3600.0,
    )
    try:
        audit.sealer.run_once()
        audit.record("covered")
        assert audit.sealer.run_once() == 1
        assert audit.verify(full=True).outcome == "ok"
    finally:
        audit.close()

    monkeypatch.setenv("FIRM_AUDIT_KEY", secret)
    result = CliRunner().invoke(
        main,
        [
            "anchor-compact",
            "--database-url",
            db_url,
            "--anchor",
            str(anchor),
        ],
    )
    assert result.exit_code == 0
    assert "coverage=1, floor=0" in result.output
    lines = anchor.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].split()[1] == "CHECKPOINT"

    verifier = AuditLog(
        database_url=db_url,
        create_schema=False,
        mac_key=secret,
        grace=0.0,
        anchor_path=str(anchor),
        anchor_max_age=3600.0,
    )
    try:
        assert verifier.verify(full=True).outcome == "ok"
    finally:
        verifier.close()
