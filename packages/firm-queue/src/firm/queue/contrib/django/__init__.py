"""Django integration — add one line to ``INSTALLED_APPS`` and firm-queue is wired up.

    # settings.py
    INSTALLED_APPS = ["myapp", "firm.queue.contrib.django"]

    # myapp/jobs.py — found automatically, no import wiring
    import firm.queue as bq

    @bq.job(queue="billing", attempts=3)
    def charge_order(order_id: int) -> None: ...

    # myapp/views.py
    from django.db import transaction
    from firm.queue.contrib.django import enqueue_on_commit

    with transaction.atomic():
        order = Order.objects.create(...)
        enqueue_on_commit(charge_order, order.pk)

The app config does three things: it configures firm from ``DATABASES`` in every process
(:mod:`firm.queue.contrib.django.apps`), it creates firm's tables from ``manage.py migrate``, and it
adds ``manage.py firm_worker``. Every knob lives in one ``FIRM_QUEUE`` settings dict — see
:mod:`firm.queue.contrib.django.conf` for the full list, all of it optional.

``enqueue_on_commit`` is the Django twin of
:func:`firm.queue.contrib.sqlalchemy.enqueue_after_commit`; read
:mod:`firm.queue.contrib.django.transaction` before enqueueing inside ``transaction.atomic()``,
because the default (a bare ``.enqueue()``) is *not* transactional.

Needs the ``[django]`` extra. Nothing here imports Django at import time, so
``import firm.queue.contrib.django`` is harmless in a process that has no Django installed — the
Django imports happen inside the app config, the management command, and the helpers below.
"""

from __future__ import annotations

from firm._core.contrib.django import sqlalchemy_url_for

from .conf import DEFAULTS, get_settings
from .transaction import enqueue_on_commit

__all__ = ["DEFAULTS", "enqueue_on_commit", "get_settings", "sqlalchemy_url_for"]
