"""The canonical messaging-platform enum.

SPEC §5 fixes the set for v1: ``telegram, instagram, messenger, whatsapp, sms,
email``. It lives in ``apps.common`` because two apps need it before the
channels app exists — ``apps.credentials`` keys platform app credentials on it
now, and issue #4 builds the adapter/policy registry (ROADMAP contract 4) around
the same values. One enum, imported; not two that drift.

Additive only. New platforms append; existing values are stored in the database
and must never be renamed.
"""

from django.db import models


class Platform(models.TextChoices):
    TELEGRAM = "telegram", "Telegram"
    INSTAGRAM = "instagram", "Instagram"
    MESSENGER = "messenger", "Facebook Messenger"
    WHATSAPP = "whatsapp", "WhatsApp"
    SMS = "sms", "SMS"
    EMAIL = "email", "Email"
