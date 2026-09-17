"""The app shell: base.html, the toast host, the layouts and the error pages."""

import re
from pathlib import Path

import pytest

NONCE_ATTR_RE = re.compile(r'nonce="([A-Za-z0-9+/=]+)"')
INLINE_SCRIPT_RE = re.compile(r"<(script|style)(?![^>]*\bsrc=)([^>]*)>", re.I)


@pytest.fixture
def tenant_client(tenancy, client_for):
    """A signed-in owner with a workspace, so base.html renders the shell.

    Uses issue #31's `tenancy` fixture rather than building a user here: the
    shell's nav is workspace-scoped now, so a user without an organization
    renders a shell with no navigation at all.
    """
    return client_for(tenancy.owner)


@pytest.fixture
def shell_url(tenancy):
    """One representative shell page: the workspace dashboard."""
    return f"/w/{tenancy.workspace.id}/"


@pytest.fixture
def shell_urls(tenancy):
    """Pages that render the full shell, across both settings layouts."""
    ws = tenancy.workspace.id
    return [
        "/ui/",
        f"/w/{ws}/",
        f"/w/{ws}/inbox/",
        "/accounts/settings/",
        "/organization/settings/",
        f"/w/{ws}/settings/tags/",
    ]


@pytest.mark.django_db
class TestPublicEntryPoint:
    """CI boots the compose stack from a checkout with no .env and runs
    `curl -fsSL / | grep -q "BrightBean Chat"`.

    Issue #31 made `/` a router that sends anonymous visitors to the login
    page, and added `-L` so the assertion follows that redirect. So the string
    has to survive on the login page — which this workstream restyles — rather
    than on a landing page of its own.
    """

    def test_the_root_sends_anonymous_visitors_to_login(self, client):
        response = client.get("/")

        assert response.status_code == 302
        assert response.headers["Location"] == "/accounts/login/"

    def test_the_login_page_carries_the_product_name(self, client):
        """What CI's grep actually lands on after following the redirect."""
        assert b"BrightBean Chat" in client.get("/accounts/login/").content

    def test_following_the_redirect_reaches_a_200(self, client):
        """`curl -fsSL` fails on a non-2xx even after following."""
        assert client.get("/", follow=True).status_code == 200

    def test_the_login_page_uses_the_auth_layout_not_the_shell(self, client):
        body = client.get("/accounts/login/").content.decode()

        assert "auth-card" in body
        assert "<aside" not in body


@pytest.mark.django_db
class TestContentSecurityPolicy:
    def test_every_inline_script_and_style_carries_a_nonce(self, tenant_client, shell_urls):
        """SECURITY-BASELINE §8. script-src has no 'unsafe-inline', so an inline
        block without a nonce is silently dead in the browser."""
        for url in shell_urls:
            body = tenant_client.get(url).content.decode()
            for tag, attrs in INLINE_SCRIPT_RE.findall(body):
                assert "nonce=" in attrs, f"<{tag}> without a nonce on {url}: {attrs[:120]}"

    def test_the_anonymous_login_page_still_has_a_nonced_inline_script(self, client):
        """The toast host sits outside the authenticated/anonymous branch, which
        is what keeps a nonce on every page including the login page — and what
        test_csp.py's nonce assertion lands on."""
        body = client.get("/accounts/login/").content.decode()

        assert NONCE_ATTR_RE.search(body)

    def test_no_inline_event_handler_attributes_anywhere(self, tenant_client, shell_urls):
        """That is what the CSP-safe hover utility classes exist for."""
        for url in shell_urls:
            body = tenant_client.get(url).content.decode().lower()
            for handler in ["onclick=", "onload=", "onerror=", "onmouseover=", "onsubmit=", "onchange="]:
                assert handler not in body, f"{handler} on {url}"

    @pytest.mark.parametrize("origin", ["jsdelivr", "unpkg", "cdnjs", "fonts.googleapis", "//cdn."])
    def test_no_cdn_origin_survives_in_any_rendered_page(self, tenant_client, shell_urls, origin):
        """Deviation 6. Includes HTML comments — a note explaining that a CDN was
        removed still puts that hostname in the response."""
        for url in [*shell_urls, "/no-such-page"]:
            assert origin not in tenant_client.get(url).content.decode(), f"{origin} on {url}"

    def test_every_script_src_is_same_origin(self, tenant_client, shell_urls):
        for url in shell_urls:
            for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', tenant_client.get(url).content.decode()):
                assert src.startswith("/static/"), src


