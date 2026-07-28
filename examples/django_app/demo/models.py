from __future__ import annotations

from django.db import models


class Order(models.Model):
    email = models.EmailField()
    amount_cents = models.IntegerField()
    charged = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Order {self.pk} ({self.email})"
