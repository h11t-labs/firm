"""Specs for the Flask extension integration."""

from __future__ import annotations

import warnings

import pytest

pytest.importorskip("flask")

from flask import Flask
from sqlalchemy import func, select

import firm.queue as bq
from firm.queue import schema
from firm.queue.config import current_runtime, set_runtime
from firm.queue.contrib.flask import FirmQueue


@bq.job()
def _gjob(x: int) -> None:
    pass


def test_extension_configures_and_enqueues(queue_url) -> None:
    app = Flask(__name__)
    app.config["FIRM_QUEUE_DATABASE_URL"] = queue_url
    FirmQueue(app)

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
    app.config["FIRM_QUEUE_DATABASE_URL"] = queue_url
    FirmQueue(app)
    try:
        assert "firm-queue" in app.cli.commands  # `flask firm-queue worker`
        assert "worker" in app.cli.commands["firm-queue"].commands
        assert "firm" in app.cli.commands  # the deprecated alias, removed in 2.0
    finally:
        set_runtime(None)


def test_deprecated_cli_group_warns_on_stderr(queue_url, capsys) -> None:
    """`flask firm ...` still runs, but says it has been renamed.

    Driving it through the runner would mean invoking `worker`, which blocks until interrupted,
    so this calls the group's own callback — the exact code that runs before any subcommand.
    """
    app = Flask(__name__)
    app.config["FIRM_QUEUE_DATABASE_URL"] = queue_url
    FirmQueue(app)
    try:
        assert app.cli.commands["firm"].callback is not None
        with pytest.warns(DeprecationWarning, match="flask firm-queue"):
            app.cli.commands["firm"].callback()
        assert "`flask firm` is now `flask firm-queue`" in capsys.readouterr().err

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            app.cli.commands["firm-queue"].callback()  # the current name stays quiet
        assert not [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert capsys.readouterr().err == ""
    finally:
        set_runtime(None)


def test_extension_registered_under_both_keys(queue_url) -> None:
    app = Flask(__name__)
    app.config["FIRM_QUEUE_DATABASE_URL"] = queue_url
    ext = FirmQueue(app)
    try:
        assert app.extensions["firm_queue"] is ext
        assert app.extensions["firm"] is ext  # deprecated alias, removed in 2.0
    finally:
        set_runtime(None)


def test_queue_key_wins_over_the_shared_one(queue_url, tmp_path) -> None:
    """FIRM_DATABASE_URL configures every module; the queue's own key overrides it."""
    app = Flask(__name__)
    app.config["FIRM_DATABASE_URL"] = f"sqlite:///{tmp_path / 'shared.db'}"
    app.config["FIRM_QUEUE_DATABASE_URL"] = queue_url
    with pytest.warns(RuntimeWarning, match="takes precedence"):
        ext = FirmQueue(app)
    try:
        assert ext.runtime is not None
        assert str(ext.runtime.engine.url) == queue_url
    finally:
        set_runtime(None)


def test_conflicting_config_warns_that_the_queue_moved(queue_url, tmp_path) -> None:
    """1.0.0 ignored the app.config queue key, so honouring it can move a live queue.

    The jobs already enqueued in the old database do not follow, and nothing else in the app
    would say so — this warning is the only signal the operator gets.
    """
    app = Flask(__name__)
    old_db = f"sqlite:///{tmp_path / 'where-the-jobs-are.db'}"
    app.config["FIRM_DATABASE_URL"] = old_db
    app.config["FIRM_QUEUE_DATABASE_URL"] = queue_url
    with pytest.warns(RuntimeWarning) as caught:
        FirmQueue(app)
    try:
        message = str(caught[0].message)
        assert old_db in message  # names the database being left behind
        assert queue_url in message  # and the one taking over
    finally:
        set_runtime(None)


def test_matching_config_under_both_keys_is_silent(queue_url) -> None:
    """Belt-and-braces config is not a conflict: same URL, nothing moves, no warning."""
    app = Flask(__name__)
    app.config["FIRM_DATABASE_URL"] = queue_url
    app.config["FIRM_QUEUE_DATABASE_URL"] = queue_url
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        FirmQueue(app)
    try:
        assert not [w for w in caught if issubclass(w.category, RuntimeWarning)]
    finally:
        set_runtime(None)


def test_shared_key_is_used_when_the_queue_key_is_absent(queue_url) -> None:
    app = Flask(__name__)
    app.config["FIRM_DATABASE_URL"] = queue_url
    ext = FirmQueue(app)
    try:
        assert ext.runtime is not None
        assert str(ext.runtime.engine.url) == queue_url
    finally:
        set_runtime(None)


def test_environment_is_the_last_resort(queue_url, monkeypatch) -> None:
    monkeypatch.setenv("FIRM_QUEUE_DATABASE_URL", queue_url)
    app = Flask(__name__)  # nothing in app.config
    ext = FirmQueue(app)
    try:
        assert ext.runtime is not None
        assert str(ext.runtime.engine.url) == queue_url
    finally:
        set_runtime(None)


def test_missing_url_raises(monkeypatch) -> None:
    monkeypatch.delenv("FIRM_QUEUE_DATABASE_URL", raising=False)
    monkeypatch.delenv("FIRM_DATABASE_URL", raising=False)
    app = Flask(__name__)  # no database URL anywhere
    with pytest.raises(RuntimeError):
        FirmQueue(app)


def test_old_class_name_warns_and_is_the_same_class() -> None:
    """`Firm` shipped in 1.0.0; it has to stay the *same* class, not a subclass."""
    import firm.queue.contrib.flask as mod

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        old = mod.Firm
    messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    assert any("FirmQueue" in m for m in messages), messages
    assert any("2.0" in m for m in messages), messages
    # Against the module's own attribute, not this file's import: a sibling test reloads the
    # module, which rebinds the class object.
    assert old is mod.FirmQueue


def test_unknown_attribute_still_raises_attribute_error() -> None:
    import firm.queue.contrib.flask as mod

    with pytest.raises(AttributeError):
        _ = mod.NoSuchThing


def test_embed_workers_start_and_stop(queue_url) -> None:
    app = Flask(__name__)
    app.config["FIRM_QUEUE_DATABASE_URL"] = queue_url
    ext = FirmQueue(app, embed_workers=True)
    try:
        assert ext._supervisor is not None  # a supervisor is running in-process
    finally:
        ext.stop()
    assert ext._supervisor is None
    ext.stop()  # a second stop is a harmless no-op
    set_runtime(None)


def test_double_init_does_not_leak_supervisor(queue_url) -> None:
    app = Flask(__name__)
    app.config["FIRM_QUEUE_DATABASE_URL"] = queue_url
    ext = FirmQueue(app, embed_workers=True)
    first = ext._supervisor
    try:
        ext.init_app(app)  # re-init must stop the first supervisor before starting a new one
        assert first is not None
        assert ext._supervisor is not first
    finally:
        ext.stop()
    set_runtime(None)
