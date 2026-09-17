"""Encrypted platform app credentials, at organization level.

Ported from BrightBean Studio's ``apps/credentials/models.py``. The resolution
order is **deployment env → organization**; the chain itself lives in
:mod:`apps.credentials.resolution`. Environment variables are the way these are
set; this table is the fallback for one deployment serving several
organizations, and is editable only by a superuser in the Django admin.

**These rows are never looked up by their contents.** ``credentials`` is an
``EncryptedJSONField``, and every write encrypts under a fresh random nonce, so
``.filter(credentials=...)`` compares two unrelated ciphertexts and silently
matches nothing — no exception, just an empty result that reads like "no such
row" (see the module docstring on ``apps.common.encryption``). The table is
keyed on plaintext columns instead: ``(organization, platform)``, unique.
Nothing here needs the deterministic HMAC sidecar that docstring prescribes; the
first thing that will is issue #4's webhook-secret lookup, which resolves an
inbound request to a connection by a secret it was given.

``is_configured`` is the other consequence: it is the only queryable projection
of an encrypted payload, so it is recomputed on every save and never settable.
"""

from typing import Any

from django.db import models

from apps.common.encryption import EncryptedJSONField
from apps.common.managers import OrgScopedManager
from apps.common.models import BaseModel
from apps.common.platforms import Platform

# Per-platform required credential keys. Each inner tuple is an "any of these
# aliases" group; a platform counts as configured only when EVERY group has a
# non-empty value. Meta's own docs use app_id / app_secret while its OAuth
# endpoints speak client_id / client_secret, so both spellings are accepted.
#
# Only the Meta platforms have deployment-level app credentials at all. A
# Telegram bot token comes from BotFather and lives on the connection; Twilio
# ships an account SID per connection; SMTP credentials are per sending domain.
# Those platforms are absent on purpose and can therefore never be
# ``is_configured`` — the same shape Studio uses for its session-auth platforms.
REQUIRED_CREDENTIAL_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    Platform.INSTAGRAM: (("client_id", "app_id"), ("client_secret", "app_secret")),
    Platform.MESSENGER: (("client_id", "app_id"), ("client_secret", "app_secret")),
    Platform.WHATSAPP: (("client_id", "app_id"), ("client_secret", "app_secret")),
}

# Platforms a credential row may be created for.
CONFIGURABLE_PLATFORMS = tuple(REQUIRED_CREDENTIAL_KEYS)


def derive_is_configured(platform: str, credentials: Any) -> bool:
    """True when ``credentials`` satisfies every required key group.

    ``str(creds.get(k) or "")`` rather than ``str(creds.get(k))`` is load
    bearing: ``str(None)`` is ``"None"``, which is truthy after ``.strip()`` and
    would mark a row with a null secret as fully configured.
    """
    groups = REQUIRED_CREDENTIAL_KEYS.get(platform)
    if not groups:
        return False
    creds = credentials if isinstance(credentials, dict) else {}
    return all(any(str(creds.get(key) or "").strip() for key in group) for group in groups)


def missing_key_groups(platform: str, credentials: Any) -> list[tuple[str, ...]]:
    """Which alias groups are still empty. Names only — never values."""
    groups = REQUIRED_CREDENTIAL_KEYS.get(platform)
    if not groups:
        return []
    creds = credentials if isinstance(credentials, dict) else {}
    return [group for group in groups if not any(str(creds.get(key) or "").strip() for key in group)]


def mask_credentials(credentials: Any) -> dict[str, str]:
    """Show the last four characters of each value and nothing else.

    The helper CONTRIBUTING.md points at for rendering a credential anywhere.
    Nothing in the product displays one today — the settings page that did was
    removed with the workspace override — so this has no caller but its tests.
    It is kept rather than deleted because the next surface that needs to show a
    credential should reach for a tested masker instead of writing another.
    """
    masked: dict[str, str] = {}
    for key, value in (credentials or {}).items():
        if isinstance(value, str) and len(value) > 4:
            masked[key] = "****" + value[-4:]
        else:
            masked[key] = "****"
    return masked


class PlatformCredential(BaseModel):
    """Organization-level credentials, entered in the Django admin.

    The lower level of the resolution chain: a self-hoster who runs one
    deployment for several orgs sets each org's own Meta app here, for the
    platforms the environment does not already answer for.
    """

    platform = models.CharField(max_length=30, choices=Platform.choices)
    credentials = EncryptedJSONField(
        default=dict,
        help_text="Encrypted JSON of platform-specific credential fields.",
    )
    is_configured = models.BooleanField(default=False)

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="platform_credentials",
    )

    objects = OrgScopedManager()

    class Meta:
        db_table = "credentials_platform_credential"
        ordering = ["platform"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "platform"], name="platformcredential_unique_org_platform"),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Keep ``is_configured`` a pure function of the credential values.

        A caller never sets it. The ``update_fields`` branch is the half that is
        easy to miss: ``save(update_fields=["credentials"])`` would otherwise
        write new secrets and leave the stale flag behind, so a row that just
        became complete would stay switched off (or vice versa).
        """
        self.is_configured = derive_is_configured(self.platform, self.credentials)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "is_configured"}
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.organization.name} - {self.get_platform_display()}"
