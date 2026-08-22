"""Global template context: the sidebar navigation and its data.

Ported from BrightBean Studio's ``apps/common/context_processors.py``, keeping
its shape (``{}`` for anonymous users, function-local model imports) and
rewriting the queries for chat.

**Deviation 4 of the L1-B brief lives here.** Studio computes sidebar active
state inline, per link, against ``request.resolver_match`` — at three different
granularities across 13 call sites (``url_name``; ``url_name`` A-or-B;
``app_name``; ``url_name`` *and* ``app_name``) — while its settings layouts use
a fourth convention entirely: a ``settings_active`` string that 11 separate
views have to remember to put in their context. Here the navigation is a data
structure, the ``active`` flag is computed once, and every template renders it
in a loop. A new page becomes an entry in a list, not a fifth convention.
"""

from dataclasses import dataclass
from typing import Any

from django.core.signals import setting_changed
from django.dispatch import receiver
from django.http import HttpRequest
from django.urls import NoReverseMatch, get_urlconf, reverse

# Resolved URLs, keyed by (urlconf, route name).
#
# Every nav row reverses its own route on every request — a dozen calls per
# page — and until issue #31 lands allauth, `account_logout` raises
# NoReverseMatch every single time, which makes Django build a message
# describing the whole failed lookup. None of it varies between requests for a
# given URLconf, so it is resolved once and remembered.
#
# The key includes the URLconf because tests swap it with override_settings,
# and the receiver below clears the cache whenever ROOT_URLCONF changes, so a
# test can never see another test's routes.
_URL_CACHE: dict[tuple[str | None, str, tuple[tuple[str, str], ...]], str | None] = {}


def reverse_cached(url_name: str, **kwargs: Any) -> str | None:
    """``reverse(url_name, kwargs=...)``, memoised. ``None`` when unresolvable.

    Workspace-scoped routes vary by workspace id, so the id is part of the key;
    a user switching workspace gets a fresh entry rather than the previous
    workspace's URL.
    """
    key = (get_urlconf(), url_name, tuple(sorted((k, str(v)) for k, v in kwargs.items())))
    if key not in _URL_CACHE:
        try:
            _URL_CACHE[key] = reverse(url_name, kwargs=kwargs or None)
        except NoReverseMatch:
            _URL_CACHE[key] = None
    return _URL_CACHE[key]


@receiver(setting_changed)
def _clear_url_cache(*, setting: str, **kwargs: Any) -> None:
    if setting in {"ROOT_URLCONF", "INSTALLED_APPS"}:
        _URL_CACHE.clear()


@dataclass(frozen=True)
class NavItem:
    """One row in a sidebar navigation group.

    ``url_name`` is the route to link to. ``url_names`` is the set of route
    names that light the row up, defaulting to just ``url_name``. It is a set
    because one nav entry legitimately covers several routes — a list page and
    its detail page are the same *section* to a reader, and Studio's
    "``create_landing`` or ``compose``" special case was exactly this, written
    by hand at one call site.

    Both are matched against ``resolver_match.view_name``, which includes the
    namespace, so a later app registering a generic ``list`` route cannot
    accidentally light up someone else's row.
    """

    key: str
    label: str
    icon: str
    url_name: str
    url_names: frozenset[str] = frozenset()
    badge_key: str = ""
    # Workspace-scoped rows reverse with {"workspace_id": ...} (SPEC §16), and
    # are hidden entirely when there is no current workspace — RBACMiddleware
    # leaves request.workspace None for a user whose workspaces are all
    # archived, and a row linking into a workspace that is not there is worse
    # than no row.
    workspace_scoped: bool = False

    def resolved(self, request: HttpRequest, badges: dict[str, int], workspace_id: Any = None) -> dict[str, Any]:
        matches = self.url_names or frozenset({self.url_name})
        match = request.resolver_match
        # view_name, not url_name: it carries the namespace, so "contacts:list"
        # and "flows:list" are different entries. Comparing bare url_name is
        # what forced Studio to hand-write a compound
        # `url_name == "list" and app_name == "notifications"` guard for the one
        # nav row where the collision had already bitten. Layer 2 onwards adds
        # namespaced apps with generic route names, so this is the difference
        # between the convention holding and needing that patch again.
        current = match.view_name if match else None
        return {
            "key": self.key,
            "label": self.label,
            "icon": self.icon,
            # "#" when a later layer owns the route and has not merged yet:
            # a dead link beats a 500 on every page of the app.
            "url": self._url(workspace_id),
            "active": current in matches,
            # A blank badge_key is never a key in `badges`, so this is 0.
            "badge": badges.get(self.badge_key, 0),
        }

    def _url(self, workspace_id: Any) -> str:
        if self.workspace_scoped:
            if workspace_id is None:
                return "#"
            return reverse_cached(self.url_name, workspace_id=workspace_id) or "#"
        return reverse_cached(self.url_name) or "#"


