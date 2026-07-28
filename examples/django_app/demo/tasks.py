"""The same queue through Django 6's official interface, via the `TASKS` backend in settings.py.

`@task` and `@bq.job` end up in the same `firm_queue_jobs` table and are run by the same
`manage.py firm_worker`. What you give up on this side is documented in docs/django.md: no
result retrieval, no `takes_context`, and retries are firm's registry defaults rather than a
per-task setting.
"""

from __future__ import annotations

from django.tasks import task

from demo.models import Order


@task(queue_name="billing", priority=10)
def send_receipt(order_id: int) -> None:
    order = Order.objects.get(pk=order_id)
    print(f"  [task] receipt for order {order_id} -> {order.email}")
