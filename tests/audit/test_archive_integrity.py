"""Archive integration preserves signed rows and every seal-aligned pruning gate."""

from datetime import timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import inspect, select, update

from firm._core.clock import now_utc
from firm._core.database import transaction
from firm.audit import AuditLog, events, schema
from firm.audit.integrity import row_mac
from firm.audit.sql_archive import SQLArchiver, restore_sql
from tests.audit.test_migration import _config

STREAM = str(UUID(int=1))
SECRET = "archive-integration-row-secret-0123456789"  # noqa: S105


def test_signed_range_waits_for_every_archive_member(db_url, archive, at_time):
    old = now_utc() - timedelta(hours=2)
    with AuditLog(db_url, mac_key=SECRET, max_age=3600, grace=0) as protected:
        # Activation's legacy prefix is deleted with the range, so it needs archival too.
        with at_time(old), transaction(protected.engine) as conn:
            events.append(conn, action="legacy")
        assert protected.sealer.run_once() == 0
        with at_time(old):
            protected.record("signed")
        with patch("firm.audit.sealing.now_utc", lambda: old):
            assert protected.sealer.run_once() == 1
        worker = SQLArchiver(protected, archive, stream=STREAM)
        worker.enable()
        worker.run_once(batch_size=1)
        assert protected.retention.run_once() == 0
        assert len(protected.history()) == 2
        with transaction(protected.engine) as conn:
            assert not conn.execute(
                select(schema.seals).where(schema.seals.c.kind == "floor")
            ).all()
        worker.run_once(batch_size=1)
        assert protected.retention.run_once() == 2
        assert worker.status()["unarchived"] == 0
        with transaction(protected.engine) as conn:
            assert not conn.execute(select(schema.archive_members)).all()
        assert protected.verify(full=True).outcome == "ok"


def test_signed_archive_restores_stored_bytes_and_row_mac(db_url, archive, at_time, tmp_path):
    with AuditLog(db_url, mac_key=SECRET) as source:
        source.record("signed", data={"z": 1, "a": 2})
        # A valid signature may cover nonstandard JSON whitespace from a prior writer.
        with transaction(source.engine) as conn:
            row = dict(conn.execute(select(schema.audit_events)).one()._mapping)
            row["data"] = '{ "z": 1, "a": 2 }'
            fields = {
                key: value for key, value in row.items() if key not in {"id", "row_mac", "key_id"}
            }
            signature = row_mac(source._key, **fields)
            conn.execute(update(schema.audit_events).values(data=row["data"], row_mac=signature))
        receipt = archive.export_sql(source, stream=STREAM, batch_id=str(uuid4()))
        assert archive.read(receipt)[0]["integrity"]["row_mac"] == signature
        with AuditLog(f"sqlite:///{tmp_path / 'restored-signed.db'}", mac_key=SECRET) as target:
            assert restore_sql(archive, [receipt], target) == 1
            with transaction(target.engine) as conn:
                restored = dict(conn.execute(select(schema.audit_events)).one()._mapping)
            assert restored["data"] == row["data"]
            assert restored["row_mac"] == signature
            fields = {
                key: value
                for key, value in restored.items()
                if key not in {"id", "row_mac", "key_id"}
            }
            assert row_mac(target._key, **fields) == signature


def test_copy_export_and_migration_from_released_schema(db_url, archive):
    url = db_url
    from firm._core.database import create_engine_for

    engine = create_engine_for(url)
    try:
        # Materialize the released schema, not current metadata through the old baseline.
        schema.drop_all(engine)
        for table in (schema.audit_events, schema.seals, schema.verify_status):
            table.create(engine)
        command.stamp(_config(url), "0002")
        assert not inspect(engine).has_table(schema.archive_imports.name)
        with AuditLog(engine=engine, create_schema=False, mac_key=SECRET) as source:
            source.record("released")
            with engine.connect() as conn:
                original = dict(conn.execute(select(schema.audit_events)).one()._mapping)
            receipt = archive.export_sql(source, stream=STREAM, batch_id=str(uuid4()))
            assert archive.read(receipt)[0]["event"]["action"] == "released"
        command.upgrade(_config(url), "head")
        assert inspect(engine).has_table(schema.archive_imports.name)
        with transaction(engine) as conn:
            assert conn.execute(select(schema.archive_policy.c.stream)).scalar_one() is None
            assert dict(conn.execute(select(schema.audit_events)).one()._mapping) == original
            assert (
                conn.exec_driver_sql(
                    "SELECT version_num FROM firm_audit_alembic_version"
                ).scalar_one()
                == "0003"
            )
    finally:
        engine.dispose()


def test_restore_refuses_existing_integrity_state(audit, archive, tmp_path):
    audit.record("plain")
    receipt = archive.export_sql(audit, stream=STREAM, batch_id=str(uuid4()))
    with AuditLog(f"sqlite:///{tmp_path / 'activated.db'}", mac_key=SECRET) as target:
        assert target.sealer.run_once() == 0
        with pytest.raises(ValueError, match="integrity"):
            restore_sql(archive, [receipt], target)
