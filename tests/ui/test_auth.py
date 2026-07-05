"""Specs for dashboard authentication: the backends, the import loader, the CLI guards, and the
server chokepoint."""

from __future__ import annotations

import base64
import getpass
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from firm.ui import cli
from firm.ui.auth import (
    Allow,
    AuthRequest,
    BasicAuth,
    Deny,
    ProxyHeaderAuth,
    hash_password,
    load_authenticator,
    verify_password,
)
from firm.ui.server import create_server


class _Headers(dict):
    def get(self, name: str, default: str = "") -> str:
        return dict.get(self, name, default)


def _req(headers=None, *, addr="127.0.0.1", method="GET", path="/") -> AuthRequest:
    return AuthRequest(method=method, path=path, headers=_Headers(headers or {}), client_addr=addr)


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# -- password hashing --------------------------------------------------------------------------


def test_password_hash_roundtrip() -> None:
    encoded = hash_password("hunter2", rounds=1000)
    assert verify_password("hunter2", encoded)
    assert not verify_password("nope", encoded)
    assert not verify_password("hunter2", "not-a-valid-hash")  # malformed -> False, never raises


# -- BasicAuth ---------------------------------------------------------------------------------


def test_basic_auth_plaintext() -> None:
    auth = BasicAuth("admin", password="s3cret")
    assert isinstance(auth.authenticate(_req()), Deny)  # no Authorization header
    assert isinstance(auth.authenticate(_req({"Authorization": _basic("admin", "x")})), Deny)
    assert isinstance(auth.authenticate(_req({"Authorization": _basic("nope", "s3cret")})), Deny)
    ok = auth.authenticate(_req({"Authorization": _basic("admin", "s3cret")}))
    assert isinstance(ok, Allow) and ok.user == "admin"


def test_basic_auth_challenge_is_401_with_header() -> None:
    deny = BasicAuth("admin", password="x").authenticate(_req())
    assert isinstance(deny, Deny) and deny.status == 401
    assert deny.headers.get("WWW-Authenticate", "").startswith("Basic ")


def test_basic_auth_hashed() -> None:
    auth = BasicAuth("admin", password_hash=hash_password("hunter2", rounds=1000))
    assert isinstance(auth.authenticate(_req({"Authorization": _basic("admin", "wrong")})), Deny)
    assert isinstance(auth.authenticate(_req({"Authorization": _basic("admin", "hunter2")})), Allow)


def test_basic_auth_requires_a_secret() -> None:
    with pytest.raises(ValueError):
        BasicAuth("admin")
    with pytest.raises(ValueError):
        BasicAuth("admin", password_hash="garbage")  # not a hash_password() value


# -- ProxyHeaderAuth ---------------------------------------------------------------------------


def test_proxy_header_auth() -> None:
    auth = ProxyHeaderAuth("X-Forwarded-User", trusted_proxies={"127.0.0.1"})
    spoof = auth.authenticate(_req({"X-Forwarded-User": "admin"}, addr="10.0.0.9"))
    assert isinstance(spoof, Deny) and spoof.status == 403  # untrusted peer cannot set the header
    assert isinstance(auth.authenticate(_req({}, addr="127.0.0.1")), Deny)  # trusted, but no header
    ok = auth.authenticate(_req({"X-Forwarded-User": "alice"}, addr="127.0.0.1"))
    assert isinstance(ok, Allow) and ok.user == "alice"


# -- custom authenticator loading --------------------------------------------------------------


def test_load_authenticator(tmp_path, monkeypatch) -> None:
    (tmp_path / "myauth.py").write_text(
        "from firm.ui.auth import Allow\n"
        "class MyAuth:\n"
        "    def authenticate(self, req):\n"
        "        return Allow('bob')\n"
        "instance = MyAuth()\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert load_authenticator("myauth:instance").authenticate(_req()).user == "bob"
    assert load_authenticator("myauth:MyAuth").authenticate(_req()).user == "bob"  # class -> ()
    with pytest.raises(TypeError):
        load_authenticator("firm.ui.auth:Allow")  # importable, but not an Authenticator
    with pytest.raises(ValueError):
        load_authenticator("no-colon-path")


# -- CLI guards --------------------------------------------------------------------------------


def test_cli_refuses_exposed_bind_without_auth() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--database-url", "sqlite:///unused.db", "--host", "0.0.0.0"])


def test_cli_is_loopback() -> None:
    assert cli._is_loopback("127.0.0.1")
    assert cli._is_loopback("::1")
    assert cli._is_loopback("localhost")
    assert not cli._is_loopback("0.0.0.0")
    assert not cli._is_loopback("192.168.1.5")


def test_cli_hash_password(monkeypatch, capsys) -> None:
    monkeypatch.setattr(getpass, "getpass", lambda *a, **k: "secret")
    cli.main(["--hash-password"])  # prints a hash and returns; needs no database URL
    assert verify_password("secret", capsys.readouterr().out.strip())


# -- server chokepoint -------------------------------------------------------------------------


@contextmanager
def _running(dashboard, authenticator) -> Iterator[str]:
    server = create_server(dashboard, "127.0.0.1", 0, authenticator=authenticator)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_server_enforces_basic_auth(dashboard, seed) -> None:
    seed.ready()
    with _running(dashboard, BasicAuth("admin", password="pw")) as base:
        with pytest.raises(HTTPError) as exc:
            urlopen(base + "/")
        assert exc.value.code == 401
        assert exc.value.headers.get("WWW-Authenticate", "").startswith("Basic ")
        req = Request(base + "/", headers={"Authorization": _basic("admin", "pw")})
        with urlopen(req) as resp:  # correct credentials -> the page loads
            assert resp.status == 200
            assert "Overview" in resp.read().decode()


def test_server_blocks_unauthenticated_post(dashboard, seed) -> None:
    seed.cache_entry(key=b"keep-me")
    with _running(dashboard, BasicAuth("admin", password="pw")) as base:
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(base + "/cache/clear", data=b""))  # no auth -> 401 before the action
        assert exc.value.code == 401
        creds = {"Authorization": _basic("admin", "pw")}
        with urlopen(Request(base + "/cache", headers=creds)) as resp:
            assert "keep-me" in resp.read().decode()  # the destructive action did not run
