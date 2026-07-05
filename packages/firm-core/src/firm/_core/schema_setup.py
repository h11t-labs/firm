"""Reconciles the two schema-creation paths: ``create_all`` auto-create and Alembic.

Every firm package lets you create its tables directly (``schema.create_all`` — used by the
``create_schema=True`` constructors, quickstarts, and tests) *or* via its bundled Alembic
migrations. Without coordination those paths conflict: an auto-created database carries no
revision stamp, so a later ``alembic upgrade`` would try to re-run the baseline against
existing tables. :func:`create_all_and_stamp` closes that gap — after creating the tables it
stamps the package's own version table (``firm_<module>_alembic_version``) at the migration
head, exactly as ``alembic stamp head`` would, so either path can be followed by the other.
"""

from __future__ import annotations

from functools import cache
from importlib import import_module

from sqlalchemy import Column, Connection, Engine, MetaData, String, Table, select

from .dialects import get_dialect


@cache
def _head_revision(migrations_package: str) -> str | None:
    from alembic.script import ScriptDirectory

    # __path__ (not importlib.resources.files) because the migrations dirs are namespace
    # packages, whose Traversable doesn't stringify to a filesystem path. Importing the
    # package runs no code — env.py is only executed by Alembic itself.
    directory = import_module(migrations_package).__path__[0]
    return ScriptDirectory(directory).get_current_head()


def _version_table(name: str) -> Table:
    # Mirrors Alembic's own version table shape (version_num VARCHAR(32) primary key).
    return Table(name, MetaData(), Column("version_num", String(32), primary_key=True))


def _stamp(conn: Connection, table: Table, head: str) -> None:
    if conn.execute(select(table.c.version_num)).first() is None:
        # insert_ignore: two processes auto-creating the same schema at once must not race.
        stmt = get_dialect(conn.engine).insert_ignore(
            table, {"version_num": head}, index_elements=("version_num",)
        )
        conn.execute(stmt)


def create_all_and_stamp(
    bind: Engine | Connection,
    metadata: MetaData,
    *,
    migrations_package: str,
    version_table: str,
) -> None:
    """Create every table in ``metadata``, then stamp ``version_table`` at the head revision
    of ``migrations_package`` — only when the database carries no stamp yet (an existing
    Alembic-managed stamp is left untouched)."""
    metadata.create_all(bind)
    head = _head_revision(migrations_package)
    if head is None:
        return
    table = _version_table(version_table)
    if isinstance(bind, Engine):
        table.create(bind, checkfirst=True)
        with bind.begin() as conn:
            _stamp(conn, table, head)
    else:
        table.create(bind, checkfirst=True)
        _stamp(bind, table, head)


def drop_all_and_unstamp(
    bind: Engine | Connection, metadata: MetaData, *, version_table: str
) -> None:
    """Drop every table in ``metadata`` and the version stamp with it, keeping the two in
    sync: a re-created schema re-stamps at whatever head is current then."""
    metadata.drop_all(bind)
    _version_table(version_table).drop(bind, checkfirst=True)
