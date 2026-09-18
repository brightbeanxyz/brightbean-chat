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
from django.utils.functional import SimpleLazyObject

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


def reverse_cached(url_name: str, *, cache: bool = True, **kwargs: Any) -> str | None:
    """``reverse(url_name, kwargs=...)``, memoised. ``None`` when unresolvable.

    Workspace-scoped routes vary by workspace id, so the id is part of the key;
    a user switching workspace gets a fresh entry rather than the previous
    workspace's URL.

    ``cache=False`` skips the memo and still swallows ``NoReverseMatch`` —
    for a route keyed on a row id, where remembering every answer would grow
    this module-level dict by one entry per row in the deployment and never
    shed one. The *never raises* half is the point of calling this at all: a
    context processor runs on every response, including the error pages, so a
    bare ``reverse`` here turns one unmounted route into a blank 500 on every
    page of the product.
    """
    if not cache:
        try:
            return reverse(url_name, kwargs=kwargs or None)
        except NoReverseMatch:
            return None
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
    #: The workspace permission key the row's page is gated on, from
    #: ``apps.members.roles.PERMISSION_KEYS``. A viewer without it does not see
    #: the row.
    #:
    #: This replaced the two-list split (``ACCOUNT_SETTINGS_GROUPS`` /
    #: ``WORKSPACE_SETTINGS_GROUPS``), which approximated the same idea at group
    #: granularity and got both halves wrong. It hid every Workspace row from
    #: the account settings page — the page the account menu's Settings row lands on
    #: — so Channels, Tags, Labels and five more had no entry point in the
    #: product at all. And within a group it hid nothing, so an Editor still saw
    #: rows they would be refused at. Gating the row on the same key its view
    #: gates on means one settings nav that is both complete and honest.
    permission: str = ""
    #: The organisation role the row's page requires, "member" or "admin".
    #: Checked against ORG_ROLE_LEVEL, so "member" passes for an admin too.
    org_role: str = ""
    #: A template that draws this row instead of the default anchor, for a row
    #: that is more than a link. Read by partials/_sidebar_items.html, which
    #: hands it the resolved item and nothing else. Exactly one row uses it —
    #: Notifications, which is also the trigger for the bell's panel — and the
    #: alternative was a second nav renderer, or an `{% if item.key ==
    #: "notifications" %}` in the shared one. A field keeps the special case
    #: with the row that is special.
    partial: str = ""
    #: Rows that exist only for a signed-in person. ``sidebar_context`` returns
    #: ``{}`` for anonymous requests, so this matters for exactly one caller:
    #: apps.common.views.ui_demo, which calls ``navigation_context`` directly
    #: for visitors with no session and whose docstring promises /ui/ "reads no
    #: database and no session".
    authenticated_only: bool = False

    def visible_to(self, request: HttpRequest) -> bool:
        """Whether this viewer may open the page behind the row.

        Advertising a page somebody is refused at is worse than omitting it:
        they cannot tell a permission from a bug. Unset gates mean "everyone",
        which is what every sidebar row and every tab uses.
        """
        if self.authenticated_only:
            user = getattr(request, "user", None)
            if user is None or not user.is_authenticated:
                return False
        if self.permission:
            membership = getattr(request, "workspace_membership", None)
            if membership is None:
                return False
            if not membership.effective_permissions.get(self.permission, False):
                return False
        if self.org_role:
            from apps.members.roles import ORG_ROLE_LEVEL

            membership = getattr(request, "org_membership", None)
            if membership is None:
                return False
            held = ORG_ROLE_LEVEL.get(membership.org_role, 0)
            if held < ORG_ROLE_LEVEL.get(self.org_role, 0):
                return False
        return True

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
            # Whether the row gets a badge *element* at all — a different
            # question from whether the count is non-zero. The owning app keeps
            # its number live with an htmx out-of-band swap, and htmx resolves
            # such a swap by id, so the element has to exist at zero too or the
            # notification bell's 60s poll logs htmx:oobErrorNoTarget on every
            # page for the whole session. templates/partials/_nav_badge.html
            # renders the zero state as an empty, hidden span for that reason.
            #
            # Rows no app owns get no slot: an id nothing swaps is dead weight,
            # and templates/partials/_nav_badge_sinks.html mirrors this same
            # list for the settings layouts, which replace the main nav
            # wholesale and would otherwise leave the poll with no target at
            # all — the bug this flag exists to close.
            "badge_slot": bool(self.badge_key),
            # Empty for every row but Notifications; the renderer falls back to
            # its own anchor markup when this is blank.
            "partial": self.partial,
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