@pytest.mark.django_db
class TestTheRail:
    """The rail replaced a collapsible sidebar, and with it a whole mechanism.

    The old sidebar could start a page at either of two widths, so three pieces
    had to ship together to stop the wrong one flashing: a pre-paint script
    reading localStorage, a .sidebar-initial block mirroring every collapsed
    rule in plain CSS, and an x-init handover on <aside>. The rail is a fixed
    76px with nothing persisted, so all three were removed rather than reduced.
    These tests pin that they stay gone — a half-restored mechanism is worse
    than either state.
    """

    def test_nothing_persists_or_restores_a_sidebar_width(self, tenant_client, shell_urls, shell_url):
        body = tenant_client.get(shell_url).content.decode()

        for ghost in [
            "sidebarCollapsed",
            "sidebar-is-collapsed",
            "sidebar-initial",
            "sidebar-collapsed",
        ]:
            assert ghost not in body, f"{ghost} came back without the rest of the mechanism"

    def test_the_cloak_rule_survives_on_every_page(self, tenant_client, shell_urls, shell_url, client):
        """The one line of the old pre-paint <style> that is still load-bearing.

        The workspace switcher, the user menu and the bell panel are all Alpine
        dropdowns, and the auth templates use Alpine too. Without this rule every
        one of them renders open on first paint — which is why it is asserted on
        an anonymous page as well as a signed-in one.
        """
        for body in [
            tenant_client.get(shell_url).content.decode(),
            client.get("/accounts/login/").content.decode(),
        ]:
            assert "[x-cloak]" in body

    def test_the_rail_renders_its_rows(self, tenant_client, shell_urls, shell_url):
        body = tenant_client.get(shell_url).content.decode()
        rail = body[body.index("<aside") : body.index("</aside>")]

        assert "rail-item" in rail
        # Labels track the design, keys track the route — see MAIN_NAV.
        for label in ["Home", "Inbox", "Flows", "Broadcasts", "Contacts"]:
            assert f">{label}</span>" in rail, label

    def test_sequences_and_notifications_left_the_rail(self, tenant_client, shell_urls, shell_url):
        """Both moved rather than vanished: Sequences is a tab on the flows
        page, Notifications is the header bell. A row for either here would be
        a second way to reach one destination."""
        body = tenant_client.get(shell_url).content.decode()
        rail = body[body.index("<aside") : body.index("</aside>")]

        assert ">Sequences</span>" not in rail
        assert ">Notifications</span>" not in rail

    def test_the_rail_survives_a_missing_optional_app(self, tenant_client, shell_urls, shell_url):
        """apps.analytics is an optional install, so the Insights row reverses
        to "#" and _render_nav drops it. The rail must not assume six items —
        it is laid out with flex for exactly this."""
        body = tenant_client.get(shell_url).content.decode()
        rail = body[body.index("<aside") : body.index("</aside>")]

        assert 'href="#"' not in rail

    def test_no_element_combines_x_show_with_a_display_none_utility(self):
        """`class="hidden" x-show="..."` is a trap that cannot be seen in a
        rendered page: Alpine shows an element by clearing its inline display,
        after which the utility's own display:none reasserts itself and the
        element stays invisible forever.

        The shell's dropdowns moved into partials, so those are read too — the
        moment this stopped covering them would be the moment new dropdowns
        were added.
        """
        root = Path(__file__).parents[3] / "templates"
        sources = [root / "base.html", *sorted((root / "partials").glob("_app_*.html"))]

        offenders = []
        for path in sources:
            html = path.read_text()
            for tag in re.findall(r"<[a-z]+\s[^>]*x-show=[^>]*>", html, re.S):
                classes = re.search(r'class="([^"]*)"', tag)
                # Token-exact: a responsive variant like `lg:hidden` is fine.
                # Only an unconditional `hidden` is a trap.
                if classes and "hidden" in classes.group(1).split():
                    offenders.append(f"{path.name}: {tag}")

        assert not offenders, f"x-show on an element that a utility class keeps hidden: {offenders}"


