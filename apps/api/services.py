"""Issuing keys and configuring webhooks — the write path behind the settings UI.

The views are thin; the rules are here, so a management command, a shell or a
later internal caller gets the same enforcement a form post does. That is the
same reasoning ``apps.contacts.services`` applies to contact writes, and the
same one BrightBean Studio's ``api_keys.services`` applies to issuance.

**Two gates on issuing a key, and they are at different tiers.**

The *page* is org-tier: SPEC §4.1 puts ``manage_api_keys`` at the organization,
because a key spans "any workspace in the org", and this repo expresses org
authority as ``@require_org_role`` rather than an org permission table (there is
none — see ``apps/members/roles.py``). So an org admin or owner reaches the
page.

The *target workspace* is workspace-tier: issuing a key for a workspace also
requires ``manage_api_keys`` there, which is the merged code's existing
admin-only key. An org admin who is not an admin of a particular workspace does
not get to mint a credential inside it by going around the workspace's own
membership.

**Scopes are capped once, at issuance.** A scope may not grant a permission the
issuer does not hold. After that the key is independent of them — see
``ApiKey``'s docstring for why that is deliberate rather than an oversight.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils import timezone

from apps.api import keys as key_tokens
from apps.api.auth import SCOPE_PERMISSIONS, permissions_for_scopes
from apps.api.events import SUBSCRIBABLE_EVENTS
from apps.api.models import ApiKey, ApiScope, OutboundWebhook
from apps.members.models import OrgMembership, WorkspaceMembership
from apps.members.roles import ORG_ROLE_LEVEL, OrgRole

__all__ = [
    "ApiKeysError",
    "create_webhook",
    "issue_api_key",
    "revoke_api_key",
    "update_webhook",
]

MAX_KEYS_PER_WORKSPACE = 50
MAX_WEBHOOKS_PER_WORKSPACE = 20


class ApiKeysError(ValueError):
    """A refused management operation.

    A ``ValueError`` for the same reason ``apps.contacts.errors.ContactsError``
    is one: the caller passed something that is not allowed, and the views turn
    it into a form error rather than a 500.
    """


def _issuer_may_manage(user: Any, workspace: Any) -> None:
    """Both gates. Raises :class:`ApiKeysError` naming the one that refused."""
    org_membership = OrgMembership.objects.filter(user=user, organization_id=workspace.organization_id).first()
    org_level = ORG_ROLE_LEVEL.get(getattr(org_membership, "org_role", ""), 0)
    if org_level < ORG_ROLE_LEVEL[OrgRole.ADMIN]:
        raise ApiKeysError("Only an organization owner or admin can issue API keys.")

    membership = WorkspaceMembership.objects.filter(user=user, workspace=workspace).first()
    if membership is None or not membership.effective_permissions.get("manage_api_keys", False):
        raise ApiKeysError("You need the manage_api_keys permission in that workspace to issue a key for it.")


def _validated_scopes(scopes: Any, issuer_permissions: dict[str, bool]) -> list[str]:
    requested = sorted({str(scope) for scope in (scopes or ())})
    if not requested:
        raise ApiKeysError("A key needs at least one scope.")
    unknown = [scope for scope in requested if scope not in SCOPE_PERMISSIONS]
    if unknown:
        raise ApiKeysError(f"Unknown scope: {', '.join(unknown)}.")

    held = {key for key, granted in issuer_permissions.items() if granted}
    for scope in requested:
        missing = sorted(SCOPE_PERMISSIONS[scope] - held)
        if missing:
            raise ApiKeysError(
                f"You cannot grant the {scope} scope: it includes {', '.join(missing)}, which you do not hold."
            )
    return requested


@transaction.atomic
def issue_api_key(*, workspace: Any, issuer: Any, name: str, scopes: Any) -> ApiKey:
    """Mint a key. The returned object carries ``raw_token`` — show it once.

    Nothing persists the plaintext, and nothing can recover it: an operator who
    loses it issues a new key and revokes this one.
    """
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ApiKeysError("Give the key a name so you can recognise it later.")
    if len(cleaned_name) > 100:
        raise ApiKeysError("The name is too long (100 characters maximum).")

    _issuer_may_manage(issuer, workspace)
    membership = WorkspaceMembership.objects.get(user=issuer, workspace=workspace)
    validated = _validated_scopes(scopes, membership.effective_permissions)

    live = ApiKey.objects.for_workspace(workspace).filter(revoked_at__isnull=True).count()
    if live >= MAX_KEYS_PER_WORKSPACE:
        raise ApiKeysError(
            f"This workspace already has {MAX_KEYS_PER_WORKSPACE} active keys. Revoke one before issuing another."
        )

    minted = key_tokens.mint()
    api_key = ApiKey.objects.create(
        workspace=workspace,
        name=cleaned_name,
        scopes=validated,
        lookup_prefix=minted.lookup_prefix,
        token_digest=minted.token_digest,
        created_by=issuer,
    )
    api_key.raw_token = minted.plaintext
    return api_key


def revoke_api_key(api_key: ApiKey) -> bool:
    """Switch a key off. Immediate — the auth path reads the column every request.

    Idempotent: revoking an already-revoked key returns False and leaves the
    original timestamp, so the audit trail says when it actually happened.
    """
    if api_key.revoked_at is not None:
        return False
    now = timezone.now()
    api_key.revoked_at = now
    ApiKey.objects.for_workspace(api_key.workspace_id).filter(pk=api_key.pk).update(revoked_at=now, updated_at=now)
    return True


def scope_summary(api_key: ApiKey) -> str:
    """What a key's scopes actually grant, for the settings page."""
    granted = sorted(key for key, allowed in permissions_for_scopes(api_key.scopes).items() if allowed)
    return ", ".join(granted) or "nothing"


