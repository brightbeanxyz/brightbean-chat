"""The conditional-GET helper behind SPEC §14's 3-second polling.

Unit-level, because the inbox's own suite exercises it end to end and this is
where the HTTP-spec details get pinned: weak comparison, the header being a
list, and ``*``.
"""

from typing import Any

from django.http import HttpResponse
from django.test import RequestFactory

from apps.common.polling import conditional, if_none_match, version_etag


def _request(header: str | None = None) -> Any:
    headers = {"If-None-Match": header} if header is not None else {}
    return RequestFactory().get("/", headers=headers)


class TestTheToken:
    def test_it_is_weak(self) -> None:
        """These responses are semantically equivalent, not byte-identical: a
        relative timestamp inside the markup moves on its own."""
        assert version_etag("a", 1).startswith('W/"')

    def test_the_same_parts_give_the_same_tag(self) -> None:
        assert version_etag("a", 1, None) == version_etag("a", 1, None)

    def test_different_parts_give_different_tags(self) -> None:
        assert version_etag("a", 1) != version_etag("a", 2)

    def test_none_and_the_string_none_are_not_the_same_version(self) -> None:
        """An empty workspace's Max(updated_at) is None, and "None" is a value a
        query string can carry — so joining by str() alone would collide."""
        assert version_etag(None) != version_etag("None")

    def test_parts_cannot_be_confused_by_concatenation(self) -> None:
        """("ab", "c") and ("a", "bc") must not hash alike, or two filter states
        could share a tag."""
        assert version_etag("ab", "c") != version_etag("a", "bc")


class TestIfNoneMatch:
    def test_no_header_is_not_a_match(self) -> None:
        assert if_none_match(_request(), 'W/"abc"') is False

    def test_the_same_tag_matches(self) -> None:
        assert if_none_match(_request('W/"abc"'), 'W/"abc"') is True

    def test_comparison_is_weak(self) -> None:
        """RFC 9110 §13.1.2: If-None-Match uses the weak comparison function, so
        a proxy that strips the W/ prefix must not force a full re-render."""
        assert if_none_match(_request('"abc"'), 'W/"abc"') is True

    def test_the_header_is_a_list(self) -> None:
        assert if_none_match(_request('W/"old", W/"abc"'), 'W/"abc"') is True

    def test_a_star_matches_anything(self) -> None:
        assert if_none_match(_request("*"), 'W/"abc"') is True

    def test_a_different_tag_does_not_match(self) -> None:
        assert if_none_match(_request('W/"other"'), 'W/"abc"') is False


class TestConditional:
    def test_a_match_is_a_304_with_no_body(self) -> None:
        response = conditional(_request('W/"abc"'), 'W/"abc"', lambda: HttpResponse("rendered"))

        assert response.status_code == 304
        assert response.content == b""

    def test_a_match_never_renders(self) -> None:
        """The callable is the point: at one request every three seconds per
        open tab, an unchanged poll must not build the template it would throw
        away."""
        rendered = False

        def build() -> HttpResponse:
            nonlocal rendered
            rendered = True
            return HttpResponse("rendered")

        conditional(_request('W/"abc"'), 'W/"abc"', build)

        assert rendered is False

    def test_a_miss_renders_and_carries_the_tag(self) -> None:
        response = conditional(_request('W/"stale"'), 'W/"abc"', lambda: HttpResponse("rendered"))

        assert response.status_code == 200
        assert response.content == b"rendered"
        assert response.headers["ETag"] == 'W/"abc"'

    def test_both_answers_refuse_the_browser_cache(self) -> None:
        """Revalidation here is driven by JavaScript holding the last tag. With
        the HTTP cache also in play, a 304 on the wire comes back to the caller
        as a 200 from cache and the "did anything change?" answer is lost."""
        hit = conditional(_request('W/"abc"'), 'W/"abc"', lambda: HttpResponse("x"))
        miss = conditional(_request(), 'W/"abc"', lambda: HttpResponse("x"))

        assert hit.headers["Cache-Control"] == "no-store"
        assert miss.headers["Cache-Control"] == "no-store"
