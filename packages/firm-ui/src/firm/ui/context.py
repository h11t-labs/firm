"""Resolves which parts (queue / cache / channel / audit) the dashboard can show, and holds their
engines.

A part is enabled only if its primary table exists in the database configured for it, so the same
``firm-ui`` works against one shared database or several separate ones — pass `--database-url`
for the common case, or per-part `--queue-url` / `--cache-url` / `--channel-url` / `--audit-url`
to split them.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from sqlalchemy import Engine, inspect

from firm._core.config import Runtime, Settings
from firm._core.database import create_engine_for

_QUEUE_TABLE = "firm_jobs"
_CACHE_TABLE = "firm_entries"
_CHANNEL_TABLE = "firm_messages"
_AUDIT_TABLE = "firm_audits"


def _has_table(engine: Engine, table: str) -> bool:
    try:
        return table in inspect(engine).get_table_names()
    except Exception:
        return False


@dataclass
class Dashboard:
    queue: Runtime | None = None
    cache: Engine | None = None
    channel: Engine | None = None
    audit: Engine | None = None

    @property
    def parts(self) -> list[str]:
        names = []
        if self.queue is not None:
            names.append("queue")
        if self.cache is not None:
            names.append("cache")
        if self.channel is not None:
            names.append("channel")
        if self.audit is not None:
            names.append("audit")
        return names

    def close(self) -> None:
        if self.queue is not None:
            self.queue.reset()
        for engine in (self.cache, self.channel, self.audit):
            if engine is not None:
                with contextlib.suppress(Exception):
                    engine.dispose()


def _engine_if_table(url: str | None, table: str) -> Engine | None:
    if not url:
        return None
    engine = create_engine_for(url)
    if _has_table(engine, table):
        return engine
    engine.dispose()
    return None


def build_dashboard(
    *,
    database_url: str | None = None,
    queue_url: str | None = None,
    cache_url: str | None = None,
    channel_url: str | None = None,
    audit_url: str | None = None,
) -> Dashboard:
    dash = Dashboard()

    queue_resolved = queue_url or database_url
    if queue_resolved:
        runtime = Runtime(Settings(database_url=queue_resolved))
        if _has_table(runtime.engine, _QUEUE_TABLE):
            dash.queue = runtime
        else:
            runtime.reset()

    dash.cache = _engine_if_table(cache_url or database_url, _CACHE_TABLE)
    dash.channel = _engine_if_table(channel_url or database_url, _CHANNEL_TABLE)
    dash.audit = _engine_if_table(audit_url or database_url, _AUDIT_TABLE)
    return dash