SETTINGS_NAV: list[NavGroup] = [
    NavGroup(
        label="Workspace",
        items=(
            NavItem(
                key="ws_general",
                permission="manage_workspace_settings",
                label="General",
                icon="settings",
                url_name="workspaces:settings",
                workspace_scoped=True,
            ),
            NavItem(
                key="ws_channels",
                permission="manage_channels",
                label="Channels",
                icon="channels",
                url_name="channels:list",
                url_names=frozenset({"channels:list", "channels:create", "channels:detail"}),
                workspace_scoped=True,
            ),
            # Library was a sidebar row, in the footer group beside Settings.
            # It is workspace content you set up rather than a place you work,
            # so it reads here beside Channels — and the sidebar is down to the
            # six rows that answer "what am I doing".
            #
            # Ungated, unlike its neighbours: media_library.views.library takes
            # `manage_media` for uploads and mutations only, and any member of
            # the workspace may browse what is in it.
            NavItem(
                key="media",
                label="Library",
                icon="image",
                url_name="media:library",
                url_names=frozenset({"media:library", "media:asset_detail"}),
                workspace_scoped=True,
            ),
            # Two rows, not one. The redesign draws a single "Tags & labels",
            # and collapsing them to hit that count left the Labels page with no
            # link anywhere in the product — the row pointed at the tag list,
            # which renders contact tags only.
            #
            # They are also not the same thing, and the Labels page says so in
            # its own first paragraph: a tag like "VIP" follows a person across
            # every channel they use, a label like "waiting on shipping" is true
            # of one thread and stops being true when it is answered. They are
            # different models answering to different permissions. One row would
            # have to lead somewhere that hides half of what it promises.
            NavItem(
                key="ws_tags",
                permission="manage_crm",
                label="Tags",
                icon="tag",
                url_name="contacts:tag_list",
                workspace_scoped=True,
            ),
            NavItem(
                key="ws_labels",
                permission="reply_in_inbox",
                label="Labels",
                icon="tag",
                url_name="inbox:label_settings",
                workspace_scoped=True,
            ),
            NavItem(
                key="ws_inbox_rules",
                permission="manage_workspace_settings",
                label="Inbox rules",
                icon="flows",
                url_name="inbox:rule_settings",
                workspace_scoped=True,
            ),
            # The redesign names four Workspace rows; the product has nine
            # settings pages and these four have no other entry point anywhere
            # in the app. Extra rows in a named group is a far smaller
            # deviation than four unreachable pages.
            NavItem(
                key="ws_fields",
                permission="manage_crm",
                label="Fields",
                icon="fields",
                url_name="contacts:field_list",
                workspace_scoped=True,
            ),
            NavItem(
                key="ws_email_tracking",
                permission="manage_workspace_settings",
                label="Email tracking",
                icon="analytics",
                url_name="analytics:tracking_settings",
                workspace_scoped=True,
            ),
            # Outbound webhooks (issue #25). Workspace-scoped, unlike the
            # org-tier "Developers" row below: SPEC §5 gives outbound_webhook a
            # workspace_id, so its url, secret and subscriptions belong to one
            # workspace's data.
            NavItem(
                key="ws_webhooks",
                permission="manage_workspace_settings",
                label="Webhooks",
                icon="channels",
                url_name="api_webhooks:list",
                url_names=frozenset({"api_webhooks:list", "api_webhooks:detail"}),
                workspace_scoped=True,
            ),
        ),
    ),
    NavGroup(
        label="Organisation",
        items=(
            NavItem(
                key="org_general",
                label="General",
                icon="building",
                url_name="organizations:settings",
                org_role="member",
            ),
            NavItem(
                key="org_workspaces",
                label="Workspaces",
                icon="grid",
                url_name="organizations:workspaces",
                org_role="member",
            ),
            NavItem(
                key="org_members", label="People & roles", icon="users", url_name="members:list", org_role="member"
            ),
            NavItem(
                key="org_billing",
                label="Plan & billing",
                icon="billing",
                url_name="organizations:billing",
                org_role="member",
            ),
            NavItem(
                key="org_api_keys",
                org_role="admin",
                label="Developers",
                icon="key",
                url_name="settings_org_api_keys",
                # The issuance response is its own page, so the row has to stay
                # lit while the operator is copying the key off it.
                url_names=frozenset({"settings_org_api_keys", "api_keys_issue"}),
            ),
            # Your own profile, in the organisation's group rather than under a
            # "You" heading of its own. The heading was one row long, and the
            # row is where the account menu's "Organization Settings" lands —
            # so the group that opens is the group it belongs to. Ungated like
            # the Library row above: every viewer has a profile.
            #
            # "Notifications" used to sit beside it, pointing at the placeholder
            # in config/urls.py's _GLOBAL_STUBS. A settings row whose whole
            # content is "Preferences is not built yet. Lands with issue #31
            # follow-up." is a dead end with an issue number on it — worse than
            # no row at all. The route stays (it is a real endpoint, and tests
            # walk it); what is gone is the promise in the nav. Put the row back
            # in the same commit as the page.
            NavItem(key="profile", label="Profile", icon="user", url_name="accounts:settings"),
        ),
    ),
]