@pytest.mark.django_db
class TestTheNavBadge:
    def test_the_badge_markup_exists_for_a_non_zero_count(self):
        """Pairs with the allowance above: the class is unreachable today only
        because every badge count is 0, not because nothing renders it."""
        from django.template import Context, Template

        html = Template('{% include "partials/_nav_groups.html" %}').render(
            Context(
                {
                    "groups": [
                        {
                            "label": "",
                            "items": [
                                {
                                    "key": "inbox",
                                    "label": "Inbox",
                                    "icon": "inbox",
                                    "url": "/inbox/",
                                    "active": False,
                                    "badge": 7,
                                    "badge_slot": True,
                                },
                            ],
                        }
                    ]
                }
            )
        )

        assert 'class="sidebar-badge"' in html
        assert ">7<" in html

    def test_a_zero_badge_renders_nothing_rather_than_a_zero(self):
        from django.template import Context, Template

        html = Template('{% include "partials/_nav_groups.html" %}').render(
            Context(
                {
                    "groups": [
                        {
                            "label": "",
                            "items": [
                                {
                                    "key": "inbox",
                                    "label": "Inbox",
                                    "icon": "inbox",
                                    "url": "/inbox/",
                                    "active": False,
                                    "badge": 0,
                                    "badge_slot": True,
                                },
                            ],
                        }
                    ]
                }
            )
        )

        assert "sidebar-badge" not in html
        assert ">0<" not in html

    def test_a_zero_badge_still_renders_the_slot_an_out_of_band_swap_aims_at(self):
        """The other half of the contract above, and the reason the badge is
        `hidden` rather than absent: the owning app keeps its count live by id
        — notifications polls one every 60s — and htmx logs
        `htmx:oobErrorNoTarget` for a swap whose target is not in the document.
        A zero badge has to show nothing while still *being* somewhere.
        """
        from django.template import Context, Template

        html = Template('{% include "partials/_nav_groups.html" %}').render(
            Context(
                {
                    "groups": [
                        {
                            "label": "",
                            "items": [
                                {
                                    "key": "inbox",
                                    "label": "Inbox",
                                    "icon": "inbox",
                                    "url": "/inbox/",
                                    "active": False,
                                    "badge": 0,
                                    "badge_slot": True,
                                },
                            ],
                        }
                    ]
                }
            )
        )

        slot = re.search(r'<span id="nav-badge-inbox"[^>]*>(.*?)</span>', html)
        # Scoped to the slot: _nav_icon.html gives every row an
        # `aria-hidden="true"` svg, so `"hidden" in html` is true of any nav at
        # all and would pass with the attribute deleted.
        assert slot, "no slot for the swap to land on"
        assert slot.group(1) == ""
        assert "hidden" in slot.group(0)

    def test_a_row_no_app_badges_gets_no_slot_at_all(self):
        """An id nothing ever swaps is dead weight on every page, and
        partials/_nav_badge_sinks.html has to mirror whatever this loop
        renders for the layouts that replace the nav wholesale — two lists
        worth keeping to the rows an app actually owns."""
        from django.template import Context, Template

        html = Template('{% include "partials/_nav_groups.html" %}').render(
            Context(
                {
                    "groups": [
                        {
                            "label": "",
                            "items": [
                                {
                                    "key": "contacts",
                                    "label": "Contacts",
                                    "icon": "contacts",
                                    "url": "/contacts/",
                                    "active": False,
                                    "badge": 0,
                                    "badge_slot": False,
                                },
                            ],
                        }
                    ]
                }
            )
        )

        assert "nav-badge-" not in html


