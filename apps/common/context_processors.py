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
_URL_CACHE: dict[tuple[str | None, str], str | None] = {}


def reverse_cached(url_name: str) -> str | None:
    """``reverse(url_name)``, memoised per URLconf. ``None`` when unresolvable."""
    key = (get_urlconf(), url_name)
    if key not in _URL_CACHE:
        try:
            _URL_CACHE[key] = reverse(url_name)
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

    def resolved(self, request: HttpRequest, badges: dict[str, int]) -> dict[str, Any]:
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
            "url": reverse_cached(self.url_name) or "#",
            "active": current in matches,
            # A blank badge_key is never a key in `badges`, so this is 0.
            "badge": badges.get(self.badge_key, 0),
        }


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
            NavItem(key="dashboard", label="Dashboard", icon="dashboard", url_name="dashboard"),
            NavItem(key="inbox", label="Inbox", icon="inbox", url_name="inbox", badge_key="unread_inbox"),
            NavItem(key="contacts", label="Contacts", icon="contacts", url_name="contacts"),
            NavItem(key="flows", label="Flows", icon="flows", url_name="flows"),
            NavItem(key="sequences", label="Sequences", icon="sequences", url_name="sequences"),
            NavItem(key="broadcasts", label="Broadcasts", icon="broadcasts", url_name="broadcasts"),
        ),
    ),
]

SETTINGS_NAV: list[NavGroup] = [
    NavGroup(
        label="Account",
        items=(
            NavItem(key="profile", label="Profile", icon="user", url_name="settings_profile"),
            NavItem(key="preferences", label="Preferences", icon="sliders", url_name="settings_preferences"),
        ),
    ),
    NavGroup(
        label="Organization",
        items=(
            NavItem(key="org_general", label="General", icon="building", url_name="settings_org_general"),
            NavItem(key="org_workspaces", label="Workspaces", icon="grid", url_name="settings_org_workspaces"),
            NavItem(key="org_members", label="Team Members", icon="users", url_name="settings_org_members"),
            NavItem(key="org_api_keys", label="API Keys", icon="key", url_name="settings_org_api_keys"),
        ),
    ),
    NavGroup(
        label="Workspace",
        items=(
            NavItem(key="ws_general", label="General", icon="settings", url_name="settings_ws_general"),
            NavItem(key="ws_channels", label="Channels", icon="channels", url_name="settings_ws_channels"),
            NavItem(key="ws_fields", label="Fields", icon="fields", url_name="settings_ws_fields"),
            NavItem(key="ws_tags", label="Tags", icon="tag", url_name="settings_ws_tags"),
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


def _render_nav(groups: list[NavGroup], request: HttpRequest, badges: dict[str, int]) -> list[dict[str, Any]]:
    return [{"label": g.label, "items": [i.resolved(request, badges) for i in g.items]} for g in groups]


def navigation_context(request: HttpRequest) -> dict[str, Any]:
    """Build the shell's navigation payload.

    Split out of :func:`sidebar_context` so a view can render the app chrome
    for a request the context processor deliberately skips — the UI style guide
    at ``/ui/`` is the only such caller, and it exists so the design system
    stays inspectable before issue #31 lands a way to log in.
    """
    # Model imports stay inside the function. A context processor is imported
    # by dotted path while the template engine is being configured, which can
    # precede app-registry population; a module-level model import would raise
    # AppRegistryNotReady. It also means the anonymous path in sidebar_context
    # imports nothing at all.
    badges: dict[str, int] = {}

    # TODO(L4-D): unread inbox count, once inbox.Conversation exists (issue #14).
    badges["unread_inbox"] = 0

    # TODO(L1-A): the workspace switcher, once workspaces.Workspace and
    # members.WorkspaceMembership exist (issue #31).
    #
    # THE TEMPLATE CONTRACT IS FIXED HERE, not left for #31 to infer. Each entry
    # is a plain dict — {"name", "url", "is_current"} — rather than a model
    # instance, because base.html reads `.url` on it and a Workspace has no such
    # attribute unless someone remembers to add get_absolute_url. Handing the
    # template a shape it cannot silently fail to satisfy is the point: an
    # undefined key renders as the empty string, so `href="{{ ws.url }}"`
    # becomes href="" and the link quietly reloads the current page.
    sidebar_workspaces: list[dict[str, Any]] = []
    current_workspace = None
    can_create_workspace = False

    # TODO(L2-B): connected channels, once channels.ChannelConnection exists
    # (issue #4). Consumed by the sidebar's channel list and the settings nav.
    channel_connections: list[Any] = []

    return {
        "nav_groups": _render_nav(MAIN_NAV, request, badges),
        "settings_nav_groups": _render_nav(_subset(SETTINGS_NAV, ACCOUNT_SETTINGS_GROUPS), request, badges),
        "workspace_settings_nav_groups": _render_nav(_subset(SETTINGS_NAV, WORKSPACE_SETTINGS_GROUPS), request, badges),
        "sidebar_workspaces": sidebar_workspaces,
        "current_workspace": current_workspace,
        "can_create_workspace": can_create_workspace,
        # Named rather than indexed out of the nav in the template. Positional
        # lookup (`settings_nav_groups.0.items.0.url`) fails soft in Django, so
        # reordering SETTINGS_NAV would silently retarget the footer link — and
        # reordering being safe is the whole point of nav-as-data.
        "settings_home_url": reverse_cached("settings_profile") or "#",
        # The switcher's "New workspace" target. #31 supplies the real route;
        # until then the control is hidden by can_create_workspace being False.
        "create_workspace_url": reverse_cached("settings_org_workspaces") or "#",
        "channel_connections": channel_connections,
        # Rendered as a POST form in the sidebar footer. allauth's route
        # arrives with issue #31; until then there is nothing to sign out of,
        # and the footer omits the control rather than 500-ing on {% url %}.
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
