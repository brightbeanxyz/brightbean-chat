"""Deploy-safety system checks (SECURITY-BASELINE §8).

``config/settings/base.py`` refuses to boot without ``SECRET_KEY``,
``ENCRYPTION_KEY_SALT`` and ``ALLOWED_HOSTS``. That check runs while settings
are being *imported*, off the environment's ``DEBUG`` value, which makes it
sensitive to import order: a settings module that sets ``DEBUG = False`` after
``from .base import *`` gets the development branch — the hardcoded,
repo-public key and salt — and then looks like production.

That is exactly the bug this file exists to catch. These checks read the
**fully-loaded** settings, so they see what the process will actually run with
no matter how the module was assembled. ``manage.py check`` runs them, and
Django runs them before ``runserver`` and every management command.
"""

from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, register


@register(Tags.security)
def check_production_secrets(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Refuse to run outside DEBUG on development placeholders or no hosts."""
    if settings.DEBUG:
        return []

    errors: list[CheckMessage] = []

    dev_secret_key = getattr(settings, "DEV_INSECURE_SECRET_KEY", None)
    if dev_secret_key and dev_secret_key == settings.SECRET_KEY:
        errors.append(
            Error(
                "SECRET_KEY is the development placeholder, but DEBUG is False.",
                hint=(
                    "This value is committed to the repository, so every session cookie, "
                    "signed token and encrypted credential would be forgeable by anyone "
                    "who can read it. Set a real SECRET_KEY. If you expected the "
                    "development default, note that a settings module must set DEBUG "
                    "before importing config.settings.base, not after."
                ),
                id="common.E001",
            )
        )

    dev_salt = getattr(settings, "DEV_INSECURE_ENCRYPTION_KEY_SALT", "")
    dev_salt_bytes = dev_salt.encode("utf-8") if dev_salt else b""
    if dev_salt_bytes and dev_salt_bytes == settings.ENCRYPTION_KEY_SALT:
        errors.append(
            Error(
                "ENCRYPTION_KEY_SALT is the development placeholder, but DEBUG is False.",
                hint=(
                    "Field encryption would derive its key from a salt committed to the "
                    "repository. Set a real ENCRYPTION_KEY_SALT."
                ),
                id="common.E002",
            )
        )

    if not [host for host in settings.ALLOWED_HOSTS if host.strip()]:
        errors.append(
            Error(
                "ALLOWED_HOSTS is empty, but DEBUG is False.",
                hint="Django will reject every request with a 400, /healthz included.",
                id="common.E003",
            )
        )

    return errors
