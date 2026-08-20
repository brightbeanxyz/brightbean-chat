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
from django.core.checks import CheckMessage, Error, Tags, Warning, register

from apps.common.placeholders import is_placeholder_secret


@register(Tags.security)
def check_production_secrets(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Refuse to run outside DEBUG on development placeholders or no hosts."""
    if settings.DEBUG:
        return []

    errors: list[CheckMessage] = []

    if is_placeholder_secret(settings.SECRET_KEY):
        errors.append(
            Error(
                "SECRET_KEY is a placeholder, but DEBUG is False.",
                hint=(
                    "Placeholder values ship in this repository (.env.example, and the "
                    "DEBUG defaults), so every session cookie, signed token and encrypted "
                    "credential would be forgeable by anyone who can read it. Set a real "
                    "SECRET_KEY. If you expected the development default, note that a "
                    "settings module must set DEBUG before importing "
                    "config.settings.base, not after."
                ),
                id="common.E001",
            )
        )

    if is_placeholder_secret(settings.ENCRYPTION_KEY_SALT):
        errors.append(
            Error(
                "ENCRYPTION_KEY_SALT is a placeholder, but DEBUG is False.",
                hint=(
                    "Field encryption would derive its key from a salt published in this "
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


@register(Tags.security)
def check_s3_custom_domain_signing(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Warn when a custom S3 domain silently disables URL signing.

    django-storages' ``url()`` branches on ``custom_domain`` *before* it
    reaches the S3 presigner, and on that branch it signs only when a
    CloudFront signer is configured::

        if self.custom_domain:
            url = "...custom domain..."
            if self.querystring_auth and self.cloudfront_signer:
                return self.cloudfront_signer.generate_presigned_url(...)
            return url

    So ``AWS_QUERYSTRING_AUTH = True`` plus a private ACL plus a custom domain
    yields an *unsigned* URL to a private object: every ``default_storage.url()``
    is a 403. It fails closed rather than leaking, but it fails silently and at
    delivery time, and SECURITY-BASELINE §9 requires media delivery URLs to be
    signed — so the media library (#16) would be building links that cannot
    work. Either drop S3_CUSTOM_DOMAIN and let the S3 presigner run, or supply
    AWS_CLOUDFRONT_KEY_ID / AWS_CLOUDFRONT_KEY.
    """
    if getattr(settings, "STORAGE_BACKEND", "local").lower() != "s3":
        return []
    if not getattr(settings, "AWS_S3_CUSTOM_DOMAIN", ""):
        return []
    if not getattr(settings, "AWS_QUERYSTRING_AUTH", False):
        # Public-read delivery is a deliberate choice; unsigned URLs are the point.
        return []
    if getattr(settings, "AWS_CLOUDFRONT_KEY_ID", "") and getattr(settings, "AWS_CLOUDFRONT_KEY", ""):
        return []

    return [
        Warning(
            "S3_CUSTOM_DOMAIN is set with AWS_QUERYSTRING_AUTH, but no CloudFront signer is configured.",
            hint=(
                "django-storages returns UNSIGNED urls on the custom-domain path unless "
                "AWS_CLOUDFRONT_KEY_ID and AWS_CLOUDFRONT_KEY are both set, so private "
                "objects will 403. Either unset S3_CUSTOM_DOMAIN so the S3 presigner runs, "
                "or configure the CloudFront signer."
            ),
            id="common.W001",
        )
    ]
