"""The credential resolution chain (SPEC §4).

**deployment environment → organization.**

Platform app credentials are *developer* credentials: they identify the Meta app
this deployment registered, not anything a tenant owns. So the deployment's own
environment is the authoritative level, and ``PLATFORM_<PLATFORM>_<KEY>`` is the
way to set them. The organization row exists for the one case the environment
cannot express — a single deployment serving several organizations, each with
its own Meta app — and is editable only by a superuser in the Django admin.

This is the direction BrightBean Studio uses, and the inverse of what this
project shipped first. The original chain put a workspace-level override on top,
which meant a workspace admin could replace the deployment's app credentials
from a settings form; the override table and its page are gone.

**A level is used only if it is complete.** Incomplete levels fall through
rather than shadowing the one below (this is a decision, and the alternatives
were real):

* Merging keys across levels — an env ``client_id`` with an org
  ``client_secret`` — produces credential sets that no provider will ever
  accept, assembled from two places, and the resulting 401 names neither.
* Letting an incomplete level win outright means one blank field silently
  disables a working configuration below it.

Falling through is the only option where a half-finished level is harmless.
The debug log names the missing key *group*, never a value.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from apps.credentials.models import (
    PlatformCredential,
    derive_is_configured,
    missing_key_groups,
)

logger = logging.getLogger(__name__)

SOURCE_ORGANIZATION = "organization"
SOURCE_ENV = "env"
SOURCE_NONE = "none"

#: Keys that share the ``PLATFORM_<PLATFORM>_*`` namespace without being app
#: credentials. ``verify_token`` is read straight from the environment by the
#: webhook verification GET (which is unauthenticated and names no
#: organization), so it never takes part in the chain — and a level holding
#: nothing else is not a half-finished credential set.
NON_CREDENTIAL_KEYS = frozenset({"verify_token"})


@dataclass(frozen=True)
class CredentialResolution:
    """The credentials in force for one platform, and where they came from.

    The source is carried for diagnostics: a 401 from Meta is much easier to act
    on when the log line says which level answered, and "env" versus
    "organization" is the difference between editing a deploy config and editing
    a database row.
    """

    platform: str
    source: str = SOURCE_NONE
    credentials: dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.source != SOURCE_NONE


def env_credentials(platform: str) -> dict[str, Any]:
    """Deployment-level credentials from ``PLATFORM_<PLATFORM>_<KEY>`` env vars."""
    return dict(getattr(settings, "PLATFORM_CREDENTIALS_FROM_ENV", {}).get(platform, {}))


def resolve_platform_credentials(
    platform: str,
    *,
    workspace: Any = None,
    organization: Any = None,
) -> CredentialResolution:
    """Resolve credentials for ``platform``: the environment first, then the org.

    ``organization`` is inferred from ``workspace`` when only the latter is
    given, so callers holding a request's workspace do not have to pass both.
    ``organization_id`` rather than ``.organization``: this runs on every
    inbound delivery, and the row below is fetched by id anyway.

    A complete environment level short-circuits before any query is issued,
    which is the common self-hosted shape.
    """
    from_env = env_credentials(platform)
    if derive_is_configured(platform, from_env):
        return CredentialResolution(platform=platform, source=SOURCE_ENV, credentials=from_env)
    _log_skip(SOURCE_ENV, platform, from_env)

    if organization is None and workspace is not None:
        organization = workspace.organization_id

    row = _organization_credential(platform, organization)
    if row is not None:
        # ``EncryptedJSONField`` subclasses ``TextField``, so django-stubs types
        # the attribute as ``str`` even though the column holds JSON — the same
        # quirk the models and tests annotate around.
        candidate: dict[str, Any] = dict(row.credentials or {})  # type: ignore[arg-type]
        if derive_is_configured(platform, candidate):
            return CredentialResolution(platform=platform, source=SOURCE_ORGANIZATION, credentials=candidate)
        _log_skip(SOURCE_ORGANIZATION, platform, candidate)

    return CredentialResolution(platform=platform)


def _log_skip(source: str, platform: str, candidate: dict[str, Any]) -> None:
    """Names the missing key *group*, never a value.

    Reached only for a level that exists but is not complete. Two shapes are
    legitimately partial and are not reported:

    * A level holding nothing but :data:`NON_CREDENTIAL_KEYS`. An environment
      carrying only a verify token is the *correct* configuration for a
      deployment whose app id and secret live on an organization row, and this
      runs on every inbound delivery — reporting it would be a log line per
      webhook for a working setup.
    * A platform with no required keys at all (Telegram, SMS, email). Nothing it
      carries can be a credential set, so ``missing`` comes back empty.

    A level with keys the chain does not recognise — a typo'd
    ``PLATFORM_INSTAGRAM_CLIENTID`` — *is* reported, because believing you
    configured something that silently does nothing is the failure this log
    exists for.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    if not set(candidate) - NON_CREDENTIAL_KEYS:
        return
    missing = missing_key_groups(platform, candidate)
    if not missing:
        return
    logger.debug(
        "Skipping %s credentials for %s: missing %s",
        source,
        platform,
        ", ".join("/".join(group) for group in missing),
    )


def _organization_credential(platform: str, organization: Any) -> PlatformCredential | None:
    if organization is None:
        return None
    org_id = organization.pk if hasattr(organization, "pk") else organization
    return PlatformCredential.objects.for_org(org_id).filter(platform=platform).first()