@dataclass(frozen=True)
class NavGroup:
    """A labelled run of nav items. An empty label renders no heading.

    ``items`` is a tuple, not a list. ``frozen=True`` stops the field being
    rebound but does nothing about mutating a list in place, and these groups
    are module-level singletons shared by every request in the worker — so
    ``group.items.append(...)``, the obvious way to add a conditional row,
    would leak that row into every subsequent response and grow without bound.
    A tuple turns that mistake into an immediate AttributeError.
    """

    label: str
    items: tuple[NavItem, ...] = ()


# --- The product's navigation ------------------------------------------------
# Deviation 7: this is BrightBean Chat's nav, not Studio's. Every target is a
# "coming soon" stub in apps/common/views.py that the owning issue replaces
# with the real view; the entry here does not change when that happens.
MAIN_NAV: list[NavGroup] = [
    NavGroup(
        label="",
        items=(
            NavItem(
                key="dashboard",
                label="Dashboard",
                icon="dashboard",
                url_name="workspaces:dashboard",
                workspace_scoped=True,
            ),
            NavItem(
                key="inbox",
                label="Inbox",
                icon="inbox",
                url_name="inbox",
                badge_key="unread_inbox",
                workspace_scoped=True,
            ),
            NavItem(key="contacts", label="Contacts", icon="contacts", url_name="contacts", workspace_scoped=True),
            NavItem(
                key="flows",
                label="Flows",
                icon="flows",
                url_name="flows:list",
                # The builder is the same section to a reader, so the row
                # stays lit while it is open (issue #6).
                url_names=frozenset({"flows:list", "flows:edit"}),
                workspace_scoped=True,
            ),
            NavItem(key="sequences", label="Sequences", icon="sequences", url_name="sequences", workspace_scoped=True),
            NavItem(
                key="broadcasts", label="Broadcasts", icon="broadcasts", url_name="broadcasts", workspace_scoped=True
            ),
            # Issue #16. The detail page is the same section to a reader, so it
            # lights the same row — that is what url_names is for.
            NavItem(
                key="media",
                label="Media",
                icon="image",
                url_name="media:library",
                url_names=frozenset({"media:library", "media:asset_detail"}),
                workspace_scoped=True,
            ),
            # Deliberately NOT workspace_scoped (issue #7): a notification is
            # addressed to a person, and the feed spans every workspace they
            # belong to — an alert about the workspace you are not currently
            # looking at is precisely the one you need to see.
            NavItem(
                key="notifications",
                label="Notifications",
                icon="bell",
                url_name="notifications:list",
                badge_key="unread_notifications",
            ),
        ),
    ),
]

