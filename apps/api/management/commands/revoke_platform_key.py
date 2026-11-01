from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.api.models import PlatformApiKey
from apps.api.services import revoke_platform_key


class Command(BaseCommand):
    help = "Revoke a platform API key by id. Takes effect on the next request."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("key_id", help="The id printed by create_platform_key.")

    def handle(self, *args: Any, key_id: str, **options: Any) -> None:
        try:
            key = PlatformApiKey.objects.filter(pk=key_id).first()
        except (ValueError, ValidationError):  # not a UUID
            key = None
        if key is None:
            raise CommandError("No such platform key.")
        revoked = revoke_platform_key(key)
        self.stdout.write("Revoked." if revoked else "Already revoked.")
