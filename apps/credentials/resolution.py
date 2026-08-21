"""The credential resolution chain (SPEC §4).

**workspace override → organization → deployment environment.**

This is the inverse of BrightBean Studio's ``resolve_platform_credentials``,
which is env-dominant with an org fallback. Studio's direction makes the most
specific configuration the least authoritative: a self-hoster who sets an env
var for convenience silently overrides every org that entered its own app in the
admin. Here the most specific level that is actually usable wins, which is what
"override" has to mean.

**A level is used only if it is complete.** Incomplete levels fall through
rather than shadowing the ones below (this is a decision, and the alternatives
were real):

* Merging keys across levels — workspace ``client_id`` with org
  ``client_secret`` — produces credential sets that no provider will ever
  accept, assembled from two places, and the resulting 401 names neither.
* Letting an incomplete override win outright means one blank field in a
  workspace form silently disables a working org configuration.

Falling through is the only option where a half-finished override is harmless.
The debug log names the missing key *group*, never a value.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

from apps.credentials.models import (
    PlatformCredential,
    WorkspaceCredentialOverride,
    derive_is_configured,
    missing_key_groups,
)

logger = logging.getLogger(__name__)

SOURCE_WORKSPACE = "workspace"
SOURCE_ORGANIZATION = "organization"
SOURCE_ENV = "env"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class CredentialResolution:
    """The credentials in force for one platform, and where they came from.

    The source is carried so the settings UI can say "using the organization's
    credentials" instead of showing an empty override form next to a working
    integration and leaving the operator to guess.
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
    """Resolve credentials for ``platform``, most specific usable level first.

    ``organization`` is inferred from ``workspace`` when only the latter is
    given, so callers holding a request's workspace do not have to remember to
    pass both.
    """
    if organization is None and workspace is not None:
        organization = workspace.organization

    for source, candidate in (
        (SOURCE_WORKSPACE, _workspace_credentials(platform, workspace)),
        (SOURCE_ORGANIZATION, _organization_credentials(platform, organization)),
        (SOURCE_ENV, env_credentials(platform)),
    ):
        if candidate is None:
            continue
        if derive_is_configured(platform, candidate):
            return CredentialResolution(platform=platform, source=source, credentials=dict(candidate))
        if candidate:
            missing = ["/".join(group) for group in missing_key_groups(platform, candidate)]
            logger.debug(
                "Skipping %s credentials for %s: missing %s",
                source,
                platform,
                ", ".join(missing) or "required keys",
            )

    return CredentialResolution(platform=platform)


def _workspace_credentials(platform: str, workspace: Any) -> dict[str, Any] | None:
    if workspace is None:
        return None
    override = WorkspaceCredentialOverride.objects.for_workspace(workspace).filter(platform=platform).first()
    return dict(override.credentials or {}) if override else None  # type: ignore[arg-type]


def _organization_credentials(platform: str, organization: Any) -> dict[str, Any] | None:
    if organization is None:
        return None
    credential = PlatformCredential.objects.for_org(organization.pk if hasattr(organization, "pk") else organization)
    row = credential.filter(platform=platform).first()
    return dict(row.credentials or {}) if row else None
