"""Shared validators."""

import pytest
from django.core.exceptions import ValidationError

from apps.common.validators import is_renderable_url, is_valid_hex_color, validate_hex_color

VALID = ["#3B82F6", "#000000", "#ffffff", "#AbCdEf"]
INVALID = ["3B82F6", "#3B82F", "#3B82F6A", "#GGGGGG", "red", "#3b82f6 ", 0x3B82F6]


@pytest.mark.parametrize("value", VALID)
def test_valid_colors_pass(value):
    validate_hex_color(value)
    assert is_valid_hex_color(value)


@pytest.mark.parametrize("value", INVALID)
def test_invalid_colors_fail(value):
    with pytest.raises(ValidationError):
        validate_hex_color(value)
    assert not is_valid_hex_color(value)


@pytest.mark.parametrize("value", ["", None])
def test_empty_means_no_override(value):
    """Empty and None pass so that "no colour override" keeps working."""
    validate_hex_color(value)
    assert is_valid_hex_color(value)


class TestIsRenderableUrl:
    """May a platform-supplied string become an href or a src? (baseline §2)

    The inbox's hostile-content suite exercises this through rendered pages;
    here it is pinned directly, including the property that matters most about
    it — that it is a *rendering* check and does not pretend to be an SSRF
    guard.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.example.test/a.png",
            "http://cdn.example.test/a.png",
            "HTTPS://CDN.EXAMPLE.TEST/A.PNG",
            "https://cdn.example.test:8443/a.png",
            "https://cdn.example.test/a.png?token=abc#frag",
            "https://[2001:db8::1]/a.png",
        ],
    )
    def test_ordinary_web_addresses_pass(self, url):
        """It has to let real attachments through, or it is a content filter
        with extra steps."""
        assert is_renderable_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JaVaScRiPt:alert(1)",
            "  javascript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "ftp://example.test/a.png",
        ],
    )
    def test_schemes_that_execute_or_impersonate_are_refused(self, url):
        assert is_renderable_url(url) is False

    def test_a_scheme_relative_url_is_refused(self):
        """It inherits whatever scheme the page was served over, and reads as a
        path to anyone skimming the stored value."""
        assert is_renderable_url("//evil.test/steal") is False

    def test_a_relative_path_is_refused(self):
        assert is_renderable_url("/media/a.png") is False

    def test_a_scheme_with_no_host_is_refused(self):
        assert is_renderable_url("https://") is False

    @pytest.mark.parametrize("value", ["", "   ", None, 42, [], {}, b"https://x.test/a"])
    def test_anything_that_is_not_a_usable_string_is_refused(self, value):
        """An adapter should never emit these, and "should never" is exactly the
        assumption a defensive layer does not get to make."""
        assert is_renderable_url(value) is False

    @pytest.mark.parametrize(
        "url",
        [
            "https://x.test/a\nSet-Cookie: evil=1",
            "https://x.test/\r\n",
            "https://x\x00.test/",
            "java\tscript:alert(1)",
        ],
    )
    def test_control_characters_are_refused(self, url):
        """Browsers drop tab and newline from a URL before parsing it, so a
        value carrying one parses as something other than what is stored."""
        assert is_renderable_url(url) is False

    def test_a_malformed_port_is_a_refusal_not_an_exception(self):
        """urlsplit raises ValueError on this, and a render path must not."""
        assert is_renderable_url("https://example.test:notaport/a.png") is False

    def test_it_does_not_claim_to_be_an_ssrf_guard(self):
        """SECURITY-BASELINE §6 puts the one shared SSRF guard in issue #15 and
        forbids server-side fetches of user-supplied URLs until it lands. A
        future reader reaching for "the URL checker" must not find a weaker
        lookalike with nothing to warn them."""
        doc = is_renderable_url.__doc__ or ""

        assert "not an SSRF guard" in doc
        assert "#15" in doc

    def test_a_private_address_passes_because_that_is_not_its_job(self):
        """Rendering a link to localhost is harmless; *fetching* one is the
        thing issue #15 exists to stop."""
        assert is_renderable_url("http://127.0.0.1:8000/a.png") is True
