"""The IDOR fuzz suite (SECURITY-BASELINE §1).

**Every PR that adds an endpoint extends this file.** That is not a convention
kept by good intentions: :func:`iter_tenant_routes` walks the *registered URL
patterns*, and a route carrying a tenant-shaped id it does not know how to build
raises rather than being skipped. Adding ``/w/<uuid:workspace_id>/contacts/<uuid:contact_id>/``
without registering a ``contact_id`` resolver turns the suite red, which is the
whole mechanism.

What it proves: hitting a route with **another tenant's** object ids, as a fully
privileged member of a different organization, answers **404** — never 403, never
200. A 403 would confirm the id names something real, which over a UUID space is
the only information an attacker was missing.

The contract per route, per method:

* every method answers 404 or 405, and
* at least one method answers 404.

The 405 allowance is for POST-only views: the stacking convention puts
``@require_POST`` innermost, so a GET is rejected on the method before the view
body runs — and a 405 there is returned for real and fake ids alike, so it tells
an attacker nothing. Requiring at least one 404 is what stops a route from
passing merely because nothing ever reached it.

Routes with no tenant-identifying kwarg (``/organization/members/``, say) are
not URL-addressable across tenants at all: they operate on ``request.org``,
which the middleware resolves from the signed-in user. They are skipped here and
covered by the per-app permission tests.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from django.urls import URLPattern, URLResolver, get_resolver, reverse

from tests.support import Tenancy

# ---------------------------------------------------------------------------
# Registry — the part later PRs extend
# ---------------------------------------------------------------------------

#: URL kwargs whose value names an object owned by one tenant. A route carrying
#: any of these is fuzzed. Each resolver returns the **victim's** value.
TENANT_KWARG_RESOLVERS: dict[str, Callable[[Tenancy], Any]] = {
    "workspace_id": lambda t: t.workspace.pk,
    # apps.organizations.views.set_workspace_archived deliberately avoids the
    # name ``workspace_id`` so RBACMiddleware does not 404 archived workspaces
    # before the view can restore them; it scopes to request.org instead.
    "target_id": lambda t: t.workspace.pk,
    "membership_id": lambda t: t.org_membership.pk,
    "invitation_id": lambda t: _victim_invitation(t).pk,
    "tag_id": lambda t: _victim_tag(t).pk,
    "field_id": lambda t: _victim_custom_field(t).pk,
    "connection_id": lambda t: _victim_connection(t).pk,
    "asset_id": lambda t: _victim_media_asset(t).pk,
    "folder_id": lambda t: _victim_media_folder(t).pk,
    "flow_id": lambda t: _victim_flow(t).pk,
    # Notifications (issue #7) are keyed by user, not by workspace, so "the
    # victim" here is a person rather than a tenant. Registering it is an
    # opt-in: iter_tenant_routes() skips a route carrying no *registered*
    # kwarg before it ever reaches the unknown-kwarg check, so this route
    # would otherwise be neither swept nor reported. The per-user boundary is
    # also covered directly in apps/notifications/tests/test_views.py.
    "notification_id": lambda t: _victim_notification(t).pk,
}

#: Kwargs that need *a* value but do not identify a tenant. A route made only of
#: these is not fuzzed.
NEUTRAL_KWARG_VALUES: dict[str, Any] = {
    "platform": "instagram",
    # The email webhook's provider segment (resend / ses / smtp). It selects a
    # payload shape, not a tenant's object, and is not used for lookup.
    "provider": "resend",
}

#: Why the inbound webhook routes cannot answer 404 and are therefore not
#: sweepable. Shared by both, because the reasoning is identical.
_WEBHOOK_WAIVER = (
    "Unauthenticated public endpoint (SPEC §7.1). There is no session tenant to "
    "compare the connection against — the caller is a messaging platform, not a "
    "user — so 'belongs to another workspace' is not a question it can ask, and "
    "404 is not an answer it can give without breaking ingestion. What stands in "
    "for the sweep is that the route answers the SAME status to every connection "
    "id, real or not: 403 for both an unknown connection and a bad signature once "
    "the platform has an adapter, and 503 for every id while it has none — which "
    "is why apps/channels/views_webhooks.py resolves the adapter before it looks "
    "a connection up. Both halves are asserted by "
    "apps/channels/tests/test_webhooks.py::TestIdIndistinguishability; if that "
    "class is ever deleted, this waiver must be too."
)

#: Routes exempt from the sweep, each with the reason. A waiver is a reviewed
#: line in this dict; there is no silent skip.
WAIVED_ROUTES: dict[str, str] = {
    "webhook_sms": _WEBHOOK_WAIVER,
    "webhook_email": _WEBHOOK_WAIVER,
    "accept_invite": (
        "Public by design: the invitation token IS the credential, and the page "
        "renders the same 404 body for unknown, expired and accepted tokens. "
        "Covered by apps/members/tests/test_invitations.py."
    ),
}


def _victim_connection(tenancy: Tenancy) -> Any:
    """A channel connection owned by the victim, created on demand.

    ``external_id`` is namespaced by slug because SPEC §5's unique constraint on
    ``(platform, external_id)`` is deployment-wide: a fixed literal here would
    make the victim's and the attacker's tenancies collide.
    """
    from apps.channels.models import ChannelConnection
    from apps.common.platforms import Platform

    connection = ChannelConnection.objects.for_workspace(tenancy.workspace).first()
    if connection is None:
        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name=f"{tenancy.slug} bot",
            external_id=f"bot-{tenancy.slug}",
        )
    return connection


def _victim_flow(tenancy: Tenancy) -> Any:
    """A flow owned by the victim, created on demand.

    The sweep reaches these routes through the victim's ``workspace_id`` too, so
    the middleware answers first; ``apps/flows/tests/test_api.py`` covers the
    sharper case this cannot — the attacker's *own* workspace id paired with the
    victim's flow id, where only ``get_scoped_object_or_404`` stands in the way.
    """
    from apps.flows.models import Flow
    from apps.flows.services import create_flow

    flow = Flow.objects.for_workspace(tenancy.workspace).first()
    if flow is None:
        flow = create_flow(workspace=tenancy.workspace, name="Victim onboarding")
    return flow


def _victim_invitation(tenancy: Tenancy) -> Any:
    """A pending invitation owned by the victim, created on demand."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.members.models import Invitation

    invitation = Invitation.objects.filter(organization=tenancy.organization).first()
    if invitation is None:
        invitation = Invitation.objects.create(
            organization=tenancy.organization,
            email=f"pending@{tenancy.slug}.test",
            invited_by=tenancy.owner,
            expires_at=timezone.now() + timedelta(days=7),
        )
    return invitation