# --- The product's navigation ------------------------------------------------
# The sidebar (SPEC §16's shell): the rows that answer "what am I doing", and
# nothing else. There used to be a second group pinned to the footer, carrying
# Library and Settings. Settings is gone from the sidebar — the account menu
# and the workspace switcher both lead there, so a third way in was a row
# spent on something nobody navigates to twice a day — and Library went into
# SETTINGS_NAV's Workspace group with the rest of the workspace's setup.
#
# Keys track the route; labels track the design. That is why `dashboard`,
# `analytics` and `media` keep their keys while reading Home, Insights and
# Library — every cross-app test that looks a row up by key keeps working, and
# a rename in the design costs one string.
MAIN_NAV: list[NavGroup] = [
    NavGroup(
        label="",
        items=(
            NavItem(
                key="dashboard",
                label="Home",
                icon="home",
                url_name="workspaces:dashboard",
                workspace_scoped=True,
            ),
            NavItem(
                key="inbox",
                label="Inbox",
                icon="inbox",
                # An open thread is the same section to a reader, so the row
                # stays lit on a deep link into one (issue #14).
                url_name="inbox:list",
                url_names=frozenset({"inbox:list", "inbox:thread"}),
                badge_key="unread_inbox",
                workspace_scoped=True,
            ),
            # Sequences left the nav and became a tab on this page, so the row
            # stays lit while either half of "automations" is open.
            NavItem(
                key="flows",
                label="Flows",
                icon="flows",
                url_name="flows:list",
                url_names=frozenset(
                    {"flows:list", "flows:edit", "flows:template_gallery", "campaigns:list", "campaigns:detail"}
                ),
                workspace_scoped=True,
            ),
            NavItem(
                key="broadcasts",
                label="Broadcasts",
                icon="broadcasts",
                url_name="broadcasts:list",
                url_names=frozenset({"broadcasts:list", "broadcasts:detail", "broadcasts:compose"}),
                workspace_scoped=True,
            ),
            NavItem(
                key="contacts",
                label="Contacts",
                icon="contacts",
                url_name="contacts:list",
                url_names=frozenset(
                    {"contacts:list", "contacts:detail", "contacts:import_list", "contacts:import_detail"}
                ),
                workspace_scoped=True,
            ),
            # apps.analytics is an optional install, so this row legitimately
            # reverses to "#" and is dropped on deployments without it. The
            # sidebar's nav is laid out with flex for that reason: it must not
            # assume a fixed number of rows.
            NavItem(
                key="analytics",
                label="Insights",
                icon="analytics",
                url_name="analytics:overview",
                url_names=frozenset({"analytics:overview", "analytics:flow_detail"}),
                workspace_scoped=True,
            ),
            # Back in the nav, where it was before the header existed. The bell
            # lived in the appbar because that was the one surface on every
            # page; the sidebar is that surface now, and a row can carry both
            # the unread count and the panel the bell used to open.
            #
            # Deliberately NOT workspace_scoped: a notification is addressed to
            # a person and the feed spans every workspace they belong to (see
            # apps/notifications/urls.py). It is therefore the one main-nav row
            # that survives a user whose workspaces are all archived — which is
            # exactly when a channel_needs_reauth alert matters most.
            NavItem(
                key="notifications",
                label="Notifications",
                icon="bell",
                url_name="notifications:list",
                url_names=frozenset({"notifications:list"}),
                badge_key="unread_notifications",
                partial="notifications/partials/_bell.html",
                authenticated_only=True,
            ),
        ),
    ),
]


