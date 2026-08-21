"""The app shell: base.html, the toast host, the layouts and the error pages."""

import re
from pathlib import Path

import pytest

NONCE_ATTR_RE = re.compile(r'nonce="([A-Za-z0-9+/=]+)"')
INLINE_SCRIPT_RE = re.compile(r"<(script|style)(?![^>]*\bsrc=)([^>]*)>", re.I)

SHELL_URLS = ["/", "/ui/", "/dashboard/", "/inbox/", "/settings/profile/", "/settings/workspace/tags/"]


@pytest.fixture
def member(client, django_user_model):
    """A signed-in user, so base.html renders the shell branch.

    Uses get_user_model() indirectly via pytest-django's fixture, so this keeps
    working when issue #31 swaps in a custom AUTH_USER_MODEL.
    """
    user = django_user_model.objects.create_user(username="member", password="pw-for-tests-only")
    client.force_login(user)
    return user


@pytest.mark.django_db
class TestPublicRoot:
    """CI boots the compose stack from a checkout with no .env and runs
    `curl -fsS / | grep -q "BrightBean Chat"`. curl -fsS follows no redirect and
    fails on any non-2xx, so all three properties below are load-bearing."""

    def test_root_is_reachable_without_a_session(self, client):
        response = client.get("/")

        assert response.status_code == 200

    def test_root_carries_the_product_name(self, client):
        assert b"BrightBean Chat" in client.get("/").content

    def test_root_does_not_redirect_to_a_login(self, client):
        assert client.get("/").status_code not in (301, 302, 303, 307, 308)

    def test_anonymous_root_uses_the_auth_layout_not_the_shell(self, client):
        body = client.get("/").content.decode()

        assert "auth-card" in body
        assert "<aside" not in body
        assert 'class="mb-1 sidebar-nav-item' not in body


@pytest.mark.django_db
class TestContentSecurityPolicy:
    def test_every_inline_script_and_style_carries_a_nonce(self, client, member):
        """SECURITY-BASELINE §8. script-src has no 'unsafe-inline', so an inline
        block without a nonce is silently dead in the browser."""
        for url in SHELL_URLS:
            body = client.get(url).content.decode()
            for tag, attrs in INLINE_SCRIPT_RE.findall(body):
                assert "nonce=" in attrs, f"<{tag}> without a nonce on {url}: {attrs[:120]}"

    def test_the_anonymous_root_still_has_a_nonced_inline_script(self, client):
        """The toast host sits outside the authenticated/anonymous branch, which
        is what keeps a nonce on every page including the landing page."""
        body = client.get("/").content.decode()

        assert NONCE_ATTR_RE.search(body)

    def test_no_inline_event_handler_attributes_anywhere(self, client, member):
        """That is what the CSP-safe hover utility classes exist for."""
        for url in SHELL_URLS:
            body = client.get(url).content.decode().lower()
            for handler in ["onclick=", "onload=", "onerror=", "onmouseover=", "onsubmit=", "onchange="]:
                assert handler not in body, f"{handler} on {url}"

    @pytest.mark.parametrize("origin", ["jsdelivr", "unpkg", "cdnjs", "fonts.googleapis", "//cdn."])
    def test_no_cdn_origin_survives_in_any_rendered_page(self, client, member, origin):
        """Deviation 6. Includes HTML comments — a note explaining that a CDN was
        removed still puts that hostname in the response."""
        for url in [*SHELL_URLS, "/no-such-page"]:
            assert origin not in client.get(url).content.decode(), f"{origin} on {url}"

    def test_every_script_src_is_same_origin(self, client, member):
        for url in SHELL_URLS:
            for src in re.findall(r'<script[^>]*\bsrc="([^"]+)"', client.get(url).content.decode()):
                assert src.startswith("/static/"), src