def _victim_tag(tenancy: Tenancy) -> Any:
    """A tag owned by the victim, created on demand."""
    from apps.contacts.models import Tag

    tag = Tag.objects.for_workspace(tenancy.workspace).first()
    return tag or Tag.objects.create(workspace=tenancy.workspace, name="vip")


def _victim_custom_field(tenancy: Tenancy) -> Any:
    """A custom field owned by the victim, created on demand."""
    from apps.contacts.models import CustomField, CustomFieldType

    field = CustomField.objects.for_workspace(tenancy.workspace).first()
    return field or CustomField.objects.create(workspace=tenancy.workspace, name="Plan", type=CustomFieldType.TEXT)


def _victim_media_asset(tenancy: Tenancy) -> Any:
    """A media asset owned by the victim, created on demand.

    Built through the model rather than the upload view: the sweep is about
    tenancy, not about validation, and going through ``create_asset`` would make
    every IDOR run write a file to storage and sniff it.
    """
    from apps.media_library.models import MediaAsset

    asset = MediaAsset.objects.for_workspace(tenancy.workspace).first()
    if asset is None:
        asset = MediaAsset.objects.create(
            workspace=tenancy.workspace,
            filename="victim.png",
            kind="image",
            mime="image/png",
            size=1,
            file="media/victim.png",
        )
    return asset


def _victim_media_folder(tenancy: Tenancy) -> Any:
    """A media folder owned by the victim, created on demand."""
    from apps.media_library.models import MediaFolder

    folder = MediaFolder.objects.for_workspace(tenancy.workspace).first()
    if folder is None:
        folder = MediaFolder.objects.create(workspace=tenancy.workspace, name="Victim folder")
    return folder


def _victim_notification(tenancy: Tenancy) -> Any:
    """A notification belonging to the victim's owner, created on demand."""
    from apps.notifications.models import Notification

    notification = Notification.objects.filter(user=tenancy.owner).first()
    if notification is None:
        notification = Notification.objects.create(
            user=tenancy.owner,
            event_type="flow_loop_cap_hit",
            title="Victim notification",
            payload={"workspace_id": str(tenancy.workspace.pk)},
        )
    return notification


# ---------------------------------------------------------------------------
# Route discovery
# ---------------------------------------------------------------------------


class UnregisteredRouteKwargError(AssertionError):
    """A tenant route carries a kwarg the suite does not know how to build."""


class UnnamedTenantRouteError(AssertionError):
    """A tenant route has no ``name=``, so the suite cannot reverse it."""


