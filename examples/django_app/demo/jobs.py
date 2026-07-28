"""`<app>/jobs.py` is imported automatically by `firm.queue.contrib.django`, in every process —
which is what lets a worker resolve these jobs without any import wiring of yours.

`@bq.job` makes a function enqueueable and leaves it callable, so `charge_order(1)` in a Django
test runs the body inline with no worker involved.
"""

from __future__ import annotations

from django.core.cache import cache
from django.db import transaction

import firm.queue as bq
from demo.models import Order
from firm.channel.contrib.django import channel


@bq.job(queue="billing", attempts=3, backoff=2.0)
def charge_order(order_id: int) -> None:
    # No connection bookkeeping here: the app config registers an `around_perform` middleware
    # that closes Django's ORM connections after every job, the way a request does.
    with transaction.atomic():
        order = Order.objects.get(pk=order_id)
        order.charged = True
        order.save(update_fields=["charged"])

    cache.delete(f"order:{order_id}")  # the cached read is now stale
    print(f"  [job] charged order {order_id} ({order.amount_cents} cents)")

    # A non-Django service pointed at the same database can subscribe to "orders" and receive
    # this. That shared store is firm's whole reason to exist here. `channel` binds itself to
    # DATABASES on first use and reuses the queue's engine, so this is one connection pool.
    channel.broadcast("orders", f'{{"order_id": {order_id}, "event": "charged"}}')