# The tabs across the top of the automations section. Rendered through the same
# _render_nav as every other nav, so active state stays one convention.
FLOWS_TABS: list[NavGroup] = [
    NavGroup(
        label="",
        items=(
            NavItem(
                key="tab_flows",
                label="Flows",
                icon="flows",
                url_name="flows:list",
                url_names=frozenset({"flows:list", "flows:edit"}),
                workspace_scoped=True,
            ),
            NavItem(
                key="tab_sequences",
                label="Sequences",
                icon="sequences",
                url_name="campaigns:list",
                url_names=frozenset({"campaigns:list", "campaigns:detail"}),
                workspace_scoped=True,
            ),
            NavItem(
                key="tab_templates",
                label="Templates",
                icon="grid",
                url_name="flows:template_gallery",
                # Unlike its two neighbours, the page behind this tab is gated:
                # views_portability.template_gallery requires edit_flows,
                # because picking a template writes a FlowImport row. Without
                # the key here an Agent saw a tab that answered 403 while Flows
                # and Sequences beside it read fine.
                permission="edit_flows",
                workspace_scoped=True,
            ),
        ),
    ),
]


# There used to be two group lists here — one for layouts/settings.html and one
# for layouts/workspace_settings.html — so that an Editor reaching workspace
# settings would not be shown organisation rows they cannot open.
#
# The intent was right and the mechanism was too coarse, in both directions. It
# hid every Workspace row from the account settings page, which is where the
# account menu's Settings row lands, so Channels, Tags, Labels and five more had no
# entry point anywhere in the product once the first-run checklist was done.
# And inside a group it filtered nothing, so an Editor still saw the rows they
# would be refused at.
#
# Both halves are fixed by gating each ROW on the key its own view is gated on
# (NavItem.permission / NavItem.org_role). One settings nav, filtered per
# viewer, which is what the design drew and what is now safe to draw.


