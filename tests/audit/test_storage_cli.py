"""Drive prepared S3 writes and archive export/restore through the real command parser."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from click.testing import CliRunner

from firm.audit.cli import main


def test_cli_s3_prepare_write_and_get(object_client, tmp_path, monkeypatch):
    boto3 = pytest.importorskip("boto3")
    object_client.close = lambda: None
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: object_client)
    runner = CliRunner()
    base = ["s3", "--bucket", "events", "--stream", str(UUID(int=1))]
    path = tmp_path / "prepared.json"
    result = runner.invoke(
        main, [*base, "prepare", "a", "--event-file", str(path), "--data", '{"x": 1}']
    )
    assert result.exit_code == 0, result.output
    assert runner.invoke(main, [*base, "prepare", "b", "--event-file", str(path)]).exit_code != 0
    result = runner.invoke(main, [*base, "write", str(path)])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert runner.invoke(main, [*base, "write", str(path)]).output == result.output  # idempotent
    for locator in (["1"], [receipt["key"], "--sha256", receipt["sha256"]]):
        result = runner.invoke(main, [*base, "get", *locator])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"] == {"x": 1}


def test_cli_s3_has_the_sql_commands(object_client, monkeypatch):
    boto3 = pytest.importorskip("boto3")
    object_client.close = lambda: None
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: object_client)
    monkeypatch.setenv("FIRM_AUDIT_KEY", "cli-s3-row-and-seal-secret-0123456789abc")
    from firm.audit import AuditLog, S3Storage

    with AuditLog(
        storage=S3Storage("events", stream=str(UUID(int=1)), client=object_client)
    ) as log:
        log.record("invoice.paid", subject=("Invoice", 42))
        log.record("user.login")
    runner = CliRunner()
    base = ["s3", "--bucket", "events", "--stream", str(UUID(int=1))]
    assert runner.invoke(main, [*base, "stats"]).output == "events: 2\n"
    result = runner.invoke(main, [*base, "history", "--subject-type", "Invoice"])
    assert result.exit_code == 0 and "invoice.paid" in result.output
    assert "user.login" not in result.output
    assert runner.invoke(main, [*base, "seal"]).output.startswith("sealed ")
    result = runner.invoke(main, [*base, "verify", "--full"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith(("OK:", "WARNING:"))
    assert runner.invoke(main, [*base, "prune"]).output == "pruned 0 events\n"


def test_cli_copy_export_read_restore(object_client, audit, tmp_path, monkeypatch):
    boto3 = pytest.importorskip("boto3")
    object_client.close = lambda: None
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: object_client)
    audit.record("source")
    runner = CliRunner()
    base = ["archive", "--bucket", "archives"]
    result = runner.invoke(
        main,
        [
            *base,
            "export-sql",
            "--database-url",
            audit.engine.url.render_as_string(hide_password=False),
            "--stream",
            str(UUID(int=1)),
            "--batch-id",
            str(uuid4()),
        ],
    )
    assert result.exit_code == 0, result.output
    key = json.loads(result.output)["key"]
    result = runner.invoke(main, [*base, "read", key])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["event"]["action"] == "source"
    target = f"sqlite:///{tmp_path / 'restored.db'}"
    result = runner.invoke(main, [*base, "restore-sql", key, "--target-url", target])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["restored"] == 1
