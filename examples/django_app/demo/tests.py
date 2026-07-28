"""`python manage.py test demo`

Note the base class: **`TransactionTestCase`, not `TestCase`**. `TestCase` wraps each test in a
transaction it never commits, on Django's connection. firm reads and writes on its *own*
connection, so it cannot see those rows — and on SQLite it cannot even write while Django
holds the write lock ("database is locked"). `TransactionTestCase` commits for real, and it is
also the only class where `on_commit` callbacks fire without `captureOnCommitCallbacks`.

Django's flush between `TransactionTestCase` methods only truncates tables Django knows about,
so firm's tables have to be cleared explicitly.

Two ways to test a job, both without a worker process: call it directly (`@bq.job` leaves the
function callable), or enqueue it and `run_ready()` once, which exercises argument
serialization and the job registry too.
"""

from __future__ import annotations

from django.core.cache import cache
from django.db import transaction
from django.test import TransactionTestCase

import firm.queue as bq
from demo.jobs import charge_order
from demo.models import Order
from demo.tasks import send_receipt
from firm.queue.contrib.django import enqueue_on_commit
from firm.queue.queues import clear
from firm.queue.worker import run_ready


class JobTest(TransactionTestCase):
    def setUp(self) -> None:
        clear(bq.current_runtime(), "billing")
        cache.clear()

    def test_call_the_body_directly(self) -> None:
        order = Order.objects.create(email="a@example.com", amount_cents=100)
        charge_order(order.pk)  # no enqueue, no worker
        order.refresh_from_db()
        self.assertTrue(order.charged)

    def test_enqueue_then_drain(self) -> None:
        order = Order.objects.create(email="b@example.com", amount_cents=250)
        with transaction.atomic():
            enqueue_on_commit(charge_order, order.pk)

        self.assertEqual(run_ready(bq.current_runtime(), limit=10), 1)
        order.refresh_from_db()
        self.assertTrue(order.charged)

    def test_rollback_does_not_enqueue(self) -> None:
        """enqueue_on_commit is what makes this true; a bare enqueue() would survive the
        rollback and leave a job pointing at a row that never existed."""
        with self.assertRaises(RuntimeError), transaction.atomic():
            order = Order.objects.create(email="c@example.com", amount_cents=1)
            enqueue_on_commit(charge_order, order.pk)
            raise RuntimeError("boom")

        self.assertEqual(run_ready(bq.current_runtime(), limit=10), 0)

    def test_django_task_runs_on_the_same_queue(self) -> None:
        """A django.tasks task is an ordinary firm job once enqueued, so the same drain runs it."""
        order = Order.objects.create(email="d@example.com", amount_cents=500)
        send_receipt.enqueue(order.pk)

        self.assertEqual(run_ready(bq.current_runtime(), limit=10), 1)


class CacheTest(TransactionTestCase):
    def test_roundtrip(self) -> None:
        cache.set("k", {"v": 1})
        self.assertEqual(cache.get("k"), {"v": 1})