def _render_nav(
    groups: list[NavGroup], request: HttpRequest, badges: dict[str, int], workspace_id: Any = None
) -> list[dict[str, Any]]:
    """Render the groups, dropping rows the viewer cannot use.

    Two reasons a row is omitted. A workspace-scoped row with no current
    workspace has nowhere to point — the user has no workspace to be in, so the
    section does not exist for them yet. And a row whose page the viewer lacks
    the permission for is not shown at all, because a link that always answers
    403 reads as a bug rather than as a boundary (see NavItem.visible_to).

    A group whose every row is dropped renders no heading either.
    """
    rendered = []
    for group in groups:
        allowed = [i for i in group.items if i.visible_to(request)]
        items = [i.resolved(request, badges, workspace_id) for i in allowed]
        items = [i for i in items if i["url"] != "#"]
        if items:
            rendered.append({"label": group.label, "items": items})
    return rendered


#: What the sidebar's channel block hands the template when there is nothing to
#: draw — no workspace, no membership, no permission, or no channels app in this
#: deployment. A constant so every early return is the same shape.
_NO_CHANNELS: dict[str, Any] = {"connections": [], "connectable": [], "home_url": None}


def _sidebar_channels(request: HttpRequest, workspace: Any, membership: Any, workspace_id: Any) -> dict[str, Any]:
    """The sidebar's channel block: what is connected, what is not, and the way in.

    Both lists are gated on ``manage_channels``, the key every view behind them
    is gated on (``apps.channels.views`` and each platform's connect flow), for
    the reason NavItem.visible_to states: a row that always answers 403 reads as
    a bug rather than as a boundary. An Agent sees neither list.

    **A platform leaves the connect list only while it is ACTIVE.** That is the
    rule ``apps.flows.capabilities.connected_platforms`` and the dashboard's
    setup checklist both apply, and for the same reason: a ``disabled`` or
    ``needs_reauth`` connection cannot deliver, so treating it as "you have
    Telegram" would leave the one surface on every page offering no way to
    connect Telegram at all. Derived from the rows already loaded rather than by
    calling that helper, because this runs on every render and one query is the
    budget; ``test_the_connect_list_agrees_with_connected_platforms`` pins the
    two against each other so they cannot drift.

    The whole list is returned, and partials/_sidebar_channels.html draws only
    the first three of it — how many rows fit under a heading is the template's
    business, and the rule above is only checkable against
    ``connected_platforms`` while this stays the complete answer.

    Everything is resolved defensively, because a context processor runs on
    every response including error pages: the app may not be installed
    (``installed_model``), and the routes may not be mounted (``reverse_cached``
    and the guard on ``home_url``). Any of those misses and the block is simply
    absent.

    Returns ``_NO_CHANNELS`` when there is nothing to draw.
    """
    if workspace is None or workspace_id is None or membership is None:
        return _NO_CHANNELS
    if not membership.effective_permissions.get("manage_channels", False):
        return _NO_CHANNELS

    # Function-local for the same reason every model import in this module is:
    # a context processor is imported by dotted path while the template engine
    # is being configured, which can precede app-registry population.
    from apps.channels.registry import connect_route_for
    from apps.common.platforms import Platform
    from apps.flows.compat import installed_model

    # Not a direct import: the channels app is one a deployment may leave out
    # (apps/flows/capabilities.py resolves the same model this way, and
    # config/urls.py guards apps.analytics likewise), and an ImportError here
    # would take down every page rather than one block.
    model = installed_model("channels", "apps.channels", "ChannelConnection")
    # `channels:list` is the block's own way in AND the proof that the app's
    # routes are mounted at all: with it unresolvable, nothing below could
    # reverse either, so the whole block steps aside instead of raising.
    home_url = reverse_cached("channels:list", workspace_id=workspace_id)
    if model is None or home_url is None:
        return _NO_CHANNELS

    from apps.channels.models import ConnectionStatus

    match = request.resolver_match
    open_connection = (
        str(match.kwargs.get("connection_id", "")) if match and match.view_name == "channels:detail" else ""
    )

    connections: list[dict[str, Any]] = []
    live: set[str] = set()
    # `only`: the sidebar draws a glyph, a name and one status. The encrypted
    # credentials column in particular is deserialised on access, and nothing
    # here reads it.
    rows = model.objects.for_workspace(workspace).only("id", "platform", "display_name", "status")
    for connection in rows:
        if connection.status == ConnectionStatus.ACTIVE:
            live.add(connection.platform)
        url = reverse_cached("channels:detail", workspace_id=workspace_id, connection_id=connection.pk, cache=False)
        if url is None:  # pragma: no cover - home_url reversing means this does too
            continue
        connections.append(
            {
                "name": connection.display_name,
                "platform": connection.platform,
                "url": url,
                # The same active convention every nav row follows (deviation 4
                # in this module's docstring), computed once here rather than
                # by the template comparing ids.
                "active": open_connection == str(connection.pk),
                # Two states mean "this is not carrying messages", and the shell
                # says which: a revoked connection needs somebody, a disabled one
                # is somebody's own decision. Without them a dead channel looks
                # exactly like a quiet one until the settings page is opened.
                "needs_reauth": connection.status == ConnectionStatus.NEEDS_REAUTH,
                "disabled": connection.status == ConnectionStatus.DISABLED,
            }
        )

    connectable: list[dict[str, Any]] = []
    for value, label in Platform.choices:
        if value in live:
            continue
        route = connect_route_for(value)
        # A platform with no guided flow yet, or one whose route is not mounted
        # in this deployment, gets no row rather than a dead one — the rule
        # NavItem.visible_to and _render_nav both follow.
        url = reverse_cached(route, workspace_id=workspace_id) if route else None
        if url:
            connectable.append({"platform": value, "label": label, "url": url})

    return {"connections": connections, "connectable": connectable, "home_url": home_url}


