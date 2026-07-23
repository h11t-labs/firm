"""CLI specs for ``firm-cache``."""

from __future__ import annotations

from click.testing import CliRunner

from firm.cache import Cache
from firm.cache.cli import main


def test_stats_reports_count_and_size(db_url: str) -> None:
    cache = Cache(database_url=db_url, auto_expire=False)
    cache.set("a", "x")
    cache.close()
    result = CliRunner().invoke(main, ["stats", "--database-url", db_url])
    assert result.exit_code == 0
    assert "entries: 1" in result.output
    assert "estimated_size:" in result.output


def test_clear_reports_count(db_url: str) -> None:
    cache = Cache(database_url=db_url, auto_expire=False)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.close()
    result = CliRunner().invoke(main, ["clear", "--database-url", db_url])
    assert result.exit_code == 0
    assert "cleared 2 entries" in result.output


def test_trim_reports_count(db_url: str) -> None:
    Cache(database_url=db_url, auto_expire=False).close()  # create the (empty) schema
    result = CliRunner().invoke(main, ["trim", "--database-url", db_url])
    assert result.exit_code == 0
    assert "evicted 0 entries" in result.output


def test_trim_max_entries_targets_eviction(db_url: str) -> None:
    # A bare Cache keeps everything (generous defaults); passing --max-entries lets the one-shot
    # command evict down to a target the process default would never reach.
    cache = Cache(database_url=db_url, max_size=None, max_age=None, auto_expire=False)
    for i in range(10):
        cache.set(f"k{i}", i)
    cache.close()
    result = CliRunner().invoke(
        main,
        ["trim", "--database-url", db_url, "--max-entries", "5", "--batch-size", "2"],
    )
    assert result.exit_code == 0
    assert "evicted 2 entries" in result.output  # one batch of the over-limit rows


def test_trim_accepts_all_options(db_url: str) -> None:
    Cache(database_url=db_url, auto_expire=False).close()
    result = CliRunner().invoke(
        main,
        [
            "trim",
            "--database-url",
            db_url,
            "--max-age",
            "3600",
            "--max-size",
            "1000000",
            "--max-entries",
            "100",
            "--batch-size",
            "50",
        ],
    )
    assert result.exit_code == 0
    assert "evicted 0 entries" in result.output


def test_env_var_supplies_url(db_url: str, monkeypatch) -> None:
    Cache(database_url=db_url, auto_expire=False).close()
    monkeypatch.setenv("FIRM_CACHE_DATABASE_URL", db_url)
    result = CliRunner().invoke(main, ["stats"])
    assert result.exit_code == 0
    assert "entries: 0" in result.output


def test_missing_url_is_a_usage_error(monkeypatch) -> None:
    monkeypatch.delenv("FIRM_CACHE_DATABASE_URL", raising=False)
    result = CliRunner().invoke(main, ["stats"])
    assert result.exit_code != 0
