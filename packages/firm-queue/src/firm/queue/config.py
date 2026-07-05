"""Process-global queue configuration.

Unlike ``Cache``/``Channel``/``AuditLog`` (one instance per use), the queue keeps a single
process-wide runtime: enqueuing job functions, workers, and the CLI all have to agree on one
database, and the ``@job`` registry is process-global anyway. This module owns that
singleton; the generic engine/dialect :class:`~firm._core.config.Runtime` lives in
``firm._core.config``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine

from .._core.config import Runtime, Settings


@dataclass
class QueueSettings(Settings):
    """Engine settings plus the queue's own knobs."""

    default_queue: str = "default"
    preserve_finished_jobs: bool = True


_runtime: Runtime | None = None


def configure(
    database_url: str | None = None,
    *,
    engine: Engine | None = None,
    busy_timeout_ms: int = 5000,
    pool_size: int = 20,
    max_overflow: int = 40,
    default_queue: str = "default",
    preserve_finished_jobs: bool = True,
) -> Runtime:
    """Set the process-global queue runtime; returns it for convenience.

    Pass either ``database_url`` (firm owns the engine) or ``engine`` to share your
    application's SQLAlchemy engine (you keep ownership; the engine-tuning settings are
    ignored in that case).
    """
    global _runtime
    settings = QueueSettings(
        database_url=database_url,
        busy_timeout_ms=busy_timeout_ms,
        pool_size=pool_size,
        max_overflow=max_overflow,
        default_queue=default_queue,
        preserve_finished_jobs=preserve_finished_jobs,
    )
    _runtime = Runtime(settings, engine=engine)
    return _runtime


def current_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("firm.queue is not configured; call firm.queue.configure(...) first.")
    return _runtime


def set_runtime(runtime: Runtime | None) -> None:
    """Replace the global runtime (used by tests)."""
    global _runtime
    _runtime = runtime
