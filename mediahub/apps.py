from __future__ import annotations

import os

from django.apps import AppConfig
from django.contrib.auth import get_user_model
from django.db.models.signals import post_migrate


class MediahubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "mediahub"

    def ready(self) -> None:
        post_migrate.connect(ensure_demo_user, sender=self)


def ensure_demo_user(**_: object) -> None:
    username = os.getenv("DEMO_USERNAME")
    password = os.getenv("DEMO_PASSWORD")
    if not username or not password:
        return

    User = get_user_model()
    user, created = User.objects.get_or_create(username=username, defaults={"is_staff": False})
    if created or not user.check_password(password):
        user.set_password(password)
        user.is_active = True
        user.save(update_fields=["password", "is_active"])