def _validated_events(events: Any) -> list[str]:
    chosen = [str(event) for event in (events or ())]
    unknown = sorted(set(chosen) - set(SUBSCRIBABLE_EVENTS))
    if unknown:
        raise ApiKeysError(f"Unknown event: {', '.join(unknown)}.")
    if not chosen:
        raise ApiKeysError("Choose at least one event to send.")
    # Preserve the catalog's order rather than the form's, so two endpoints with
    # the same subscription store the same list.
    return [event for event in SUBSCRIBABLE_EVENTS if event in set(chosen)]


def _validated_url(url: str) -> str:
    """Syntactic checks only. The address itself is the SSRF guard's business.

    Resolving a hostname here would be a check with a shelf life — DNS can point
    somewhere else by the time a delivery goes out, which is exactly the rebind
    ``guarded_request`` pins against. So this rejects only what is wrong on its
    face, and the guard re-decides on every delivery.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise ApiKeysError("Enter the URL deliveries should be sent to.")
    if len(cleaned) > 500:
        raise ApiKeysError("That URL is too long (500 characters maximum).")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ApiKeysError("The URL must start with http:// or https://.")
    if parsed.username or parsed.password:
        # The guard refuses these too; saying so here is a better error than a
        # delivery that silently never lands.
        raise ApiKeysError("The URL must not carry a username or password.")
    if not parsed.hostname:
        raise ApiKeysError("That does not look like a valid URL.")
    try:
        URLValidator(schemes=["http", "https"])(cleaned)
    except ValidationError as exc:
        raise ApiKeysError("That does not look like a valid URL.") from exc
    return cleaned


@transaction.atomic
def create_webhook(*, workspace: Any, url: str, events: Any) -> OutboundWebhook:
    """Create an endpoint and return it with ``secret`` freshly minted.

    The plaintext secret is readable on the returned instance and is shown once,
    like a channel connection's webhook secret. It stays recoverable in the
    database — unlike an API key — because the operator has to configure the
    same value in their own verifier, and "rotate and reconfigure" is not an
    acceptable answer to "I lost it" for something a third party depends on.
    """
    existing = OutboundWebhook.objects.for_workspace(workspace).count()
    if existing >= MAX_WEBHOOKS_PER_WORKSPACE:
        raise ApiKeysError(f"This workspace already has {MAX_WEBHOOKS_PER_WORKSPACE} endpoints.")

    webhook = OutboundWebhook(
        workspace=workspace,
        url=_validated_url(url),
        events=_validated_events(events),
        enabled=True,
    )
    webhook.rotate_secret()
    webhook.save()
    return webhook


@transaction.atomic
def update_webhook(webhook: OutboundWebhook, *, url: str, events: Any, enabled: bool) -> OutboundWebhook:
    """Apply an edit, clearing the failure streak when an endpoint is re-enabled.

    Re-enabling is the operator saying "I fixed it". Leaving the counter at its
    auto-disable threshold would switch the endpoint off again on the very next
    failed delivery, which reads as "re-enabling does not work".
    """
    webhook.url = _validated_url(url)
    webhook.events = _validated_events(events)
    was_enabled = webhook.enabled
    webhook.enabled = bool(enabled)
    fields = ["url", "events", "enabled", "updated_at"]
    if webhook.enabled and not was_enabled:
        webhook.consecutive_failures = 0
        webhook.disabled_at = None
        fields += ["consecutive_failures", "disabled_at"]
    webhook.save(update_fields=fields)
    return webhook


def rotate_webhook_secret(webhook: OutboundWebhook) -> str:
    """Mint a new secret and persist it; returns the plaintext once."""
    secret = webhook.rotate_secret()
    webhook.save(update_fields=["secret", "updated_at"])
    return secret


def known_scopes() -> list[tuple[str, str]]:
    """``(value, label)`` pairs for the issuance form.

    Filtered through ``SCOPE_PERMISSIONS`` rather than listing ``ApiScope``
    outright, so the picker can only ever offer a scope that
    :func:`_validated_scopes` will accept. A form that offers a choice the
    server then refuses teaches people the product is broken.
    """
    return [(scope.value, scope.label) for scope in ApiScope if scope.value in SCOPE_PERMISSIONS]