@pytest.mark.django_db
class TestTheWorkspaceSettingsAreReachable:
    """The concern the old sidebar dropdown existed for, against the new shell.

    Before #130 the nine workspace-settings pages were reachable only by typing
    a URL, and the fix then was a "Workspace settings" link in the sidebar's
    workspace switcher. The redesign removed that sidebar: the rail's Settings
    row lands on the account settings page, and the settings nav is one list
    gated per row rather than two filtered ones. That is a different answer to
    the same question, so the guard moves rather than going away — what must
    stay true is that the pages have *an* entry point that is not the URL bar.
    """

    def test_the_settings_page_reaches_the_workspace_section(self, tenancy, client_for):
        from django.urls import reverse

        html = client_for(tenancy.owner).get(reverse("accounts:settings")).content.decode()

        assert f'href="/w/{tenancy.workspace.id}/settings/channels/"' in html
        assert "Channels" in html

    def test_a_role_without_the_key_is_not_offered_the_row(self, tenancy, client_for):
        """Gated per row on the key its own view enforces, so a control never
        renders for somebody the page behind it would refuse."""
        from django.urls import reverse

        html = client_for(tenancy.user_for("viewer")).get(reverse("accounts:settings")).content.decode()

        assert f'href="/w/{tenancy.workspace.id}/settings/channels/"' not in html

    def test_the_glyph_sizes_are_pinned_so_an_ambient_context_var_cannot_move_them(self, tenant_client, shell_url):
        """`partials/_nav_icon.html` takes an optional `icon_size`, and
        `{% include %}` without `only` inherits the whole parent context — so a
        view adding a `size` key would silently resize every glyph in the shell
        if the parameter were named `size`. It is not, deliberately; this pins
        the default so a change to `default:18` cannot pass unnoticed.
        """
        html = tenant_client.get(shell_url).content.decode()

        assert 'class="flex-shrink-0" width="18" height="18"' in html


@pytest.mark.django_db
class TestToastHost:
    def test_the_host_is_on_every_page_with_no_per_page_include(self, tenant_client, shell_urls):
        """Deviation 2. Studio's host is a partial each template must remember."""
        for url in shell_urls:
            assert 'id="bb-toast-host"' in tenant_client.get(url).content.decode(), url

    def test_the_host_is_present_for_anonymous_visitors_too(self, client):
        assert 'id="bb-toast-host"' in client.get("/accounts/login/").content.decode()

    def test_it_listens_for_both_hx_trigger_toasts_and_htmx_errors(self, tenant_client, shell_urls, shell_url):
        body = tenant_client.get(shell_url).content.decode()

        assert "addEventListener('showToast'" in body
        assert "addEventListener('htmx:responseError'" in body

    def test_server_text_is_written_with_textcontent_only(self, tenant_client, shell_urls, shell_url):
        """SECURITY-BASELINE §2: toast bodies carry platform-supplied content."""
        body = tenant_client.get(shell_url).content.decode()

        assert ".textContent = detail.title" in body
        assert "innerHTML = detail" not in body

    def test_error_bodies_are_parsed_inertly(self, tenant_client, shell_urls, shell_url):
        body = tenant_client.get(shell_url).content.decode()

        assert "DOMParser()" in body

    def test_a_flattened_error_page_is_not_shown_verbatim(self, tenant_client, shell_urls, shell_url):
        """Under DEBUG a Django technical 500 page flattens to the exception and
        its traceback. Studio's handler renders whatever it extracts."""
        body = tenant_client.get(shell_url).content.decode()

        assert "message.length > 300" in body
        assert "Something went wrong. Please try again." in body

    def test_the_opt_outs_survive_the_merge(self, tenant_client, shell_urls, shell_url):
        body = tenant_client.get(shell_url).content.decode()

        assert "data-no-error-toast" in body
        assert "data-inline-error" in body

    def test_init_is_idempotent_because_htmx_reruns_swapped_in_scripts(self, tenant_client, shell_urls, shell_url):
        assert "__bbToastInit" in tenant_client.get(shell_url).content.decode()

    def test_the_host_is_not_itself_a_live_region(self, tenant_client, shell_urls, shell_url):
        """Each toast carries its own role, which IS a live region. Announcing
        the host as well made a screen reader read every toast twice."""
        body = tenant_client.get(shell_url).content.decode()

        assert '<div id="bb-toast-host"></div>' in body

    def test_errors_interrupt_and_quiet_tones_do_not(self, tenant_client, shell_urls, shell_url):
        """Politeness per tone is the reason the roles live on the toasts
        rather than as one aria-live on the container."""
        body = tenant_client.get(shell_url).content.decode()

        assert "tone === 'error' ? 'alert' : 'status'" in body

    def test_a_view_fires_a_toast_over_hx_trigger(self, client):
        import json

        response = client.post("/ui/toast/", {"tone": "warn"})

        assert response.status_code == 204
        assert json.loads(response.headers["HX-Trigger"])["showToast"]["tone"] == "warn"

    def test_an_unknown_tone_falls_back_rather_than_rendering_unstyled(self, client):
        import json

        response = client.post("/ui/toast/", {"tone": "../../etc/passwd"})

        assert json.loads(response.headers["HX-Trigger"])["showToast"]["tone"] == "info"

    def test_the_toast_endpoint_rejects_get(self, client):
        assert client.get("/ui/toast/").status_code == 405


