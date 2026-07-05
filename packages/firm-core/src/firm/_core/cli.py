"""Shared bootstrap for the module CLIs: the click import guard and --database-url plumbing.

firm-core itself does not depend on click — each module's CLI brings it; these helpers import
it lazily so importing ``firm._core`` never requires click. Parametrized the same way
``alembic_env.run_migrations`` is, so the four CLIs cannot drift apart.
"""

from __future__ import annotations

import os
from typing import Any


def require_click(extra: str) -> Any:
    """Import click, or raise the standard install hint for ``firm-<extra>``'s CLI."""
    try:
        import click
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            f'The firm-{extra} CLI requires "click". Install the {extra} extra: '
            f'pip install "firm[{extra}]"'
        ) from exc
    return click


def db_option(env_var: str) -> Any:
    """The standard ``--database-url`` option, documenting the module's env-var fallback."""
    import click

    return click.option(
        "--database-url",
        default=None,
        help=f"SQLAlchemy URL (or set {env_var}).",
    )


def require_url(database_url: str | None, env_var: str) -> str:
    """Resolve the database URL from the option or the environment, or fail with usage help."""
    import click

    url = database_url or os.environ.get(env_var)
    if not url:
        raise click.UsageError(f"No database URL: pass --database-url or set {env_var}.")
    return url