@pytest.mark.django_db
class TestSidebarCollapse:
    """The no-flash mechanism only works if all three layers ship together."""

    def test_the_pre_paint_script_stamps_the_html_element(self, client, member):
        body = client.get("/dashboard/").content.decode()

        assert "localStorage.getItem('sidebarCollapsed')" in body
        assert "classList.add('sidebar-is-collapsed')" in body

    def test_the_css_mirrors_the_collapsed_state_before_alpine_boots(self, client, member):
        body = client.get("/dashboard/").content.decode()

        assert ".sidebar-initial" in body
        assert "html.sidebar-is-collapsed .sidebar-initial" in body
        assert "[x-cloak]" in body

    def test_alpine_persists_the_state_and_takes_over_the_classes(self, client, member):
        body = client.get("/dashboard/").content.decode()

        assert "x-effect=\"localStorage.setItem('sidebarCollapsed', sidebarCollapsed)\"" in body
        assert "classList.remove('sidebar-initial')" in body
        assert "documentElement.classList.remove('sidebar-is-collapsed')" in body

    def test_the_aside_ships_with_the_pre_alpine_class(self, client, member):
        body = client.get("/dashboard/").content.decode()
        aside = body[body.index("<aside") : body.index("</aside>")]

        assert "sidebar-initial" in aside

    def test_the_pre_paint_css_targets_only_classes_the_shell_renders(self, client, member):
        """The three layers have to stay in step.

        A `.sidebar-initial .sidebar-foo` rule whose `sidebar-foo` no longer
        exists in the markup silently stops mirroring that collapsed state, and
        the only symptom is a flash of the expanded sidebar on reload — which no
        assertion about a page's text would ever catch.

        Both settings and app pages are sampled because some of these classes
        are conditional: the section-label wrapper only appears on a nav with
        group headings, and the badge only when a count is non-zero.
        """
        pages = [client.get(url).content.decode() for url in ["/dashboard/", "/settings/profile/"]]

        head_style = pages[0][pages[0].index("<style") : pages[0].index("</style>")]
        targeted = set(re.findall(r"\.(sidebar-[a-z-]+)", head_style))
        assert targeted, "the pre-paint block vanished"

        # Only real class attributes count. Searching the raw HTML would match
        # the stylesheet's own rules and make this test vacuous.
        rendered_classes: set[str] = set()
        for page in pages:
            without_style = re.sub(r"<style.*?</style>", "", page, flags=re.S)
            for attr in re.findall(r'class="([^"]*)"', without_style):
                rendered_classes.update(attr.split())
        # Added to <html> by the pre-paint script rather than by an attribute.
        rendered_classes.add("sidebar-is-collapsed")
        # Renders only when a nav item carries a non-zero count, and every count
        # is 0 until issue #14 (L4-D) supplies the unread inbox number. The
        # markup exists — test_the_badge_markup_exists_for_a_non_zero_count
        # below proves it — so the collapsed-state rule is correct, not orphaned.
        rendered_classes.add("sidebar-badge")

        missing = sorted(targeted - rendered_classes)
        assert not missing, f"styled for the collapsed state but never rendered: {missing}"

    def test_no_element_combines_x_show_with_a_display_none_utility(self):
        """`class="hidden" x-show="..."` is a trap that cannot be seen in a
        rendered page: Alpine shows an element by clearing its inline display,
        after which the utility's own display:none reasserts itself and the
        element stays invisible forever. Here it would have meant a collapsed
        sidebar with no way to expand it again.
        """
        html = (Path(__file__).parents[3] / "templates" / "base.html").read_text()

        offenders = []
        for tag in re.findall(r"<[a-z]+\s[^>]*x-show=[^>]*>", html, re.S):
            classes = re.search(r'class="([^"]*)"', tag)
            # Token-exact: a responsive variant like `lg:hidden` is fine and
            # deliberate — the mobile backdrop must stay hidden on desktop
            # whatever Alpine thinks. Only an unconditional `hidden` is a trap.
            if classes and "hidden" in classes.group(1).split():
                offenders.append(tag)

        assert not offenders, f"x-show on an element that a utility class keeps hidden: {offenders}"

    def test_the_footer_halves_are_not_cloaked(self):
        """x-cloak on either half would blank the footer until Alpine boots —
        the exact flash the pre-paint block exists to prevent. They are mirrored
        by CSS instead."""
        html = (Path(__file__).parents[3] / "templates" / "base.html").read_text()

        for half in ["sidebar-org-expanded", "sidebar-org-collapsed"]:
            tag = re.search(rf"<div class=\"{half}[^>]*>", html)
            assert tag, half
            assert "x-cloak" not in tag.group(0), f"{half} is cloaked"

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
                                },
                            ],
                        }
                    ]
                }
            )
        )

        assert "sidebar-badge" not in html


