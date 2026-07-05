"""CLI specs for ``firm-channel``."""

from __future__ import annotations

from click.testing import CliRunner

from firm.channel import Channel
from firm.channel.cli import main


def test_stats_reports_message_count(db_url: str) -> None:
    ps = Channel(database_url=db_url)
    ps.broadcast("c", b"x")
    ps.close()
    result = CliRunner().invoke(main, ["stats", "--database-url", db_url])
    assert result.exit_code == 0
    assert "messages: 1" in result.output


def test_trim_reports_count(db_url: str) -> None:
    Channel(database_url=db_url).close()  # create the (empty) schema
    result = CliRunner().invoke(main, ["trim", "--database-url", db_url])
    assert result.exit_code == 0
    assert "trimmed 0 messages" in result.output


def test_env_var_supplies_url(db_url: str, monkeypatch) -> None:
    Channel(database_url=db_url).close()
    monkeypatch.setenv("FIRM_CHANNEL_DATABASE_URL", db_url)
    result = CliRunner().invoke(main, ["stats"])
    assert result.exit_code == 0
    assert "messages: 0" in result.output


def test_missing_url_is_a_usage_error(monkeypatch) -> None:
    monkeypatch.delenv("FIRM_CHANNEL_DATABASE_URL", raising=False)
    result = CliRunner().invoke(main, ["stats"])
    assert result.exit_code != 0
