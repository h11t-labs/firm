from __future__ import annotations

from django.core.cache import cache
from django.db import transaction
from django.http import HttpRequest, JsonResponse

from demo.jobs import charge_order
from demo.models import Order
from demo.tasks import send_receipt
from firm.queue.contrib.django import enqueue_on_commit


def create_order(request: HttpRequest) -> JsonResponse:
    with transaction.atomic():
        order = Order.objects.create(
            email=request.POST.get("email", "demo@example.com"),
            amount_cents=int(request.POST.get("amount_cents", 4200)),
        )
        # firm writes on its own connection, so a bare enqueue() inside atomic() is not part of
        # this transaction. enqueue_on_commit defers it until Django commits — see
        # docs/django.md § Enqueueing inside a Django transaction.
        enqueue_on_commit(charge_order, order.pk)
        # The Tasks backend does the same by itself: ENQUEUE_ON_COMMIT defaults to True.
        send_receipt.enqueue(order.pk)

    return JsonResponse({"order_id": order.pk, "queued": True}, status=202)


def show_order(request: HttpRequest, order_id: int) -> JsonResponse:
    def load() -> dict[str, object]:
        order = Order.objects.get(pk=order_id)
        return {"id": order.pk, "email": order.email, "charged": order.charged}

    # Django's cache API, backed by firm-cache rows in the same database as the order.
    return JsonResponse(cache.get_or_set(f"order:{order_id}", load))