SETTINGS_NAV: list[NavGroup] = [
    NavGroup(
        label="Account",
        items=(
            NavItem(key="profile", label="Profile", icon="user", url_name="accounts:settings"),
            NavItem(key="preferences", label="Preferences", icon="sliders", url_name="settings_preferences"),
        ),
    ),
    NavGroup(
        label="Organization",
        items=(
            NavItem(key="org_general", label="General", icon="building", url_name="organizations:settings"),
            NavItem(key="org_workspaces", label="Workspaces", icon="grid", url_name="organizations:workspaces"),
            NavItem(key="org_members", label="Team Members", icon="users", url_name="members:list"),
            NavItem(key="org_api_keys", label="API Keys", icon="key", url_name="settings_org_api_keys"),
        ),
    ),
    NavGroup(
        label="Workspace",
        items=(
            NavItem(
                key="ws_general",
                label="General",
                icon="settings",
                url_name="workspaces:settings",
                workspace_scoped=True,
            ),
            NavItem(
                key="ws_channels",
                label="Channels",
                icon="channels",
                url_name="channels:list",
                url_names=frozenset({"channels:list", "channels:create", "channels:detail"}),
                workspace_scoped=True,
            ),
            # Renamed from "Channels" by issue #4, which took that name for the
            # connection list above. The page's own heading has always read
            # "Platform credentials"; the nav row now agrees with it.
            NavItem(
                key="ws_credentials",
                label="Platform credentials",
                icon="key",
                url_name="credentials:list",
                workspace_scoped=True,
            ),
            NavItem(
                key="ws_fields",
                label="Fields",
                icon="fields",
                url_name="settings_ws_fields",
                workspace_scoped=True,
            ),
            NavItem(
                key="ws_tags",
                label="Tags",
                icon="tag",
                url_name="settings_ws_tags",
                workspace_scoped=True,
            ),
        ),
    ),
]


# Which settings groups each layout renders. One definition above, two views of
# it: layouts/settings.html is account- and org-scoped, while
# layouts/workspace_settings.html is scoped to the current workspace. Studio
# splits these too, and the split is not cosmetic — once issue #31 lands RBAC an
# Editor can reach workspace settings without being able to see organization
# settings, so a single shared nav would advertise pages the viewer cannot open.
ACCOUNT_SETTINGS_GROUPS = ("Account", "Organization")
WORKSPACE_SETTINGS_GROUPS = ("Workspace",)


def _subset(groups: list[NavGroup], labels: tuple[str, ...]) -> list[NavGroup]:
    return [g for g in groups if g.label in labels]


def _render_nav(
    groups: list[NavGroup], request: HttpRequest, badges: dict[str, int], workspace_id: Any = None
) -> list[dict[str, Any]]:
    """Render the groups, dropping rows that have nowhere to point.

    A workspace-scoped row with no current workspace is omitted rather than
    rendered dead: the user has no workspace to be in, so the section does not
    exist for them yet.
    """
    rendered = []
    for group in groups:
        items = [i.resolved(request, badges, workspace_id) for i in group.items]
        items = [i for i in items if i["url"] != "#"]
        if items:
            rendered.append({"label": group.label, "items": items})
    return rendered


