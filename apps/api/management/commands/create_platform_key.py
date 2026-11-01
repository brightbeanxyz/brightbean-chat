from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.api.services import ApiKeysError, issue_platform_key


class Command(BaseCommand):
    help = (
        "Issue a platform API key (bbp_…). The token is printed once on stdout and cannot be recovered; "
        "do not run this where stdout is collected (CI logs, log shippers)."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--name", required=True, help='A label, e.g. "zirek-prod".')

    def handle(self, *args: Any, name: str, **options: Any) -> None:
        try:
            key = issue_platform_key(name=name)
        except ApiKeysError as exc:
            raise CommandError(str(exc)) from exc
        self.stderr.write(f"Created platform key {key.pk} ({key.name}). Store the token now; it is not shown again.")
        self.stdout.write(key.raw_token or "")
