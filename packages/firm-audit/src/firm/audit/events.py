"""Row-level audit I/O: the append-only write primitive and the read query.

``append`` is the *only* function in this package that inserts a row — both the module-level
``firm.audit.record`` and ``AuditLog.record`` funnel through it, which is what makes the
append-only contract enforceable: there is one writer, and it only inserts. (The only other
mutator anywhere in the package is :mod:`.retention`'s opt-in, age-based pruning.)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple, Union

from sqlalchemy import Connection, select

from .._core.clock import now_utc
from . import schema
from .serialization import dump_json, load_json

_audits = schema.audits


class Ref(NamedTuple):
    """An explicit audit reference; each of ``type``, ``id`` and ``name`` is independently optional.

    ``name`` is a human-readable label captured at event time (an email, a title) and stored so the
    row stays legible after the referenced record is deleted or renamed. It is display-only — no
    filter ever touches it. Use ``Ref`` when you want to attach a name or be explicit; a plain
    ``("Type", id)`` tuple, a bare ``"label"`` string, or a domain object with ``.id`` all work too.
    """

    type: str | None = None
    id: Any | None = None
    name: str | None = None


# Anything acceptable as a subject/actor: a domain object with ``.id`` (or a ``__firm_audit_ref__``
# method), an explicit ``("Type", id)`` tuple, a :class:`Ref`, a bare ``"label"`` string (a role/
# kind — stored as the *type*), or ``None``.
Reference = Union[Any, "Ref", tuple, str, None]
Subject = Reference  # backwards-compatible alias for the old name


def _norm(value: Any) -> str | None:
    """Coerce one reference part to a non-empty string, or ``None``. Empty string collapses to
    ``None`` so absent / ``None`` / ``""`` are one canonical "no value" (never the literal
    ``"None"``)."""
    if value is None:
        return None
    text = str(value)
    return text or None


def _ref(obj: Reference) -> tuple[str | None, str | None, str | None]:
    """Coerce any accepted reference form to ``(type, id, name)`` strings, each optional.

    Accepts ``None`` → all null; a bare ``"label"`` string → ``type`` (a role/kind, e.g.
    ``"cron"``); a :class:`Ref`; an explicit ``("Type", id)`` 2-tuple; an object exposing
    ``__firm_audit_ref__()`` (its return value is coerced in turn); or any domain object with
    ``.id`` → ``(ClassName, id)``.
    """
    if obj is None:
        return None, None, None
    if isinstance(obj, str):
        return _norm(obj), None, None
    if isinstance(obj, Ref):  # before the generic tuple branch — Ref *is* a tuple
        return _norm(obj.type), _norm(obj.id), _norm(obj.name)
    if isinstance(obj, tuple):
        if len(obj) != 2:
            raise TypeError(
                f"a tuple reference must be (type, id); got {len(obj)} elements. "
                "Use Ref(type, id, name=...) to attach a display name."
            )
        kind, ident = obj
        return _norm(kind), _norm(ident), None
    hook = getattr(obj, "__firm_audit_ref__", None)
    if callable(hook):
        return _ref(hook())
    if not hasattr(obj, "id"):
        raise TypeError(
            f"{type(obj).__name__} is not a valid audit reference; pass a domain object with "
            '`.id`, an ("Type", id) tuple, a Ref, or a plain "label" string.'
        )
    return type(obj).__name__, _norm(obj.id), None


def append(
    conn: Connection,
    *,
    action: str,
    subject: Reference = None,
    actor: Reference = None,
    data: dict[str, Any] | None = None,
    changes: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Insert one audit row on ``conn``. The sole mutating call on the write path."""
    subject_type, subject_id, subject_label = _ref(subject)
    actor_type, actor_id, actor_label = _ref(actor)
    conn.execute(
        _audits.insert().values(
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            subject_label=subject_label,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_label=actor_label,
            correlation_id=correlation_id,
            data=dump_json(data),
            changes=dump_json(changes),
            context=dump_json(context),
            created_at=now_utc(),
        )
    )


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "action": row.action,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "subject_label": row.subject_label,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "actor_label": row.actor_label,
        "correlation_id": row.correlation_id,
        "data": load_json(row.data),
        "changes": load_json(row.changes),
        "context": load_json(row.context),
        "created_at": row.created_at,
    }


def get(conn: Connection, event_id: int) -> dict[str, Any] | None:
    """Fetch a single event by id, or ``None`` if it doesn't exist."""
    row = conn.execute(select(_audits).where(_audits.c.id == event_id)).first()
    return None if row is None else _row_to_dict(row)


def history(
    conn: Connection,
    *,
    subject: Reference = None,
    subject_type: str | None = None,
    subject_id: Any | None = None,
    actor: Reference = None,
    actor_type: str | None = None,
    actor_id: Any | None = None,
    action: str | None = None,
    correlation_id: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read matching rows, newest first. Filters only ever hit indexed scalar columns — never
    the ``data``/``changes``/``context`` JSON-string payloads, nor the display-only labels.

    ``subject``/``actor`` filter on the ``(type, id)`` of any accepted reference (a domain object,
    an explicit tuple, a :class:`Ref`, or a bare ``"label"`` string → type only — any ``name`` is
    ignored for filtering); ``subject_type``/``subject_id``/``actor_type``/``actor_id`` filter on
    either half independently, so e.g. ``subject_type="Invoice"`` alone matches every invoice
    regardless of id. Passing both forms for the same field is a ``ValueError`` rather than
    silently picking one. A ``None`` id (paired or split) means "no filter on id", not "id is null"
    — there is no way to match rows where the id column is literally null.
    """
    if subject is not None and (subject_type is not None or subject_id is not None):
        raise ValueError("pass either subject= or subject_type=/subject_id=, not both")
    if actor is not None and (actor_type is not None or actor_id is not None):
        raise ValueError("pass either actor= or actor_type=/actor_id=, not both")

    stmt = select(_audits).order_by(_audits.c.id.desc()).limit(limit)
    if subject is not None:
        subject_type, subject_id, _ = _ref(subject)
    if subject_type is not None:
        stmt = stmt.where(_audits.c.subject_type == subject_type)
    if subject_id is not None:
        stmt = stmt.where(_audits.c.subject_id == str(subject_id))
    if actor is not None:
        actor_type, actor_id, _ = _ref(actor)
    if actor_type is not None:
        stmt = stmt.where(_audits.c.actor_type == actor_type)
    if actor_id is not None:
        stmt = stmt.where(_audits.c.actor_id == str(actor_id))
    if action is not None:
        stmt = stmt.where(_audits.c.action == action)
    if correlation_id is not None:
        stmt = stmt.where(_audits.c.correlation_id == correlation_id)
    if since is not None:
        stmt = stmt.where(_audits.c.created_at >= since)
    return [_row_to_dict(row) for row in conn.execute(stmt).all()]