@pytest.mark.django_db
class TestCsrfWiring:
    def test_htmx_requests_get_the_token_injected(self, tenant_client, shell_urls, shell_url):
        body = tenant_client.get(shell_url).content.decode()

        assert "htmx:configRequest" in body
        assert "X-CSRFToken" in body
        assert "csrfmiddlewaretoken" in body

    def test_the_token_is_gated_on_a_same_origin_check(self, tenant_client, shell_urls, shell_url):
        """htmx will issue a cross-origin request happily, and an unconditional
        header hands the session's CSRF token to whatever host an hx-* points
        at. From Layer 3 this template renders platform-supplied content
        (SECURITY-BASELINE §2), so the guard belongs here once rather than in
        every surface that later learns to display it.

        Behaviour was verified in a browser by dispatching htmx:configRequest
        against same-origin, cross-origin, protocol-relative, other-port and
        unparseable targets; only the same-origin ones received the header.
        """
        body = tenant_client.get(shell_url).content.decode()

        assert "target.origin !== window.location.origin" in body
        # The check has to come before the header is set, or it guards nothing.
        assert body.index("target.origin !==") < body.index("headers['X-CSRFToken']")


@pytest.mark.django_db
class TestStyleGuide:
    def test_it_renders_the_shell_without_a_session(self, client):
        """There is no way to log in until issue #31 merges, and a design system
        nobody can open is a design system nobody reviews."""
        body = client.get("/ui/").content.decode()

        assert "rail-item" in body

    def test_it_exercises_ui_select_outside_the_page_it_was_written_for(self, client):
        body = client.get("/ui/").content.decode()

        assert "bb-filter-select" in body
        assert "getBoundingClientRect()" in body

    def test_it_shows_every_platform_icon_and_the_fallback(self, client):
        body = client.get("/ui/").content.decode()

        for platform in ["telegram", "instagram", "messenger", "whatsapp", "sms", "email"]:
            assert f"pi-{platform}" in body
        assert "carrier-pigeon" in body

    def test_it_exercises_every_alert_tone_including_the_added_warning(self, client):
        body = client.get("/ui/").content.decode()

        for tone in ["success", "info", "warning", "error"]:
            assert f"alert-{tone}" in body


