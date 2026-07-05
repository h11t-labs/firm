"""Authentication for the dashboard — one pluggable chokepoint, standard-library only.

If an :class:`Authenticator` is configured, the server runs it at the top of every request (before
routing, alongside the existing CSRF ``Origin`` check). Three implementations ship:

* :class:`BasicAuth` — HTTP Basic, with the secret read from the environment as either a plaintext
  ``FIRM_UI_PASSWORD`` or a hashed ``FIRM_UI_PASSWORD_HASH`` (see :func:`hash_password`).
* :class:`ProxyHeaderAuth` — trust a username header set by an upstream authenticating proxy
  (oauth2-proxy, Cloudflare Access, nginx ``auth_request`` …), but only from a trusted peer.
* anything you write — implement :class:`Authenticator` and pass it to ``serve(authenticator=…)``,
  or point ``firm-ui --authenticator package.module:object`` at it.

Auth is one layer. Basic credentials travel in clear text, so keep the bind on loopback or put TLS
in front; and a return value of :class:`Deny` decides the exact response (a ``401`` challenge, a
``403``, or a ``302`` to your own login).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import importlib
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from email.message import Message

_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ROUNDS = 200_000


@dataclass(frozen=True)
class AuthRequest:
    """The parts of an incoming request an authenticator may inspect."""

    method: str
    path: str
    headers: Message[str, str]  # the server's parsed request headers (an http.client.HTTPMessage)
    client_addr: str

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name, default)


@dataclass(frozen=True)
class Allow:
    """Authentication succeeded; ``user`` is recorded for the request (optional)."""

    user: str | None = None


@dataclass(frozen=True)
class Deny:
    """Authentication failed: the status, headers, and body to send back."""

    status: int = 401
    headers: dict[str, str] = field(default_factory=dict)
    message: str = "Authentication required."


@runtime_checkable
class Authenticator(Protocol):
    def authenticate(self, req: AuthRequest) -> Allow | Deny: ...


# -- password hashing (for FIRM_UI_PASSWORD_HASH) ---------------------------------------------


def hash_password(password: str, *, rounds: int = _PBKDF2_ROUNDS) -> str:
    """Return a self-describing PBKDF2-HMAC-SHA256 hash string for ``password``."""
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"{_PBKDF2_ALGO}${rounds}${_b64(salt)}${_b64(derived)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a :func:`hash_password` string."""
    try:
        algo, rounds, salt_b64, hash_b64 = encoded.split("$")
        salt, expected = _unb64(salt_b64), _unb64(hash_b64)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
    except (ValueError, binascii.Error):
        return False
    return algo == _PBKDF2_ALGO and hmac.compare_digest(derived, expected)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# -- built-in authenticators -------------------------------------------------------------------


class BasicAuth:
    """HTTP Basic auth against one username and a plaintext password or a hashed one."""

    def __init__(
        self,
        username: str,
        *,
        password: str | None = None,
        password_hash: str | None = None,
        realm: str = "firm",
    ) -> None:
        if not password and not password_hash:
            raise ValueError("BasicAuth needs a password or a password_hash.")
        if password_hash and not password_hash.startswith(f"{_PBKDF2_ALGO}$"):
            raise ValueError("password_hash must be a value produced by hash_password().")
        self._username = username
        self._password = password
        self._password_hash = password_hash
        self._challenge = {"WWW-Authenticate": f'Basic realm="{realm}", charset="UTF-8"'}
        self._last_ok: str | None = None  # cache the last good header to skip re-hashing

    def authenticate(self, req: AuthRequest) -> Allow | Deny:
        header = req.header("Authorization")
        # The browser re-sends credentials on every request (and the dashboard auto-refreshes), so
        # short-circuit a repeat of the last accepted header instead of hashing again each time.
        if self._last_ok is not None and hmac.compare_digest(header, self._last_ok):
            return Allow(self._username)
        if not header.startswith("Basic "):
            return self._deny()
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return self._deny()
        # Evaluate both checks regardless, so timing does not reveal whether the username matched.
        if not (self._user_ok(user) & self._password_ok(password)):
            return self._deny()
        self._last_ok = header
        return Allow(self._username)

    def _user_ok(self, user: str) -> bool:
        return hmac.compare_digest(user.encode("utf-8"), self._username.encode("utf-8"))

    def _password_ok(self, password: str) -> bool:
        if self._password is not None:
            return hmac.compare_digest(password.encode("utf-8"), self._password.encode("utf-8"))
        return verify_password(password, self._password_hash or "")

    def _deny(self) -> Deny:
        return Deny(401, dict(self._challenge))


class ProxyHeaderAuth:
    """Trust a username header injected by an upstream authenticating proxy.

    The header is honoured only when the immediate peer is one of ``trusted_proxies`` — otherwise
    anyone who can reach the port could spoof it. Bind the dashboard so that only the proxy can
    connect to it (e.g. loopback, with the proxy on the same host).
    """

    def __init__(self, header: str, *, trusted_proxies: set[str]) -> None:
        self._header = header
        self._trusted = set(trusted_proxies)

    def authenticate(self, req: AuthRequest) -> Allow | Deny:
        if req.client_addr not in self._trusted:
            return Deny(403, message="Reach the dashboard through the authenticating proxy.")
        user = req.header(self._header)
        if not user:
            return Deny(401, message=f"Missing {self._header} from the proxy.")
        return Allow(user)


def load_authenticator(path: str) -> Authenticator:
    """Import an :class:`Authenticator` from a ``package.module:object`` path.

    ``object`` may be an authenticator instance or a no-argument class/factory yielding one.
    """
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError("Use 'package.module:object' for the authenticator path.")
    obj = getattr(importlib.import_module(module_name), attr)
    if isinstance(obj, type):
        obj = obj()
    if not hasattr(obj, "authenticate"):
        raise TypeError(f"{path} is not an Authenticator (no .authenticate method).")
    return obj