@pytest.mark.django_db
class TestToastHost:
    def test_the_host_is_on_every_page_with_no_per_page_include(self, client, member):
        """Deviation 2. Studio's host is a partial each template must remember."""
        for url in [*SHELL_URLS]:
            assert 'id="bb-toast-host"' in client.get(url).content.decode(), url

    def test_the_host_is_present_for_anonymous_visitors_too(self, client):
        assert 'id="bb-toast-host"' in client.get("/").content.decode()

    def test_it_listens_for_both_hx_trigger_toasts_and_htmx_errors(self, client, member):
        body = client.get("/dashboard/").content.decode()

        assert "addEventListener('showToast'" in body
        assert "addEventListener('htmx:responseError'" in body

    def test_server_text_is_written_with_textcontent_only(self, client, member):
        """SECURITY-BASELINE §2: toast bodies carry platform-supplied content."""
        body = client.get("/dashboard/").content.decode()

        assert ".textContent = detail.title" in body
        assert "innerHTML = detail" not in body

    def test_error_bodies_are_parsed_inertly(self, client, member):
        body = client.get("/dashboard/").content.decode()

        assert "DOMParser()" in body

    def test_the_opt_outs_survive_the_merge(self, client, member):
        body = client.get("/dashboard/").content.decode()

        assert "data-no-error-toast" in body
        assert "data-inline-error" in body

    def test_init_is_idempotent_because_htmx_reruns_swapped_in_scripts(self, client, member):
        assert "__bbToastInit" in client.get("/dashboard/").content.decode()

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
    def test_htmx_requests_get_the_token_injected(self, client, member):
        body = client.get("/dashboard/").content.decode()

        assert "htmx:configRequest" in body
        assert "X-CSRFToken" in body
        assert "csrfmiddlewaretoken" in body


@pytest.mark.django_db
class TestStyleGuide:
    def test_it_renders_the_shell_without_a_session(self, client):
        """There is no way to log in until issue #31 merges, and a design system
        nobody can open is a design system nobody reviews."""
        body = client.get("/ui/").content.decode()

        assert "sidebar-nav-item" in body

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
class TestSettingsLayouts:
    def test_the_settings_layout_replaces_the_nav_wholesale(self, client, member):
        """Studio's convention, kept: a settings section is a sidebar_nav
        override, not a tab bar or a second column."""
        body = client.get("/settings/profile/").content.decode()

        assert "Account" in body and "Organization" in body
        assert "Broadcasts" not in body

    def test_the_two_settings_layouts_carry_different_scopes(self, client, member):
        """Not cosmetic: once issue #31 lands RBAC an Editor reaches workspace
        settings without being able to see org settings, so one shared nav would
        advertise pages the viewer cannot open."""
        account = client.get("/settings/profile/").content.decode()
        workspace = client.get("/settings/workspace/tags/").content.decode()

        assert "Team Members" in account
        assert "Team Members" not in workspace
        assert "Tags" in workspace
        assert "Tags" not in account

    def test_no_view_supplied_settings_active_string_is_needed(self, client, member):
        """Deviation 4: the layouts read the same nav structure the main nav
        does. Studio needs 11 views to each remember a `settings_active` key."""
        body = client.get("/settings/preferences/").content.decode()

        assert 'sidebar-nav-item active"' in body


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