@pytest.mark.django_db
class TestPlatformIcon:
    def _render(self, platform, size="sm"):
        from django.template import Context, Template

        return Template('{% include "partials/_platform_icon.html" with platform=platform size=size %}').render(
            Context({"platform": platform, "size": size})
        )

    @pytest.mark.parametrize("platform", ["telegram", "instagram", "messenger", "whatsapp", "sms", "email"])
    def test_the_six_chat_platforms_render(self, platform):
        assert "<svg" in self._render(platform)

    def test_an_unknown_key_renders_the_fallback_rather_than_nothing(self):
        """Studio's second copy has no {% else %} and emits an empty slot."""
        assert "<svg" in self._render("myspace")

    def test_icons_inherit_colour_so_a_caller_can_tint_them(self):
        """Deviation 3: a channel appears on a white row, a coloured chip and in
        a dropdown — brand-hex-only serves only the first."""
        assert 'fill="currentColor"' in self._render("telegram")

    @pytest.mark.parametrize(("size", "px"), [("sm", "16"), ("md", "20")])
    def test_the_named_size_api(self, size, px):
        assert f'width="{px}"' in self._render("telegram", size=size)

    def test_size_defaults_to_sm(self):
        from django.template import Context, Template

        html = Template('{% include "partials/_platform_icon.html" with platform="sms" %}').render(Context())
        assert 'width="16"' in html


@pytest.mark.django_db
class TestLogoSizing:
    def _render(self, **ctx):
        from django.template import Context, Template

        return Template('{% include "partials/_logo.html" with size=size only %}').render(Context(ctx))

    def test_the_small_variant_uses_a_component_class_not_a_utility(self):
        """Everything in styles.css is unlayered and Tailwind's utilities live
        in @layer utilities, so unlayered wins: `w-7 h-7` next to
        .sidebar-logo-mark was silently ignored and size="sm" did nothing."""
        html = self._render(size="sm")

        assert "sidebar-logo-mark-sm" in html
        assert "w-7" not in html

    def test_the_default_variant_carries_no_modifier(self):
        assert "sidebar-logo-mark-sm" not in self._render(size="md")

    def test_the_modifier_exists_in_the_compiled_stylesheet(self):
        """A class the template emits and the bundle never defines is the same
        no-op in a different place."""
        from django.contrib.staticfiles import finders

        bundle = Path(finders.find("css/dist/styles.css")).read_text()

        assert ".sidebar-logo-mark-sm{" in bundle

    def test_ambient_context_cannot_resize_the_mark(self):
        """The include passes `only`; without it a stray `size` in the page
        context would silently change the logo."""
        from django.template import Context, Template

        html = Template('{% include "partials/_logo.html" only %}').render(Context({"size": "sm"}))

        assert "sidebar-logo-mark-sm" not in html


@pytest.mark.django_db
class TestSettingsLayouts:
    def test_the_settings_nav_sits_beside_the_rail_rather_than_replacing_it(self, tenant_client, shell_urls):
        """The redesign's structural change, and the reason the badge sinks
        could be deleted.

        The settings layouts used to override {% block sidebar_nav %} and swap
        the product's whole navigation out, which took the bell's out-of-band
        badge targets with it and left its poll aiming at nothing — every
        settings page logged htmx:oobErrorNoTarget once a minute. A second
        column leaves the rail in place, so no archetype strands an id.
        """
        body = tenant_client.get("/accounts/settings/").content.decode()

        assert 'class="setnav"' in body
        # The rail is still there, with the rest of the product on it.
        assert "rail-item" in body
        assert ">Broadcasts</span>" in body
        # And so there is nothing to go "back" to.
        assert "Back to app" not in body

    def test_both_settings_layouts_render_one_filtered_nav(self, tenant_client, shell_urls, tenancy):
        """There used to be two group lists, so an Editor on workspace settings
        would not be shown organisation rows.

        The intent was right and the mechanism was too coarse both ways. It hid
        every Workspace row from the account settings page, which is where the
        rail's Settings row lands, so Channels, Tags, Labels and five more had
        no entry point in the product at all. And inside a group it filtered
        nothing, so the Editor still saw rows they would be refused at. Each row
        is now gated on the key its own view is gated on, so one list is both
        complete and honest.
        """
        account = tenant_client.get("/accounts/settings/").content.decode()
        workspace = tenant_client.get(f"/w/{tenancy.workspace.id}/settings/tags/").content.decode()

        for body in (account, workspace):
            assert "People &amp; roles" in body
            assert ">Tags</span>" in body
            assert ">Channels</span>" in body
            assert "Profile" in body

    def test_no_view_supplied_settings_active_string_is_needed(self, tenant_client, shell_urls):
        """Deviation 4: the layouts read the same nav structure the rail does.
        Studio needs 11 views to each remember a `settings_active` key.

        Asks /accounts/settings/ rather than /accounts/preferences/: the
        Notifications row was retired with the placeholder it pointed at, so
        nothing in the nav is active on that route any more. The contract under
        test is unchanged — a settings page highlights its own row without its
        view saying which one.
        """
        body = tenant_client.get("/accounts/settings/").content.decode()

        assert 'setnav-item active"' in body