@dataclass(frozen=True)
class TenantRoute:
    name: str
    kwargs: tuple[str, ...]

    def url_for(self, tenancy: Tenancy) -> str:
        values = {}
        for kwarg in self.kwargs:
            resolver = TENANT_KWARG_RESOLVERS.get(kwarg)
            values[kwarg] = resolver(tenancy) if resolver else NEUTRAL_KWARG_VALUES[kwarg]
        return reverse(self.name, kwargs=values)


def _pattern_kwargs(pattern: Any) -> tuple[str, ...]:
    converters = getattr(pattern, "converters", None)
    if converters is not None:
        return tuple(converters)
    regex = getattr(pattern, "regex", None)
    return tuple(regex.groupindex) if regex is not None else ()


def _walk(resolver: URLResolver, prefix: tuple[str, ...], namespace: str | None) -> Iterator[TenantRoute]:
    for entry in resolver.url_patterns:
        kwargs = prefix + _pattern_kwargs(entry.pattern)
        if isinstance(entry, URLResolver):
            child_ns = ":".join(part for part in (namespace, entry.namespace) if part) or None
            yield from _walk(entry, kwargs, child_ns)
        elif isinstance(entry, URLPattern):
            if not entry.name:
                # Skipping it would be the one silent hole in a mechanism whose
                # whole point is that nothing escapes quietly: an endpoint
                # nothing reverses is exactly the kind that gets registered
                # without a name.
                if any(kwarg in TENANT_KWARG_RESOLVERS for kwarg in kwargs):
                    raise UnnamedTenantRouteError(
                        f"Route {entry.pattern!s} takes {sorted(kwargs)} but has no name=, so the IDOR "
                        f"suite cannot reverse it. Give it a name (and waive it in WAIVED_ROUTES if it "
                        f"genuinely must not be swept). See docs/SECURITY-BASELINE.md §1."
                    )
                continue
            yield TenantRoute(name=":".join(part for part in (namespace, entry.name) if part), kwargs=kwargs)


def iter_tenant_routes(urlconf: str | None = None) -> list[TenantRoute]:
    """Every registered route that names a tenant object in its URL.

    Raises :class:`UnregisteredRouteKwargError` when such a route carries a kwarg
    with no resolver — the mechanism that makes new endpoints extend this file
    instead of quietly escaping it.
    """
    routes: list[TenantRoute] = []
    for route in _walk(get_resolver(urlconf), (), None):
        if route.name in WAIVED_ROUTES:
            continue
        if not any(kwarg in TENANT_KWARG_RESOLVERS for kwarg in route.kwargs):
            continue
        unknown = [k for k in route.kwargs if k not in TENANT_KWARG_RESOLVERS and k not in NEUTRAL_KWARG_VALUES]
        if unknown:
            raise UnregisteredRouteKwargError(
                f"Route {route.name!r} takes {unknown}, which the IDOR suite cannot build. "
                f"Register a resolver in tests/idor.py (TENANT_KWARG_RESOLVERS for an id that "
                f"identifies a tenant's object, NEUTRAL_KWARG_VALUES otherwise), or waive the "
                f"route in WAIVED_ROUTES with a reason. See docs/SECURITY-BASELINE.md §1."
            )
        routes.append(route)
    return sorted(routes, key=lambda r: r.name)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

METHODS = ("get", "post")


def check_route_is_isolated(client: Any, route: TenantRoute, victim: Tenancy) -> list[str]:
    """Hit one route with the victim's ids. Returns a list of failure strings."""
    url = route.url_for(victim)
    failures: list[str] = []
    statuses: list[int] = []

    for method in METHODS:
        response = getattr(client, method)(url)
        statuses.append(response.status_code)
        if response.status_code not in (404, 405):
            failures.append(
                f"{route.name} [{method.upper()} {url}] returned {response.status_code}; "
                f"cross-tenant access must be indistinguishable from 'no such thing' (404)."
            )

    if not failures and 404 not in statuses:
        failures.append(
            f"{route.name} [{url}] never returned 404 (saw {statuses}); the request was rejected "
            f"before tenancy was ever checked, so this route is not actually covered."
        )
    return failures


def assert_cross_tenant_isolation(client: Any, victim: Tenancy, *, urlconf: str | None = None) -> None:
    """Sweep every tenant route with ``victim``'s ids using an outsider's client.

    ``client`` must be logged in as a user with **maximum** privilege in a
    different organization — an org owner and workspace admin. Testing with a
    low-privilege outsider would prove nothing: they would be refused on their
    role before tenancy was ever consulted.
    """
    failures: list[str] = []
    routes = iter_tenant_routes(urlconf)
    assert routes, "The IDOR sweep found no tenant routes at all — the walker is broken."
    for route in routes:
        failures.extend(check_route_is_isolated(client, route, victim))

    assert not failures, "Cross-tenant isolation failures:\n  " + "\n  ".join(failures)
