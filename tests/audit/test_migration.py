"""The Alembic migrations: 0001 baseline creates the audit table; 0002 renames it to
``firm_audit_events`` and adds tamper-evidence."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    Column,
    Index,
    MetaData,
    String,
    Table,
    Text,
    inspect,
    select,
)

import firm.audit
from firm._core.clock import now_utc
from firm._core.database import create_engine_for
from firm._core.schema import dt_type, pk_bigint
from firm.audit import schema

MIGRATIONS_DIR = Path(firm.audit.__file__).parent / "migrations"


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_creates_audit_events_table(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_config(url), "head")
    engine = create_engine_for(url)
    try:
        names = inspect(engine).get_table_names()
        assert "firm_audit_events" in names
        assert "firm_audits" not in names
    finally:
        engine.dispose()


def test_upgrade_creates_reference_label_columns(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_config(url), "head")
    engine = create_engine_for(url)
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("firm_audit_events")}
        assert {"subject_label", "actor_label"} <= cols
    finally:
        engine.dispose()


def test_upgrade_head_creates_tamper_evidence_schema(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    command.upgrade(_config(url), "head")
    engine = create_engine_for(url)
    try:
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("firm_audit_events")}
        assert {"entry_id", "row_mac", "key_id"} <= cols
        assert {"firm_audit_seals", "firm_audit_verify_status"} <= set(insp.get_table_names())
        idx = {i["name"]: i for i in insp.get_indexes("firm_audit_events")}
        assert idx["index_firm_audit_events_on_entry_id"]["unique"]
        seal_cols = {c["name"] for c in insp.get_columns("firm_audit_seals")}
        assert {"seq", "prev_mac", "gap_ranges"}.isdisjoint(seal_cols)
        seal_idx = {i["name"]: i for i in insp.get_indexes("firm_audit_seals")}
        assert seal_idx["index_firm_audit_seals_on_from_id"]["unique"]
        status_cols = {c["name"] for c in insp.get_columns("firm_audit_verify_status")}
        assert {"cycle_position", "cycle_length"}.isdisjoint(status_cols)
    finally:
        engine.dispose()


def _pre_0002_audits() -> Table:
    """The ``firm_audits`` table exactly as the 0.1.0 baseline shipped it — under its old name,
    with the old-style ``index_firm_audits_on_*`` secondary indexes, and before the tamper-
    evidence columns existed. Used to stand up a legacy database at revision 0001 so the 0002
    upgrade has a real table to *rename* and real columns to *add* (on a freshly built chain
    0001's ``metadata.create_all`` would already create everything under the new name, hiding the
    incremental step)."""
    return Table(
        "firm_audits",
        MetaData(),
        Column("id", pk_bigint(), primary_key=True),
        Column("action", String(255), nullable=False),
        Column("subject_type", String(255)),
        Column("subject_id", String(255)),
        Column("subject_label", String(255)),
        Column("actor_type", String(255)),
        Column("actor_id", String(255)),
        Column("actor_label", String(255)),
        Column("correlation_id", String(255)),
        Column("data", Text),
        Column("changes", Text),
        Column("context", Text),
        Column("created_at", dt_type(), nullable=False),
        Index("index_firm_audits_on_subject", "subject_type", "subject_id"),
        Index("index_firm_audits_on_actor", "actor_type", "actor_id"),
        Index("index_firm_audits_on_action", "action"),
        Index("index_firm_audits_on_correlation_id", "correlation_id"),
        Index("index_firm_audits_on_created_at", "created_at"),
    )


def test_upgrade_0001_to_0002_on_populated_db(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'legacy.db'}"

    # Stand up a legacy database: the pre-0002 ``firm_audits`` table, populated, stamped at 0001.
    engine = create_engine_for(url)
    legacy = _pre_0002_audits()
    legacy.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            legacy.insert(),
            [
                {"action": "legacy.one", "created_at": now_utc()},
                {"action": "legacy.two", "created_at": now_utc()},
            ],
        )
    engine.dispose()
    command.stamp(_config(url), "0001")

    # The upgrade under test: 0002 renames the table to ``firm_audit_events`` (rows intact),
    # renames its secondary indexes, and adds the columns/tables/index.
    command.upgrade(_config(url), "head")

    engine = create_engine_for(url)
    try:
        insp = inspect(engine)
        names = set(insp.get_table_names())
        assert "firm_audit_events" in names
        assert "firm_audits" not in names
        cols = {c["name"] for c in insp.get_columns("firm_audit_events")}
        assert {"entry_id", "row_mac", "key_id"} <= cols
        assert {"firm_audit_seals", "firm_audit_verify_status"} <= names

        index_names = {i["name"] for i in insp.get_indexes("firm_audit_events")}
        # Secondary indexes were renamed to the new convention; none of the old names survive.
        assert {
            "index_firm_audit_events_on_subject",
            "index_firm_audit_events_on_actor",
            "index_firm_audit_events_on_action",
            "index_firm_audit_events_on_correlation_id",
            "index_firm_audit_events_on_created_at",
        } <= index_names
        assert not any(name.startswith("index_firm_audits_on_") for name in index_names)
        assert any(
            i["name"] == "index_firm_audit_events_on_entry_id" and i["unique"]
            for i in insp.get_indexes("firm_audit_events")
        )

        # The pre-existing rows survive the rename and read as unprotected (NULL new columns).
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    schema.audit_events.c.action,
                    schema.audit_events.c.entry_id,
                    schema.audit_events.c.row_mac,
                    schema.audit_events.c.key_id,
                ).order_by(schema.audit_events.c.id)
            ).all()
        assert [r.action for r in rows] == ["legacy.one", "legacy.two"]
        assert all(r.entry_id is None and r.row_mac is None and r.key_id is None for r in rows)

        # The rebuilt SQLite table uses AUTOINCREMENT, so ids never regress after retention empties
        # it. This is the schema-level half of decision 10; retention has the behavioral test.
        with engine.connect() as conn:
            sql = conn.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='firm_audit_events'"
            ).scalar_one()
        assert "AUTOINCREMENT" in sql.upper()
    finally:
        engine.dispose()