class TestTemplateHygiene:
    def test_no_short_comment_spans_more_than_one_line(self):
        """Django's `{# #}` is single-line only. A multi-line one is not a
        comment at all — it renders as visible text on the page.

        This has now bitten the project twice: issue #31's base.html carries a
        note about a three-line `{# #}` that showed up in the sidebar, and this
        workstream did the same thing in a note about cascade layers, which
        appeared verbatim above the team-members table. `{% comment %}` is the
        multi-line form.
        """
        offenders = []
        for path in (Path(__file__).parents[3] / "templates").rglob("*.html"):
            src = path.read_text()
            for match in re.finditer(r"\{#", src):
                rest = src[match.start() :]
                close = rest.find("#}")
                if close == -1 or "\n" in rest[:close]:
                    line = src[: match.start()].count("\n") + 1
                    offenders.append(f"{path.name}:{line}")

        assert not offenders, f"multi-line {{# #}} renders as text: {offenders}"

    @pytest.mark.django_db
    def test_no_rendered_page_leaks_a_comment(self, tenant_client, shell_urls):
        """The symptom the rule above prevents, checked on real responses.

        Only comment syntax: the style guide at /ui/ legitimately displays
        `{% templatetag openblock %} ui_select ...` as documentation, so a blanket
        ban on `{%` would fail on a page doing exactly what it should.
        """
        for url in shell_urls:
            body = tenant_client.get(url).content.decode()
            for token in ["{#", "#}", "{% comment %}", "{% endcomment %}"]:
                assert token not in body, f"{token!r} leaked into {url}"


@pytest.mark.django_db
class TestStaticReferences:
    def test_every_static_reference_in_a_template_actually_exists(self):
        """The Docker image runs collectstatic under ManifestStaticFilesStorage,
        which hard-fails on a {% static %} path it cannot resolve — so a typo
        here breaks the image build, not the test suite. Catch it in the fast
        job instead of the slow one.

        This also covers the build wiring: css/dist/styles.css only resolves
        because `theme` is an installed app and `npm run build:css` has run.
        """
        from django.contrib.staticfiles import finders

        templates = Path(__file__).parents[3] / "templates"
        refs = set()
        for path in templates.rglob("*.html"):
            refs |= set(re.findall(r"\{%\s*static\s+'([^']+)'", path.read_text()))

        assert refs, "no {% static %} references found — did the shell disappear?"
        missing = sorted(ref for ref in refs if finders.find(ref) is None)
        assert not missing, f"referenced but not found by any static finder: {missing}"

    def test_the_compiled_stylesheet_is_the_one_the_theme_app_serves(self):
        """theme/ exists only to put the Tailwind output on the app-directories
        finder. If it were dropped from INSTALLED_APPS this would be the symptom."""
        from django.contrib.staticfiles import finders

        found = finders.find("css/dist/styles.css")

        assert found, "the Tailwind bundle is missing — run `npm run build:css`"
        assert "theme/static" in found.replace("\\", "/")


