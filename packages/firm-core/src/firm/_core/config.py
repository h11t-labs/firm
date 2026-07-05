"""Engine + dialect runtime shared by the firm packages.

A :class:`Runtime` owns one database's engine and dialect. It is created either from a URL
(the engine is built lazily, so a forked child can :meth:`Runtime.reset` to drop the parent's
inherited SQLite handles before first use) or around a caller-provided engine (shared with the
host application; the caller keeps ownership).

There is deliberately no global state here: ``Cache``/``Channel``/``AuditLog`` each own a
runtime-equivalent per instance, and the queue's process-global ``configure()`` singleton
lives in :mod:`firm.queue.config`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from sqlalchemy import Engine

from .database import create_engine_for, dispose_engine
from .dialects import Dialect, get_dialect


@dataclass
class Settings:
    """Generic engine settings. Feature packages subclass this for their own knobs."""

    database_url: str | None = None
    busy_timeout_ms: int = 5000
    pool_size: int = 20
    max_overflow: int = 40


class Runtime:
    """Owns the engine + dialect for one database."""

    def __init__(self, settings: Settings | None = None, *, engine: Engine | None = None) -> None:
        if engine is None and (settings is None or settings.database_url is None):
            raise ValueError("Runtime needs either settings.database_url or an engine")
        self.settings = settings if settings is not None else Settings()
        self._provided_engine = engine
        self._engine: Engine | None = engine
        self._dialect: Dialect | None = None
        self._lock = threading.Lock()

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    assert self.settings.database_url is not None
                    self._engine = create_engine_for(
                        self.settings.database_url,
                        busy_timeout_ms=self.settings.busy_timeout_ms,
                        pool_size=self.settings.pool_size,
                        max_overflow=self.settings.max_overflow,
                    )
        return self._engine

    @property
    def dialect(self) -> Dialect:
        if self._dialect is None:
            self._dialect = get_dialect(self.engine)
        return self._dialect

    def reset(self, *, close: bool = True) -> None:
        """Drop pooled connections. Call ``reset(close=False)`` first thing in a forked child:
        the inherited connections are the parent's live sockets and must be dropped, not
        closed (closing them would kill server sessions the parent still uses).

        An owned engine is recreated lazily from the URL on next use; a caller-provided
        engine is kept — ``dispose()`` already gives it a fresh pool.
        """
        with self._lock:
            if self._engine is not None:
                dispose_engine(self._engine, close=close)
            if self._provided_engine is None:
                self._engine = None
            self._dialect = None
