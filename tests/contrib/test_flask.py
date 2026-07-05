"""Specs for the Flask extension integration."""

from __future__ import annotations

import pytest

pytest.importorskip("flask")

from flask import Flask
from sqlalchemy import func, select

import firm.queue as bq
from firm.contrib.flask import Firm
from firm.queue import schema
from firm.queue.config import current_runtime, set_runtime


@bq.job()
def _gjob(x: int) -> None:
    pass


def test_extension_configures_and_enqueues(queue_url) -> None:
    app = Flask(__name__)
    app.config["FIRM_DATABASE_URL"] = queue_url
    Firm(app)

    @app.post("/go")
    def go() -> tuple[str, int]:
        _gjob.enqueue(1)
        return "", 202

    try:
        assert app.test_client().post("/go").status_code == 202
        with current_runtime().engine.connect() as conn:
            count = conn.execute(select(func.count()).select_from(schema.ready_executions)).scalar()
        assert count == 1
    finally:
        set_runtime(None)


def test_cli_command_registered(queue_url) -> None:
    app = Flask(__name__)
    app.config["FIRM_DATABASE_URL"] = queue_url
    Firm(app)
    try:
        assert "firm" in app.cli.commands  # `flask firm worker`
    finally:
        set_runtime(None)


def test_missing_url_raises(tmp_path) -> None:
    app = Flask(__name__)  # no FIRM_DATABASE_URL configured
    with pytest.raises(RuntimeError):
        Firm(app)


def test_embed_workers_start_and_stop(queue_url) -> None:
    app = Flask(__name__)
    app.config["FIRM_DATABASE_URL"] = queue_url
    ext = Firm(app, embed_workers=True)
    try:
        assert ext._supervisor is not None  # a supervisor is running in-process
    finally:
        ext.stop()
    assert ext._supervisor is None
    ext.stop()  # a second stop is a harmless no-op
    set_runtime(None)


def test_double_init_does_not_leak_supervisor(queue_url) -> None:
    app = Flask(__name__)
    app.config["FIRM_DATABASE_URL"] = queue_url
    ext = Firm(app, embed_workers=True)
    first = ext._supervisor
    try:
        ext.init_app(app)  # re-init must stop the first supervisor before starting a new one
        assert first is not None
        assert ext._supervisor is not first
    finally:
        ext.stop()
    set_runtime(None)
