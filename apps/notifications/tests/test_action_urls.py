"""The action_url allowlist.

``action_url`` is the one context key that becomes a *link* rather than text,
which is why escaping is not enough for it — escaping does nothing to a scheme.
The policy is enforced once, at write time, in ``notify()``.
"""

import pytest
from django.test import override_settings

from apps.notifications.action_urls import absolute, safe_path
from apps.notifications.engine import notify
from apps.notifications.mail import _action_url

APP = "https://chat.example.test"


class TestWhatSurvives:
    def test_a_root_relative_path_is_kept(self):
        assert safe_path("/w/abc/flows/1") == "/w/abc/flows/1"

    def test_a_query_string_and_fragment_survive(self):
        assert safe_path("/w/abc/inbox/?state=open#msg-3") == "/w/abc/inbox/?state=open#msg-3"

    @override_settings(APP_URL=APP)
    def test_an_absolute_url_on_this_deployment_is_reduced_to_its_path(self):
        """A caller building f"{APP_URL}/w/..." is doing something reasonable
        and should not silently lose its link."""
        assert safe_path(f"{APP}/w/abc/flows/1") == "/w/abc/flows/1"

    @override_settings(APP_URL=APP)
    def test_the_bare_base_url_becomes_the_root_path(self):
        assert safe_path(APP) == "/"

    @override_settings(APP_URL=f"{APP}/")
    def test_a_trailing_slash_on_app_url_does_not_break_matching(self):
        assert safe_path(f"{APP}/w/abc") == "/w/abc"


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "raw",
        [
            "javascript:alert(document.cookie)",
            "JavaScript:alert(1)",
            "  javascript:alert(1)  ",
            "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
            "mailto:someone@example.test",
            "vbscript:msgbox(1)",
        ],
    )
    def test_scheme_bearing_values_are_dropped(self, raw):
        assert safe_path(raw) is None

    @pytest.mark.parametrize("raw", ["//evil.test/login", "//evil.test", "/\\evil.test/login"])
    def test_protocol_relative_values_are_dropped(self, raw):
        """These pass a naive startswith("/") check and still leave the origin:
        "//host" is protocol-relative, and browsers normalise the backslash in
        "/\\host" to a slash while parsing the authority."""
        assert safe_path(raw) is None

    @pytest.mark.parametrize("raw", ["/\t/evil.test", "/\n/evil.test", "/\r/evil.test", "/\t\\evil.test"])
    def test_control_characters_are_dropped_before_they_can_be_stripped(self, raw):
        """Browsers strip TAB, LF and CR from a URL *before* parsing its origin,
        so "/\\t/evil.test" is read as "//evil.test". A check that runs on the
        raw string without removing these first can be walked straight past."""
        assert safe_path(raw) is None

    @override_settings(APP_URL=APP)
    def test_an_off_site_absolute_url_is_dropped(self):
        assert safe_path("https://evil.test/login") is None

    @override_settings(APP_URL=APP)
    def test_a_lookalike_domain_does_not_prefix_match(self):
        """Without the trailing slash in the prefix test, "https://chat.example.test"
        would also match "https://chat.example.test.evil.test/x"."""
        assert safe_path(f"{APP}.evil.test/login") is None
        assert safe_path(f"{APP}evil.test/login") is None

    @pytest.mark.parametrize("raw", [None, 42, {"a": 1}, ["/x"], "", "   ", "w/abc/flows/1"])
    def test_junk_and_relative_values_are_dropped(self, raw):
        assert safe_path(raw) is None


class TestAbsolute:
    @override_settings(APP_URL=APP)
    def test_it_builds_an_absolute_url_from_a_path(self):
        assert absolute("/w/abc") == f"{APP}/w/abc"

    @override_settings(APP_URL=APP)
    def test_nothing_safe_falls_back_to_a_route_we_control(self):
        """No database: reverse() is pure URL resolution, and the fallback has
        to work on a path that never touched a row."""
        assert absolute(None) == f"{APP}/notifications/"


@pytest.mark.django_db
class TestTheWritePathEnforcesIt:
    """The point of doing this in notify() rather than per template: every
    render site inherits it, including ones later layers add."""

    def test_a_safe_action_url_is_stored(self, tenancy):
        created = notify(
            tenancy.workspace,
            "flow_loop_cap_hit",
            users=[tenancy.owner],
            context={"flow_name": "Welcome", "action_url": "/w/abc/flows/1"},
        )

        assert created[0].payload["action_url"] == "/w/abc/flows/1"

    @pytest.mark.parametrize("hostile", ["javascript:alert(1)", "https://evil.test/login", "//evil.test"])
    def test_a_hostile_action_url_is_not_stored_at_all(self, tenancy, hostile):
        created = notify(
            tenancy.workspace,
            "flow_loop_cap_hit",
            users=[tenancy.owner],
            context={"flow_name": "Welcome", "action_url": hostile},
        )

        assert "action_url" not in created[0].payload

    def test_the_bell_falls_back_to_a_route_we_control(self, tenancy, client_for):
        """Dropping the key must not leave a dead or empty href."""
        from django.urls import reverse

        notify(
            tenancy.workspace,
            "flow_loop_cap_hit",
            users=[tenancy.owner],
            context={"flow_name": "Welcome", "action_url": "javascript:alert(1)"},
        )

        body = client_for(tenancy.owner).get(reverse("notifications:bell")).content.decode()

        assert "javascript:" not in body
        assert 'href=""' not in body
        assert f'href="{reverse("notifications:list")}"' in body


@pytest.mark.django_db
class TestTheEmailRechecksToo:
    """notify() sanitises on the way in, but a row edited in the admin — or
    written before this rule existed — must not put an off-site link in an
    email carrying this product's branding."""

    @override_settings(APP_URL=APP)
    def test_a_hostile_stored_value_is_refused_at_send_time(self, tenancy):
        notification = notify(
            tenancy.workspace, "flow_loop_cap_hit", users=[tenancy.owner], context={"flow_name": "W"}
        )[0]
        notification.payload["action_url"] = "https://evil.test/login"
        notification.save(update_fields=["payload"])

        assert _action_url(notification) == f"{APP}/notifications/"

    @override_settings(APP_URL=APP)
    def test_a_safe_stored_value_is_made_absolute(self, tenancy):
        notification = notify(
            tenancy.workspace,
            "flow_loop_cap_hit",
            users=[tenancy.owner],
            context={"flow_name": "W", "action_url": "/w/abc/flows/1"},
        )[0]

        assert _action_url(notification) == f"{APP}/w/abc/flows/1"