def navigation_context(request: HttpRequest) -> dict[str, Any]:
    """Build the shell's navigation payload.

    Split out of :func:`sidebar_context` so a view can render the app chrome
    for a request the context processor deliberately skips — the UI style guide
    at ``/ui/`` is the only such caller, and it exists so the design system
    stays inspectable without a session.

    Everything workspace-shaped is read off ``request`` the way RBACMiddleware
    leaves it, which is also how ``/ui/`` gets a populated sidebar without a
    session: it sets its own stand-in workspace on the request before calling
    here, so this function needs no idea that the style guide exists.
    """
    # Model imports stay inside the function. A context processor is imported
    # by dotted path while the template engine is being configured, which can
    # precede app-registry population; a module-level model import would raise
    # AppRegistryNotReady. It also means the anonymous path in sidebar_context
    # imports nothing at all.
    from apps.members.models import WorkspaceMembership
    from apps.members.roles import OrgRole

    badges: dict[str, int] = {}

    # Issue #7. Guarded on authentication rather than assumed: this function is
    # also called directly by apps.common.views.ui_demo, whose docstring
    # promises /ui/ "reads no database and no session" and which serves
    # anonymous visitors. An unguarded per-user count would break that promise
    # and blow up on AnonymousUser.
    badges["unread_notifications"] = 0
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        from apps.notifications.selectors import unread_count_for

        badges["unread_notifications"] = unread_count_for(request.user)
    # Also returned under its own name below. The nav row reads it out of
    # `badges` — the bell's trigger IS that row now — but the bell panel's
    # "Mark all read" guard and the notification views need it directly, and
    # those views re-supply the same key so one partial serves both the first
    # render and every htmx swap.

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
                # The switcher draws each workspace's mark beside its name, so
                # the emoji comes along with it — partials/_logo.html reads
                # `.icon` then falls back to the name's initial, and a dict
                # answers both lookups the same way a model does.
                "icon": m.workspace.icon,
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

    # Issue #14: open conversations carrying an inbound message this member has
    # not read. Guarded on a resolved workspace as well as on authentication —
    # apps.common.views.ui_demo calls this function for anonymous visitors and
    # its docstring promises /ui/ "reads no database and no session" — and on
    # the permission, because a member without use_inbox never sees the row the
    # badge sits on and counting for them would be a query nobody reads.
    badges["unread_inbox"] = 0
    user = getattr(request, "user", None)
    membership = getattr(request, "workspace_membership", None)
    if (
        user is not None
        and user.is_authenticated
        and workspace is not None
        and membership is not None
        and membership.effective_permissions.get("use_inbox", False)
    ):
        from apps.inbox.selectors import unread_count_for as inbox_unread_count

        badges["unread_inbox"] = inbox_unread_count(workspace, user)

    workspace_id = workspace.id if workspace is not None else None

    # Deferred, not computed: a context processor runs on every ``render()``,
    # and most of those are htmx fragments — the bell badge every 60s per tab,
    # the inbox list every 3s — which swap a span and never draw a sidebar.
    # Evaluated on first touch, once, by the one template that reads it.
    channels = SimpleLazyObject(lambda: _sidebar_channels(request, workspace, membership, workspace_id))

    # The switcher's last row, and the account menu's one framed control. Both
    # are gated the way the settings rows they lead to are gated — see
    # NavItem.visible_to — rather than rendered and refused at: somebody who
    # cannot tell a permission from a bug is the person a dead row costs most.
    workspace_settings_url = None
    if (
        workspace_id is not None
        and membership is not None
        and membership.effective_permissions.get("manage_workspace_settings", False)
    ):
        workspace_settings_url = reverse_cached("workspaces:settings", workspace_id=workspace_id)
    members_url = reverse_cached("members:list") if org_membership is not None else None

    settings_nav = _render_nav(SETTINGS_NAV, request, badges, workspace_id)
    main_nav = _render_nav(MAIN_NAV, request, badges, workspace_id)
    # The bell's row, pulled out by key so a settings page can pin it without
    # rendering the product's nav around it (partials/_app_sidebar.html).
    #
    # Not a second copy: it IS the row from `nav_groups`, and the two are never
    # drawn together — a page draws the product's rows or the settings ones.
    # That matters because notifications/partials/_bell.html carries the id the
    # 60s poll swaps into, and a page holding two of them sends every swap to
    # whichever comes first. Pulled out here rather than filtered in the
    # template so partials/_sidebar_items.html stays a renderer that does not
    # know what any particular row is.
    notifications_row = next(
        (item for group in main_nav for item in group["items"] if item["key"] == "notifications"),
        None,
    )
    return {
        # The sidebar's nav. One group now: the footer holds the account
        # block and the collapse toggle, and no rows.
        "nav_groups": main_nav,
        "notifications_row": notifications_row,
        "flow_tab_groups": _render_nav(FLOWS_TABS, request, badges, workspace_id),
        # One nav, two names. Both layouts render the same filtered list; the
        # second key is kept so the fourteen templates extending either layout
        # need no edit.
        "settings_nav_groups": settings_nav,
        "workspace_settings_nav_groups": settings_nav,
        "sidebar_workspaces": sidebar_workspaces,
        "current_workspace": workspace,
        "can_create_workspace": can_create_workspace,
        # The shell's channel block, as one object rather than three keys:
        # `channels.connections`, `channels.connectable` and
        # `channels.home_url`. partials/_sidebar_channels.html draws it, and
        # touching any of the three is what runs the query (see above).
        "channels": channels,
        "unread_notification_count": badges["unread_notifications"],
        # Named rather than indexed out of the nav in the template. Positional
        # lookup (`settings_nav_groups.0.items.0.url`) fails soft in Django, so
        # reordering SETTINGS_NAV would silently retarget the account menu's
        # Settings row — and reordering being safe is the whole point of
        # nav-as-data. It is also the only Settings link left in the sidebar
        # now that the footer's row is gone.
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
        # These two are None rather than "#" when they are not on offer: the
        # templates test them, so a falsy value is the row not rendering at
        # all. `or "#"` above is the opposite case — a row that always renders
        # and needs somewhere harmless to point until its app merges.
        "workspace_settings_url": workspace_settings_url,
        "members_url": members_url,
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
