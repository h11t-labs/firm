"""Specs for the Django DATABASES -> SQLAlchemy URL mapping.

Deliberately Django-free: the helper takes a plain mapping, so these tests feed it dicts and
never install or configure Django (there is a spec below that keeps it that way).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import make_url, text

from firm._core.contrib import django as django_contrib
from firm._core.contrib.django import sqlalchemy_url_for
from firm._core.database import create_engine_for

PG = "django.db.backends.postgresql"
MYSQL = "django.db.backends.mysql"
SQLITE = "django.db.backends.sqlite3"


def _pg(**overrides):
    base = {
        "ENGINE": PG,
        "NAME": "app",
        "USER": "app_user",
        "PASSWORD": "s3cret",
        "HOST": "db.internal",
        "PORT": "5432",
        "OPTIONS": {},
    }
    return {**base, **overrides}


# --- PostgreSQL ------------------------------------------------------------------------


def test_postgres_full_settings() -> None:
    url = sqlalchemy_url_for(_pg())
    assert url == "postgresql+psycopg://app_user:s3cret@db.internal:5432/app"


def test_postgres_uses_the_driver_firm_ships() -> None:
    # The mapping leans on normalize_url rather than spelling +psycopg out itself.
    assert make_url(sqlalchemy_url_for(_pg())).drivername == "postgresql+psycopg"


def test_postgres_empty_host_means_local_socket() -> None:
    # No netloc host at all: libpq then uses its default unix socket directory.
    url = sqlalchemy_url_for(_pg(HOST="", PORT=""))
    assert url == "postgresql+psycopg://app_user:s3cret@/app"
    assert make_url(url).host is None


def test_postgres_socket_directory_host_moves_into_the_query() -> None:
    url = sqlalchemy_url_for(_pg(HOST="/var/run/postgresql", PORT=""))
    parsed = make_url(url)
    assert parsed.host is None
    assert parsed.query == {"host": "/var/run/postgresql"}


def test_postgres_empty_port_is_omitted() -> None:
    assert (
        sqlalchemy_url_for(_pg(PORT="")) == "postgresql+psycopg://app_user:s3cret@db.internal/app"
    )


def test_postgres_integer_port_is_accepted() -> None:
    assert make_url(sqlalchemy_url_for(_pg(PORT=6432))).port == 6432


def test_password_with_special_characters_is_quoted() -> None:
    secret = "p@ss:w/rd#1?&="  # noqa: S105 - a fixture password made of URL metacharacters
    url = sqlalchemy_url_for(_pg(PASSWORD=secret))
    assert secret not in url  # it had to be percent-encoded to survive
    assert make_url(url).password == secret
    assert make_url(url).host == "db.internal"  # the '@' did not split the netloc early


def test_username_with_special_characters_is_quoted() -> None:
    url = sqlalchemy_url_for(_pg(USER="tenant@example.com"))
    assert make_url(url).username == "tenant@example.com"


def test_password_without_user_is_dropped() -> None:
    # A URL cannot carry a password without a username; better an explicit auth failure than a
    # malformed netloc.
    assert (
        sqlalchemy_url_for(_pg(USER="", PASSWORD="x"))
        == "postgresql+psycopg://db.internal:5432/app"
    )


def test_missing_optional_keys_do_not_raise() -> None:
    # settings.DATABASES entries are often just ENGINE + NAME; Django fills the rest in later.
    assert sqlalchemy_url_for({"ENGINE": PG, "NAME": "app"}) == "postgresql+psycopg:///app"


def test_postgres_options_become_query_parameters() -> None:
    url = sqlalchemy_url_for(_pg(OPTIONS={"sslmode": "require", "connect_timeout": 10}))
    assert make_url(url).query == {"sslmode": "require", "connect_timeout": "10"}


def test_postgres_django_only_options_are_dropped() -> None:
    # Django's own backend pops these in get_connection_params(); psycopg would reject them.
    options = {"pool": True, "isolation_level": 2, "server_side_binding": True, "assume_role": "r"}
    assert make_url(sqlalchemy_url_for(_pg(OPTIONS=options))).query == {}


def test_boolean_option_renders_as_true_false() -> None:
    url = sqlalchemy_url_for(_pg(OPTIONS={"keepalives": True, "sslcompression": False}))
    assert make_url(url).query == {"keepalives": "true", "sslcompression": "false"}


def test_postgres_service_replaces_a_missing_name() -> None:
    url = sqlalchemy_url_for({"ENGINE": PG, "NAME": "", "OPTIONS": {"service": "app"}})
    parsed = make_url(url)
    assert parsed.database is None
    assert parsed.query == {"service": "app"}


def test_legacy_psycopg2_engine_is_recognised() -> None:
    settings = _pg(ENGINE="django.db.backends.postgresql_psycopg2")
    assert sqlalchemy_url_for(settings) == sqlalchemy_url_for(_pg())


# --- MySQL -----------------------------------------------------------------------------


def test_mysql_full_settings() -> None:
    url = sqlalchemy_url_for(
        {
            "ENGINE": MYSQL,
            "NAME": "app",
            "USER": "app_user",
            "PASSWORD": "s3cret",
            "HOST": "db.internal",
            "PORT": 3306,
            "OPTIONS": {"charset": "utf8mb4"},
        }
    )
    assert url == "mysql+pymysql://app_user:s3cret@db.internal:3306/app?charset=utf8mb4"


def test_mysql_socket_host_becomes_unix_socket() -> None:
    url = sqlalchemy_url_for(
        {
            "ENGINE": MYSQL,
            "NAME": "app",
            "USER": "u",
            "HOST": "/var/run/mysqld/mysqld.sock",
            "PORT": "",
        }
    )
    parsed = make_url(url)
    assert parsed.host is None
    assert parsed.query == {"unix_socket": "/var/run/mysqld/mysqld.sock"}


def test_mysql_nested_ssl_dict_is_flattened() -> None:
    # SQLAlchemy's MySQL dialect rebuilds the driver's ssl={...} from these flat arguments, so
    # TLS settings must never be silently dropped.
    url = sqlalchemy_url_for(
        {
            "ENGINE": MYSQL,
            "NAME": "app",
            "HOST": "db.internal",
            "OPTIONS": {"ssl": {"ca": "/etc/ssl/ca.pem", "check_hostname": True}},
        }
    )
    assert make_url(url).query == {"ssl_ca": "/etc/ssl/ca.pem", "ssl_check_hostname": "true"}


def test_mysql_isolation_level_is_dropped() -> None:
    settings = {"ENGINE": MYSQL, "NAME": "app", "OPTIONS": {"isolation_level": "read committed"}}
    assert make_url(sqlalchemy_url_for(settings)).query == {}


# --- SQLite ----------------------------------------------------------------------------


def test_sqlite_path_name(tmp_path) -> None:
    # Django's default is BASE_DIR / "db.sqlite3" — a Path, and absolute.
    name = tmp_path / "db.sqlite3"
    url = sqlalchemy_url_for({"ENGINE": SQLITE, "NAME": name})
    assert url == f"sqlite:///{name}"  # four slashes total: an absolute path
    assert make_url(url).database == str(name)


def test_sqlite_relative_name() -> None:
    assert sqlalchemy_url_for({"ENGINE": SQLITE, "NAME": "db.sqlite3"}) == "sqlite:///db.sqlite3"


def test_sqlite_memory_name() -> None:
    assert sqlalchemy_url_for({"ENGINE": SQLITE, "NAME": ":memory:"}) == "sqlite://"


def test_sqlite_shared_memory_test_database_is_refused() -> None:
    settings = {"ENGINE": SQLITE, "NAME": "file:memorydb_default?mode=memory&cache=shared"}
    with pytest.raises(ValueError, match="in-memory SQLite test database"):
        sqlalchemy_url_for(settings)


def test_sqlite_options_are_ignored() -> None:
    # firm configures the SQLite connection itself in create_engine_for.
    settings = {"ENGINE": SQLITE, "NAME": "db.sqlite3", "OPTIONS": {"timeout": 20, "uri": True}}
    assert sqlalchemy_url_for(settings) == "sqlite:///db.sqlite3"


def test_sqlite_without_name_raises() -> None:
    with pytest.raises(ValueError, match="no NAME"):
        sqlalchemy_url_for({"ENGINE": SQLITE, "NAME": ""})


# --- Errors ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "engine",
    ["django.db.backends.oracle", "django.contrib.gis.db.backends.postgis", "", "postgresql"],
)
def test_unsupported_engine_raises_an_actionable_error(engine: str) -> None:
    with pytest.raises(ValueError) as exc:
        sqlalchemy_url_for({"ENGINE": engine, "NAME": "app"})
    message = str(exc.value)
    assert repr(engine) in message  # says which backend it choked on
    assert "django.db.backends.postgresql" in message  # ... and which ones work
    assert "database_url=" in message  # ... and what to do instead


def test_missing_engine_key_is_not_a_key_error() -> None:
    with pytest.raises(ValueError):
        sqlalchemy_url_for({"NAME": "app"})


def test_non_scalar_option_raises_rather_than_being_dropped() -> None:
    settings = _pg(OPTIONS={"cursor_factory": object()})
    with pytest.raises(ValueError, match="cursor_factory"):
        sqlalchemy_url_for(settings)


def test_non_scalar_mysql_ssl_member_raises() -> None:
    settings = {"ENGINE": MYSQL, "NAME": "app", "OPTIONS": {"ssl": {"ca": ["a", "b"]}}}
    with pytest.raises(ValueError, match=r"ssl\.ca"):
        sqlalchemy_url_for(settings)


# --- The URL is one firm can actually use ------------------------------------------------


def test_sqlite_url_builds_a_working_engine(tmp_path) -> None:
    url = sqlalchemy_url_for({"ENGINE": SQLITE, "NAME": tmp_path / "db.sqlite3"})
    engine = create_engine_for(url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("select 1")).scalar() == 1
    finally:
        engine.dispose()


def test_helper_never_imports_django() -> None:
    """firm-core must stay Django-free — this is a pure dict -> str mapping."""
    tree = ast.parse(Path(django_contrib.__file__).read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert "django" not in roots