class TestTailwindSourceCoverage:
    """styles.css imports Tailwind with `source(none)`, which turns off Tailwind
    4's automatic content detection and makes the @source directives the entire
    content list.

    That was done deliberately — auto-detection scanned the whole repo and emitted
    a rule for any file that merely contained a word matching a utility name, so
    `blur` arrived from the DOM event in the minified alpine bundle and `isolate`
    from a Makefile comment. A stylesheet that changes when someone edits a
    Makefile is not reproducible.

    The cost of that choice is this class. With auto-detection on, a template in a
    new location still got its classes; with it off, the template renders unstyled
    and nothing fails — not the suite, which only checks that {% static %} paths
    resolve, and not the audit job's determinism check, which compares two builds
    of the same input and so is blind to an under-inclusive source list by
    construction. The failure only shows up in a browser. Hence a test.
    """

    CSS = Path(__file__).parents[3] / "theme" / "static_src" / "src" / "styles.css"

    def _globs(self):
        text = self.CSS.read_text()
        assert "source(none)" in text, (
            "styles.css no longer imports Tailwind with source(none). If automatic "
            "source detection is back on, this class is obsolete — but so is the "
            "reproducibility it was protecting; see the class docstring."
        )
        patterns = re.findall(r'@source\s+"([^"]+)"', text)
        assert patterns, "styles.css declares no @source globs, so it can emit nothing"
        return patterns

    def test_every_template_lives_under_an_at_source_glob(self):
        """A template Tailwind never reads still renders — just with no styles."""
        root = Path(__file__).parents[3]

        covered = set()
        for pattern in self._globs():
            covered |= {p.resolve() for p in self.CSS.parent.glob(pattern)}

        # Anything Django's loaders would find: the DIRS entry plus, because
        # APP_DIRS is on, every apps/*/templates tree.
        present = {p.resolve() for p in (root / "templates").rglob("*.html")}
        present |= {p.resolve() for p in root.glob("apps/*/templates/**/*.html")}

        assert present, "no templates found at all — did the shell move?"
        unscanned = sorted(str(p.relative_to(root)) for p in present - covered)
        assert not unscanned, (
            "these templates are outside every @source glob in styles.css, so "
            f"their classes are missing from the bundle: {unscanned}. Add an "
            "@source directive covering them, and if the new path is outside "
            "templates/ also add it to the frontend stage's COPY in the Dockerfile."
        )

    def test_no_python_file_emits_a_class_attribute(self):
        """The @source globs cover templates only. A form widget or a
        MESSAGE_TAGS mapping that starts naming CSS classes in Python would be a
        content source nothing scans."""
        root = Path(__file__).parents[3]
        pattern = re.compile(r"""["']class["']\s*:|\bclass=["']""")

        offenders = []
        for directory in ("apps", "config", "theme"):
            for path in (root / directory).rglob("*.py"):
                if "/tests/" in path.as_posix() or "/migrations/" in path.as_posix():
                    continue
                for line in path.read_text().splitlines():
                    # config's LOGGING maps "class" to dotted handler paths, which
                    # are not CSS and are not rendered into any page.
                    if pattern.search(line) and "logging." not in line:
                        offenders.append(f"{path.relative_to(root)}: {line.strip()}")

        assert not offenders, (
            "CSS classes generated in Python are invisible to Tailwind's @source "
            f"globs and will not be emitted: {offenders}. Move them into a "
            "template, or add the file to the @source list in styles.css."
        )


@pytest.mark.django_db
class TestErrorPages:
    def test_404_keeps_its_literal_heading(self, client):
        response = client.get("/no-such-page")

        assert response.status_code == 404
        assert b"404 Not Found" in response.content

    @pytest.mark.parametrize("name", ["403.html", "404.html", "500.html"])
    def test_error_templates_render_standalone(self, name):
        """django.views.defaults.server_error renders 500.html with a bare
        Context() — no context processors, no `request`. Rendering with an empty
        context here is exactly that path, and it must not raise."""
        from django.template.loader import get_template

        html = get_template(name).render({})

        assert "BrightBean Chat" in html

    @pytest.mark.parametrize(
        ("name", "heading"),
        [("403.html", "403 Forbidden"), ("404.html", "404 Not Found"), ("500.html", "500 Server Error")],
    )
    def test_each_error_page_keeps_its_heading(self, name, heading):
        from django.template.loader import get_template

        assert heading in get_template(name).render({})

    def test_error_pages_are_styled_but_reference_no_request(self, client):
        """A nonce would render empty on the 500 path, so there is no inline
        script to need one."""
        from django.template.loader import get_template

        html = get_template("500.html").render({})

        assert "css/dist/styles.css" in html
        assert "<script" not in html
