from __future__ import annotations

from demo import views
from django.urls import path

urlpatterns = [
    path("orders/", views.create_order),
    path("orders/<int:order_id>/", views.show_order),
]
