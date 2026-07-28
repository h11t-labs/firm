"""Specs for the lazy Django handles: ``firm.channel.contrib.django.channel`` and
``firm.audit.contrib.django.audit``.

These have no ``AppConfig`` — the whole point is that they need no entry in ``INSTALLED_APPS``
— so what has to be pinned down is the binding behaviour: when it resolves, when it rebinds,
and whose engine it ends up on.
"""

from __future__ import annotations

import pytest

pytest.importorskip("django")

from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from firm._core.contrib.django_handle import LazyHandle, shared_engine
from firm.queue.config import current_runtime, set_runtime


def _handle(**kwargs):
    from sqlalchemy import Engine

    built: list[Engine] = []

    def factory(engine, conf):
        built.append(engine)
        return type("Built", (), {"engine": engine, "conf": conf})()

    defaults = {"DATABASE_ALIAS": "default", "DATABASE_URL": None, "OPTION": 1}
    return LazyHandle(setting_name="FIRM_TEST", defaults=defaults, factory=factory, **kwargs), built


# --- binding ---------------------------------------------------------------------------------


def test_nothing_is_built_until_first_access(django_project) -> None:
    """Import time is too early: settings may not be final, and under `manage.py test` the
    database does not exist yet."""
    handle, built = _handle()
    assert built == []
    assert "unbound" in repr(handle)

    handle.engine  # noqa: B018 - first access is what resolves it
    assert len(built) == 1


def test_rebinds_when_the_database_changes(django_project, tmp_path) -> None:
    """The failure this prevents: `manage.py test` swaps DATABASES, and a handle built once at
    import keeps writing to the development database for the whole test run."""
    handle, built = _handle()
    first = handle.engine

    other = f"sqlite:///{tmp_path / 'swapped.db'}"
    with override_settings(FIRM_TEST={"DATABASE_URL": other}):
        second = handle.engine

    assert len(built) == 2
    assert str(second.url) == other
    assert second is not first


def test_stays_bound_while_the_database_is_unchanged(django_project) -> None:
    """Rebuilding on every attribute access would open a connection pool per call."""
    handle, built = _handle()
    handle.engine  # noqa: B018
    handle.engine  # noqa: B018
    handle.conf  # noqa: B018
    assert len(built) == 1


def test_unknown_setting_is_rejected(django_project) -> None:
    handle, _ = _handle()
    with (
        override_settings(FIRM_TEST={"OPTOIN": 2}),
        pytest.raises(ImproperlyConfigured, match="OPTOIN"),
    ):
        handle.engine  # noqa: B018


def test_unknown_alias_names_the_setting_that_is_wrong(django_project) -> None:
    handle, _ = _handle()
    with (
        override_settings(FIRM_TEST={"DATABASE_ALIAS": "nope"}),
        pytest.raises(ImproperlyConfigured, match="DATABASE_ALIAS"),
    ):
        handle.engine  # noqa: B018


# --- sharing the queue's pool ------------------------------------------------------------------


def test_shares_the_queues_engine_on_the_same_database(django_env) -> None:
    """One process talking to one database should hold one pool, not one per module."""
    handle, _ = _handle()
    assert handle.engine is current_runtime().engine


def test_opens_its_own_engine_on_a_different_database(django_env, tmp_path) -> None:
    other = f"sqlite:///{tmp_path / 'elsewhere.db'}"
    handle, _ = _handle()
    with override_settings(FIRM_TEST={"DATABASE_URL": other}):
        assert handle.engine is not current_runtime().engine


def test_shared_engine_is_none_when_the_queue_is_unconfigured(django_project) -> None:
    set_runtime(None)
    assert shared_engine("sqlite:///anything.db") is None


# --- the real handles --------------------------------------------------------------------------


def test_channel_handle_binds_to_the_django_database(django_queue) -> None:
    from firm.channel.contrib.django import channel

    assert channel.engine is current_runtime().engine
    channel.broadcast("orders", b'{"ok": true}')  # proves it is a working Channel


def test_audit_handle_binds_to_the_django_database(django_queue) -> None:
    from firm.audit.contrib.django import audit

    assert audit.engine is current_runtime().engine
    audit.record("demo.event", data={"n": 1})


def test_audit_handle_starts_no_background_threads_by_default() -> None:
    """Every gunicorn worker touches this handle; a scheduler per process would have them all
    competing over the same rows. Background work is opt-in, elsewhere."""
    from firm.audit.contrib.django import DEFAULTS

    assert not any(key.startswith("BACKGROUND_") for key in DEFAULTS)
