"""Database schema — firm-queue's 11 tables (SQLAlchemy Core).

All tables and indexes are namespaced ``firm_*``. Primary keys use ``BigInteger``
everywhere except on SQLite, where the ``Integer`` variant maps to ``INTEGER PRIMARY KEY``
(rowid) and gets proper autoincrement.

The job lifecycle moves a row's *execution* between tables:

    jobs (source of truth)
      └─ scheduled_executions   (future) ──dispatcher──▶ ready/blocked
      └─ ready_executions       (claimable now) ──worker──▶ claimed_executions
      └─ blocked_executions     (waiting on a concurrency semaphore)
      └─ claimed_executions     (running) ──▶ finished (jobs.finished_at) | failed_executions

These Table objects are a supported *read* surface (the dashboard's queries build on them);
renaming a table or column is a breaking change. Mutations must go through this package's
functions — never write to these tables directly.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import Connection, Engine

from .._core import schema as _core_schema
from .._core.clock import now_utc
from .._core.schema_setup import create_all_and_stamp, drop_all_and_unstamp

metadata = MetaData()

VERSION_TABLE = "firm_queue_alembic_version"


def _dt() -> DateTime:
    """A timestamp column type.

    MySQL's plain ``DATETIME`` truncates to whole seconds, which would corrupt sub-second
    ordering of ``scheduled_at``/``expires_at`` and the ``(task_key, run_at)`` dedupe; we use
    ``DATETIME(6)`` there. Postgres/SQLite already keep fractional seconds.
    """
    return DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")


def _long_text() -> Text:
    """Text that maps to ``LONGTEXT`` on MySQL (plain ``TEXT`` caps at 64 KiB)."""
    return Text().with_variant(LONGTEXT, "mysql")


def _pk() -> Column:
    """A bigint primary key that becomes a SQLite rowid (autoincrement) under the hood."""
    return Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True)


def _job_fk() -> Column:
    return Column(
        "job_id",
        BigInteger,
        ForeignKey("firm_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )


def _created_at() -> Column:
    return Column("created_at", _dt(), nullable=False, default=now_utc)


jobs = Table(
    "firm_jobs",
    metadata,
    _pk(),
    Column("queue_name", String(255), nullable=False),
    Column("class_name", String(255), nullable=False),
    Column("arguments", _long_text()),
    Column("priority", Integer, nullable=False, server_default="0", default=0),
    Column("active_job_id", String(255)),
    Column("scheduled_at", _dt()),
    Column("finished_at", _dt()),
    Column("concurrency_key", String(255)),
    # firm owns retry counting (rather than delegating it), tracked in this column.
    Column("attempts", Integer, nullable=False, server_default="0", default=0),
    Column("created_at", _dt(), nullable=False, default=now_utc),
    Column("updated_at", _dt(), nullable=False, default=now_utc, onupdate=now_utc),
    Index("index_firm_jobs_on_active_job_id", "active_job_id"),
    Index("index_firm_jobs_on_class_name", "class_name"),
    Index("index_firm_jobs_on_finished_at", "finished_at"),
    Index("index_firm_jobs_for_filtering", "queue_name", "finished_at"),
    Index("index_firm_jobs_for_alerting", "scheduled_at", "finished_at"),
)

ready_executions = Table(
    "firm_ready_executions",
    metadata,
    _pk(),
    _job_fk(),
    Column("queue_name", String(255), nullable=False),
    Column("priority", Integer, nullable=False, server_default="0", default=0),
    _created_at(),
    Index("index_firm_ready_executions_on_job_id", "job_id", unique=True),
    Index("index_firm_poll_all", "priority", "job_id"),
    Index("index_firm_poll_by_queue", "queue_name", "priority", "job_id"),
)

claimed_executions = Table(
    "firm_claimed_executions",
    metadata,
    _pk(),
    _job_fk(),
    Column("process_id", BigInteger),
    _created_at(),
    Index("index_firm_claimed_executions_on_job_id", "job_id", unique=True),
    Index(
        "index_firm_claimed_executions_on_process_id_and_job_id",
        "process_id",
        "job_id",
    ),
)

scheduled_executions = Table(
    "firm_scheduled_executions",
    metadata,
    _pk(),
    _job_fk(),
    Column("queue_name", String(255), nullable=False),
    Column("priority", Integer, nullable=False, server_default="0", default=0),
    Column("scheduled_at", _dt(), nullable=False),
    _created_at(),
    Index("index_firm_scheduled_executions_on_job_id", "job_id", unique=True),
    Index("index_firm_dispatch_all", "scheduled_at", "priority", "job_id"),
)

blocked_executions = Table(
    "firm_blocked_executions",
    metadata,
    _pk(),
    _job_fk(),
    Column("queue_name", String(255), nullable=False),
    Column("priority", Integer, nullable=False, server_default="0", default=0),
    Column("concurrency_key", String(255), nullable=False),
    Column("expires_at", _dt(), nullable=False),
    _created_at(),
    Index(
        "index_firm_blocked_executions_for_release",
        "concurrency_key",
        "priority",
        "job_id",
    ),
    Index(
        "index_firm_blocked_executions_for_maintenance",
        "expires_at",
        "concurrency_key",
    ),
    Index("index_firm_blocked_executions_on_job_id", "job_id", unique=True),
)

failed_executions = Table(
    "firm_failed_executions",
    metadata,
    _pk(),
    _job_fk(),
    Column("error", _long_text()),
    _created_at(),
    Index("index_firm_failed_executions_on_job_id", "job_id", unique=True),
)

semaphores = Table(
    "firm_semaphores",
    metadata,
    _pk(),
    Column("key", String(255), nullable=False),
    Column("value", Integer, nullable=False, server_default="1", default=1),
    Column("expires_at", _dt(), nullable=False),
    Column("created_at", _dt(), nullable=False, default=now_utc),
    Column("updated_at", _dt(), nullable=False, default=now_utc, onupdate=now_utc),
    Index("index_firm_semaphores_on_key", "key", unique=True),
    Index("index_firm_semaphores_on_key_and_value", "key", "value"),
    Index("index_firm_semaphores_on_expires_at", "expires_at"),
)

pauses = Table(
    "firm_pauses",
    metadata,
    _pk(),
    Column("queue_name", String(255), nullable=False),
    _created_at(),
    Index("index_firm_pauses_on_queue_name", "queue_name", unique=True),
)

# firm_processes is core infrastructure (process registration lives in firm._core.process);
# its definition is owned by firm._core.schema and copied here so the queue's create_all and
# Alembic baseline manage it alongside the queue tables.
processes = _core_schema.processes.to_metadata(metadata)

recurring_tasks = Table(
    "firm_recurring_tasks",
    metadata,
    _pk(),
    Column("key", String(255), nullable=False),
    Column("schedule", String(255), nullable=False),
    Column("command", String(2048)),
    Column("class_name", String(255)),
    Column("arguments", Text),
    Column("queue_name", String(255)),
    Column("priority", Integer, server_default="0", default=0),
    Column("static", Boolean, nullable=False, server_default="1", default=True),
    Column("description", Text),
    Column("created_at", _dt(), nullable=False, default=now_utc),
    Column("updated_at", _dt(), nullable=False, default=now_utc, onupdate=now_utc),
    Index("index_firm_recurring_tasks_on_key", "key", unique=True),
    Index("index_firm_recurring_tasks_on_static", "static"),
)

recurring_executions = Table(
    "firm_recurring_executions",
    metadata,
    _pk(),
    _job_fk(),
    Column("task_key", String(255), nullable=False),
    Column("run_at", _dt(), nullable=False),
    _created_at(),
    Index("index_firm_recurring_executions_on_job_id", "job_id", unique=True),
    Index(
        "index_firm_recurring_executions_on_task_key_and_run_at",
        "task_key",
        "run_at",
        unique=True,
    ),
)


def create_all(bind: Engine | Connection) -> None:
    """Create every firm-queue table and stamp the Alembic baseline, so an auto-created schema
    stays ``alembic upgrade``-able later."""
    create_all_and_stamp(
        bind, metadata, migrations_package="firm.queue.migrations", version_table=VERSION_TABLE
    )


def drop_all(bind: Engine | Connection) -> None:
    drop_all_and_unstamp(bind, metadata, version_table=VERSION_TABLE)
