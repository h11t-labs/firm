"""Tie a job enqueue to Django's transaction — the Django twin of
:func:`firm.queue.contrib.sqlalchemy.enqueue_after_commit`.

    from django.db import transaction
    from firm.queue.contrib.django import enqueue_on_commit

    with transaction.atomic():
        order = Order.objects.create(...)
        enqueue_on_commit(charge_order, order.pk)

**The default is not this.** ``charge_order.enqueue(order.pk)`` writes on firm's own
SQLAlchemy connection, immediately — firm never sees Django's connection, so it cannot join
Django's transaction. Outside ``atomic()`` a bare ``.enqueue()`` is exactly right. *Inside*
one it is wrong twice over: on PostgreSQL and MySQL the job is committed straight away and
survives a rollback, then fails on a row that never existed; on SQLite it blocks on the write
lock Django is holding until ``busy_timeout`` expires and raises "database is locked".

``enqueue_on_commit`` hands the enqueue to ``django.db.transaction.on_commit``: it runs after
Django commits (and releases that lock), and is dropped if the transaction rolls back. Outside
``atomic()`` Django runs the callback immediately, so the helper is always safe to use.

Two consequences worth knowing. The guarantee is "enqueue if and only if the transaction
committed", not one atomic write — a crash in the window between Django's commit and firm's
insert loses the enqueue, the same trade-off the SQLAlchemy helper documents. And there is no
return value: ``on_commit`` discards its callback's, so the ``job_id`` is unavailable.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from firm.queue.job import Job


def _require_django() -> None:
    try:
        import django  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the 'django' extra
        raise ImportError(
            'The firm Django integration requires "django". Install the django extra: '
            'pip install "firm-queue[django]"'
        ) from exc


def enqueue_on_commit(job: Job, *args: Any, **kwargs: Any) -> None:
    """Enqueue ``job`` (a ``@bq.job``) once the current Django transaction commits.

    ``args``/``kwargs`` are the job's own, so this takes no ``using=`` of its own: it registers
    on the connection ``FIRM_QUEUE["DATABASE_ALIAS"]`` names. To hang an enqueue off a
    transaction on some other alias, call Django directly —
    ``transaction.on_commit(partial(job.enqueue, ...), using="shard1")`` — which is also how
    you defer ``enqueue_at`` / ``enqueue_in``.
    """
    _require_django()
    from django.db import transaction

    from .conf import get_settings

    transaction.on_commit(
        partial(job.enqueue, *args, **kwargs), using=get_settings()["DATABASE_ALIAS"]
    )
