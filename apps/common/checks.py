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


@register(Tags.models)
def check_workspace_scoped_models(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """Every tenant model must keep the plain manager as its default.

    ``WorkspaceScopedModel`` relies on declaration order: ``all_objects`` is
    created before ``objects``, so Django picks it as ``_default_manager``, and
    the admin, serialization and reverse related access keep working while
    ``Model.objects`` stays enforcing.

    That is a one-line invariant with no syntax to protect it. Swap the two
    declarations and everything still imports; what breaks is
    ``workspace.contacts.all()`` at runtime, in whichever later layer happens to
    touch it first. This check turns that into a failed build.
    """
    from django.apps import apps as django_apps

    from apps.common.scoping import WorkspaceScopedManager, WorkspaceScopedModel

    errors: list[CheckMessage] = []
    for model in django_apps.get_models():
        if not issubclass(model, WorkspaceScopedModel):
            continue
        if isinstance(model._meta.default_manager, WorkspaceScopedManager):
            errors.append(
                Error(
                    f"{model._meta.label}'s default manager is the enforcing workspace-scoped one.",
                    hint=(
                        "Django's admin, serialization and reverse related managers all go "
                        "through _default_manager, and the enforcing manager raises "
                        "UnscopedQueryError there. Declare `all_objects = models.Manager()` "
                        "before `objects = WorkspaceScopedManager()` (apps/common/scoping.py), "
                        "or set Meta.default_manager_name = 'all_objects'."
                    ),
                    id="common.E004",
                )
            )
    return errors


@register(Tags.compatibility)
def check_platform_env_slugs(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    """``PLATFORM_ENV_SLUGS`` must match the ``Platform`` enum.

    A settings module cannot import model code, so the slugs the
    ``PLATFORM_*`` environment scan uses are written out a second time in
    ``config/settings/base.py``. The failure mode of a mismatch is silent: a
    platform added to the enum but not the tuple simply never picks up its
    deployment credentials, and the operator sees "not configured" while looking
    straight at the env var they set.
    """
    from apps.common.platforms import Platform

    declared = set(getattr(settings, "PLATFORM_ENV_SLUGS", ()))
    known = {choice.value for choice in Platform}
    if declared == known:
        return []

    return [
        Error(
            "PLATFORM_ENV_SLUGS does not match apps.common.platforms.Platform.",
            hint=(
                f"Only in settings: {sorted(declared - known) or 'none'}. "
                f"Only in the enum: {sorted(known - declared) or 'none'}. "
                "Deployment credentials for a platform missing from the settings tuple are "
                "silently ignored."
            ),
            id="common.E005",
        )
    ]