def navigation_context(request: HttpRequest) -> dict[str, Any]:
    """Build the shell's navigation payload.

    Split out of :func:`sidebar_context` so a view can render the app chrome
    for a request the context processor deliberately skips — the UI style guide
    at ``/ui/`` is the only such caller, and it exists so the design system
    stays inspectable without a session.
    """
    # Model imports stay inside the function. A context processor is imported
    # by dotted path while the template engine is being configured, which can
    # precede app-registry population; a module-level model import would raise
    # AppRegistryNotReady. It also means the anonymous path in sidebar_context
    # imports nothing at all.
    from apps.members.models import WorkspaceMembership
    from apps.members.roles import OrgRole

    badges: dict[str, int] = {}

    # TODO(L4-D): unread inbox count, once inbox.Conversation exists (issue #14).
    badges["unread_inbox"] = 0

    # Issue #7. Guarded on authentication rather than assumed: this function is
    # also called directly by apps.common.views.ui_demo, whose docstring
    # promises /ui/ "reads no database and no session" and which serves
    # anonymous visitors. An unguarded per-user count would break that promise
    # and blow up on AnonymousUser.
    badges["unread_notifications"] = 0
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        from apps.notifications.selectors import unread_count_for

        badges["unread_notifications"] = unread_count_for(request.user)
    # Also returned under its own name below: the nav row reads it out of
    # `badges`, but the bell and the mobile bar sit outside the nav loop and
    # need it directly, and the notification views re-supply the same key so one
    # partial serves both the first render and every htmx swap.

    # RBACMiddleware (issue #31) resolves these before any view runs. getattr
    # rather than attribute access because /ui/ renders the chrome for requests
    # that never went through the middleware.
    workspace = getattr(request, "workspace", None)
    org_membership = getattr(request, "org_membership", None)

    sidebar_workspaces: list[dict[str, Any]] = []
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        # The switcher lists what this user can actually reach, not what the
        # org contains — membership is the authority, and archived workspaces
        # are not somewhere anyone can be sent.
        memberships = (
            WorkspaceMembership.objects.filter(user=request.user, workspace__is_archived=False)
            .select_related("workspace")
            .order_by("workspace__name")
        )
        sidebar_workspaces = [
            {
                "name": m.workspace.name,
                "url": reverse_cached("workspaces:dashboard", workspace_id=m.workspace_id) or "#",
                "is_current": workspace is not None and m.workspace_id == workspace.id,
            }
            for m in memberships
        ]

    # Creating a workspace is an org-tier action (issue #31's RBAC), so the
    # control is hidden rather than rendered and refused.
    can_create_workspace = org_membership is not None and org_membership.org_role in {
        OrgRole.OWNER,
        OrgRole.ADMIN,
    }

    # TODO(L2-B): connected channels for the sidebar's channel list, once
    # channels.ChannelConnection exists (issue #4). The credential store from
    # #31 is per-platform configuration, not a connected account.
    channel_connections: list[Any] = []

    workspace_id = workspace.id if workspace is not None else None
    return {
        "nav_groups": _render_nav(MAIN_NAV, request, badges, workspace_id),
        "settings_nav_groups": _render_nav(
            _subset(SETTINGS_NAV, ACCOUNT_SETTINGS_GROUPS), request, badges, workspace_id
        ),
        "workspace_settings_nav_groups": _render_nav(
            _subset(SETTINGS_NAV, WORKSPACE_SETTINGS_GROUPS), request, badges, workspace_id
        ),
        "sidebar_workspaces": sidebar_workspaces,
        "current_workspace": workspace,
        "can_create_workspace": can_create_workspace,
        "channel_connections": channel_connections,
        "unread_notification_count": badges["unread_notifications"],
        # Named rather than indexed out of the nav in the template. Positional
        # lookup (`settings_nav_groups.0.items.0.url`) fails soft in Django, so
        # reordering SETTINGS_NAV would silently retarget the footer link — and
        # reordering being safe is the whole point of nav-as-data.
        "settings_home_url": reverse_cached("accounts:settings") or "#",
        # Where "Back to app" goes. The dashboard is workspace-scoped, so a
        # user with no current workspace (every one archived) is sent to the
        # org's workspace list — the only place they can bring one back.
        "app_home_url": (
            reverse_cached("workspaces:dashboard", workspace_id=workspace_id)
            if workspace_id is not None
            else reverse_cached("organizations:workspaces")
        )
        or "/",
        "create_workspace_url": reverse_cached("organizations:workspaces") or "#",
        "logout_url": reverse_cached("account_logout"),
        # The shell renders its chrome when this is true. It tracks
        # authentication, and /ui/ overrides it (see navigation_context).
        "show_app_shell": True,
    }


def sidebar_context(request: HttpRequest) -> dict[str, Any]:
    """Inject the sidebar's navigation and data into every template context.

    Returns ``{}`` for anonymous requests, exactly as Studio does — not a dict
    of empty defaults. Every ``{% if nav_groups %}`` in the shell then falls
    through cleanly, and the login and landing pages cost zero queries.

    The two-part guard is deliberate: ``hasattr(request, "user")`` covers
    requests that never went through ``AuthenticationMiddleware`` — a bare
    ``RequestFactory`` request in a test, or a template rendered from a
    management command.
    """
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {}
    return navigation_context(request)
