"""Command-line entry point: ``firm-ui`` (no extra dependencies).

Binds to localhost by default — it's an internal ops tool that exposes tracebacks and destructive
actions (retry / discard / pause / clear). Add authentication (``--basic-auth-user``,
``--trust-auth-header``, or a custom ``--authenticator``) before exposing it beyond localhost; the
server refuses a non-loopback ``--host`` without auth unless you pass ``--insecure``. Tabs appear
for whichever parts (queue / cache / channel / audit) have tables in the database(s) you point it
at.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import os

from . import auth
from .auth import Authenticator
from .context import build_dashboard
from .server import create_server


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.hash_password:
        _print_password_hash(parser)
        return

    authenticator, auth_label = _build_authenticator(args, parser)

    if authenticator is None and not _is_loopback(args.host) and not args.insecure:
        parser.error(
            f"refusing to bind {args.host} without authentication. Add --basic-auth-user, "
            "--trust-auth-header, or --authenticator — or pass --insecure to override."
        )

    if not (
        args.database_url or args.queue_url or args.cache_url or args.channel_url or args.audit_url
    ):
        parser.error(
            "No database URL: pass --database-url (or a per-part --queue-url/--cache-url/"
            "--channel-url/--audit-url), or set FIRM_DATABASE_URL (or the per-module "
            "FIRM_<MODULE>_DATABASE_URL variables)."
        )

    dashboard = build_dashboard(
        database_url=args.database_url,
        queue_url=args.queue_url,
        cache_url=args.cache_url,
        channel_url=args.channel_url,
        audit_url=args.audit_url,
    )
    if not dashboard.parts:
        parser.error("No firm tables found at the given URL(s); nothing to show.")

    server = create_server(dashboard, args.host, args.port, authenticator=authenticator)
    print(
        f"firm-ui → http://{args.host}:{args.port}  "
        f"tabs: {', '.join(dashboard.parts)}  auth: {auth_label}  (Ctrl-C to stop)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        dashboard.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="firm-ui", description="A small web dashboard for firm.")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("FIRM_DATABASE_URL"),
        help="Shared SQLAlchemy URL for all parts (or set FIRM_DATABASE_URL).",
    )
    # Per-part overrides fall back to the same env vars the modules' own CLIs use.
    parser.add_argument(
        "--queue-url",
        default=os.environ.get("FIRM_QUEUE_DATABASE_URL"),
        help="Override the URL for the queue tab (or set FIRM_QUEUE_DATABASE_URL).",
    )
    parser.add_argument(
        "--cache-url",
        default=os.environ.get("FIRM_CACHE_DATABASE_URL"),
        help="Override the URL for the cache tab (or set FIRM_CACHE_DATABASE_URL).",
    )
    parser.add_argument(
        "--channel-url",
        default=os.environ.get("FIRM_CHANNEL_DATABASE_URL"),
        help="Override the URL for the channel tab (or set FIRM_CHANNEL_DATABASE_URL).",
    )
    parser.add_argument(
        "--audit-url",
        default=os.environ.get("FIRM_AUDIT_DATABASE_URL"),
        help="Override the URL for the audit tab (or set FIRM_AUDIT_DATABASE_URL).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default 8787).")

    grp = parser.add_argument_group("authentication")
    grp.add_argument(
        "--basic-auth-user",
        metavar="USER",
        help="Enable HTTP Basic auth for USER; the password comes from FIRM_UI_PASSWORD or "
        "FIRM_UI_PASSWORD_HASH.",
    )
    grp.add_argument(
        "--trust-auth-header",
        metavar="HEADER",
        help="Trust a username header set by an upstream auth proxy (e.g. X-Forwarded-User).",
    )
    grp.add_argument(
        "--trusted-proxy",
        action="append",
        metavar="IP",
        help="Peer address allowed to set --trust-auth-header (repeatable; default loopback).",
    )
    grp.add_argument(
        "--authenticator",
        metavar="module:object",
        help="Import path to a custom Authenticator (instance or no-arg class).",
    )
    grp.add_argument(
        "--insecure",
        action="store_true",
        help="Allow a non-loopback --host with no authentication (not recommended).",
    )
    grp.add_argument(
        "--hash-password",
        action="store_true",
        help="Prompt for a password, print a hash for FIRM_UI_PASSWORD_HASH, and exit.",
    )
    return parser


def _build_authenticator(
    args: argparse.Namespace, parser: argparse.ArgumentParser
) -> tuple[Authenticator | None, str]:
    active = [
        name
        for name, value in (
            ("--basic-auth-user", args.basic_auth_user),
            ("--trust-auth-header", args.trust_auth_header),
            ("--authenticator", args.authenticator),
        )
        if value
    ]
    if len(active) > 1:
        parser.error(f"choose at most one of {', '.join(active)}.")

    if args.basic_auth_user:
        password = os.environ.get("FIRM_UI_PASSWORD")
        password_hash = os.environ.get("FIRM_UI_PASSWORD_HASH")
        if not password and not password_hash:
            parser.error(
                "--basic-auth-user needs FIRM_UI_PASSWORD or FIRM_UI_PASSWORD_HASH in the "
                "environment (run with --hash-password to generate a hash)."
            )
        try:
            authn = auth.BasicAuth(
                args.basic_auth_user, password=password, password_hash=password_hash
            )
        except ValueError as exc:
            parser.error(str(exc))
        return authn, "basic"

    if args.trust_auth_header:
        proxies = set(args.trusted_proxy) if args.trusted_proxy else {"127.0.0.1", "::1"}
        return auth.ProxyHeaderAuth(args.trust_auth_header, trusted_proxies=proxies), "proxy-header"

    if args.authenticator:
        try:
            return auth.load_authenticator(args.authenticator), "custom"
        except Exception as exc:  # import / attribute / type errors surface as a clean CLI error
            parser.error(f"--authenticator could not be loaded: {exc}")

    return None, "none"


def _print_password_hash(parser: argparse.ArgumentParser) -> None:
    password = getpass.getpass("Password: ")
    if not password:
        parser.error("empty password.")
    if password != getpass.getpass("Confirm password: "):
        parser.error("passwords do not match.")
    print(auth.hash_password(password))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":  # pragma: no cover
    main()
