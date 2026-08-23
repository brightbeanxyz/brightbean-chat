"""The shared media resolution path (``apps.channels.media``).

Nothing here stubs :func:`apps.common.outbound.guarded_request`. The guard is
the reason this module exists rather than each adapter fetching for itself, so
these tests replace DNS and the socket with ``tests.ssrf.FakeInternet`` and leave
the guard entirely real: a stubbed guard proves nothing about
SECURITY-BASELINE §6. The External Request node's suite drives the same harness,
which is why it lives beside ``guard_required`` rather than in either app.

The adapter is a fake whose ``media_source`` returns a canned answer with no
network of its own. That is deliberate: the guard belongs to the *shared* half,
so the proof that it is in the path must not depend on which adapter is
plugged in. Telegram's own half is ``test_telegram_media.py``.
"""

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from apps.channels.media import (
    MEDIA_DOWNLOAD_TIMEOUT,
    MEDIA_RESOLVE_TIMEOUT,
    MediaSource,
    MediaUnavailableError,
    fetch_media,
    media_response,
)
from apps.channels.tests.fake_adapter import FakeAdapter, fake_adapter_for, swapped_adapter, unregistered
from apps.common.platforms import Platform
from tests.ssrf import FakeInternet, deployment_cache_cleared, guard_required, serving

MEDIA_HOST = "media.example.test"
MEDIA_URL = f"https://{MEDIA_HOST}/RC123/Media/ME456"

# Real magic bytes, because the module under test decides by reading them.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 56
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PDF = b"%PDF-1.7\n" + b"\x00" * 55
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!doctype html><html><body><script>alert(1)</script></body></html>"
GARBAGE = b"\x07\x08\x09 not a format anyone recognises, padded out" + b"\x00" * 20


@pytest.fixture(autouse=True)
def _clear_deployment_cache() -> Iterator[None]:
    """The guard caches its own host's addresses; these tests swap the resolver."""
    with deployment_cache_cleared():
        yield


def fake_internet(handler: Any, names: dict[str, list[str]] | None = None) -> FakeInternet:
    """FakeInternet pointed at this module's one media host by default."""
    return FakeInternet(handler, names if names is not None else {MEDIA_HOST: [FakeInternet.PUBLIC]})


class MediaAdapter(FakeAdapter):
    """A fake whose ``media_source`` answers without touching the network."""

    #: Class-level, like ``FakeAdapter.sends``: the registry hands out a fresh
    #: instance per call, so an instance attribute would never be read back.
    source: MediaSource | None = MediaSource(url=MEDIA_URL)
    asked: list[str] = []

    def media_source(self, connection: Any, media_id: str) -> MediaSource | None:
        type(self).asked.append(media_id)
        return type(self).source


@pytest.fixture
def media_adapter() -> Iterator[type[MediaAdapter]]:
    """``MediaAdapter`` in Telegram's slot, with its own fresh log."""
    adapter_cls = type("MediaAdapterTelegram", (MediaAdapter,), {"platform": Platform.TELEGRAM, "asked": []})
    with swapped_adapter(Platform.TELEGRAM, adapter_cls):
        yield adapter_cls


@pytest.mark.django_db
class TestTheGuardIsInThePath:
    """SECURITY-BASELINE §6: "new call sites add a test proving the guard"."""

    def test_the_download_goes_through_the_ssrf_guard(self, connection, media_adapter, monkeypatch):
        internet = fake_internet(serving(PNG)).install(monkeypatch)

        with guard_required() as guarded:
            fetch_media(connection, "ME456")

        assert len(guarded) == 1
        assert internet.requests[0].url.host == FakeInternet.PUBLIC, "pinned to the address the guard checked"
        assert internet.requests[0].headers["host"] == MEDIA_HOST

    def test_a_source_pointing_at_the_metadata_service_never_leaves_the_process(
        self, connection, media_adapter, monkeypatch
    ):
        """The reason a webhook-supplied media URL is not `request_json`'s case."""
        internet = fake_internet(serving(PNG), names={MEDIA_HOST: ["169.254.169.254"]}).install(monkeypatch)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

        assert internet.requests == []

    def test_credentials_travel_as_headers(self, connection, media_adapter, monkeypatch):
        """Twilio's shape: Basic auth, not userinfo — which the guard refuses."""
        media_adapter.source = MediaSource(url=MEDIA_URL, headers=(("Authorization", "Basic QUM6dG9r"),))
        internet = fake_internet(serving(JPEG)).install(monkeypatch)

        fetch_media(connection, "ME456")

        assert internet.requests[0].headers["authorization"] == "Basic QUM6dG9r"


@pytest.mark.django_db
class TestWhatItServes:
    """SECURITY-BASELINE §9, applied to bytes a stranger sent."""

    def test_an_image_is_served_inline_under_its_sniffed_type(self, connection, media_adapter, monkeypatch):
        fake_internet(serving(PNG)).install(monkeypatch)

        response = media_response(fetch_media(connection, "ME456"))

        assert response["Content-Type"] == "image/png"
        assert response["Content-Disposition"].startswith("inline;")
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_the_platforms_declared_content_type_is_ignored(self, connection, media_adapter, monkeypatch):
        """The whole of "sniffing, not trusting", in one case.

        A stranger sends markup and labels it ``image/png``. Believing the label
        would put an attacker-authored document on this deployment's own origin
        with an ``inline`` disposition, which is the stored-XSS shape §9 exists
        to forbid.
        """
        fake_internet(serving(HTML, headers={"Content-Type": "image/png"})).install(monkeypatch)

        response = media_response(fetch_media(connection, "ME456"))

        assert response["Content-Type"] == "application/octet-stream"
        assert response["Content-Disposition"].startswith("attachment;")
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_svg_is_never_inline(self, connection, media_adapter, monkeypatch):
        fake_internet(serving(SVG)).install(monkeypatch)

        response = media_response(fetch_media(connection, "ME456"))

        assert response["Content-Type"] == "application/octet-stream"
        assert response["Content-Disposition"].startswith("attachment;")

    def test_a_pdf_keeps_its_type_and_is_still_an_attachment(self, connection, media_adapter, monkeypatch):
        fake_internet(serving(PDF)).install(monkeypatch)

        response = media_response(fetch_media(connection, "ME456"))

        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"].startswith("attachment;")

    def test_unrecognised_bytes_are_offered_as_a_download_rather_than_hidden(
        self, connection, media_adapter, monkeypatch
    ):
        fake_internet(serving(GARBAGE)).install(monkeypatch)

        resolved = fetch_media(connection, "ME456")

        assert resolved.content == GARBAGE
        assert resolved.mime == "application/octet-stream"
        assert resolved.inline is False

    def test_the_filename_is_this_deployments_and_not_the_identifiers(self, connection, media_adapter, monkeypatch):
        """A media id reaches a response header only if something puts it there."""
        fake_internet(serving(JPEG)).install(monkeypatch)

        response = media_response(fetch_media(connection, 'ME"; drop'))

        assert "drop" not in response["Content-Disposition"]
        assert "attachment.jpg" in response["Content-Disposition"]

    def test_the_response_is_privately_cacheable(self, connection, media_adapter, monkeypatch):
        """One workspace's contact's picture must not sit in a shared cache."""
        fake_internet(serving(PNG)).install(monkeypatch)

        response = media_response(fetch_media(connection, "ME456"))

        assert response["Cache-Control"] == "private, max-age=3600"


@pytest.mark.django_db
class TestWhenItCannot:
    """Every failure is one exception type, so no caller can branch on why."""

    def test_a_platform_that_refuses(self, connection, media_adapter, monkeypatch):
        fake_internet(serving(b"nope", status=401)).install(monkeypatch)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

    def test_a_body_over_the_cap(self, connection, media_adapter, monkeypatch, settings):
        """Truncated, not served: half a JPEG is not a picture.

        Driven through the setting rather than by generating sixteen megabytes,
        which also asserts the thing the cap exists for — that an operator who
        lowers it is actually obeyed. Passing ``max_bytes=`` to the guard
        overrides ``EXTERNAL_REQUEST_MAX_RESPONSE_BYTES``, so a hard-coded
        number here would be a limit nobody could turn down.
        """
        settings.INBOUND_MEDIA_MAX_BYTES = 128
        fake_internet(serving(PNG + b"\x00" * 512)).install(monkeypatch)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

    def test_a_body_under_a_raised_cap_is_served(self, connection, media_adapter, monkeypatch, settings):
        """The other direction, so the test above cannot pass by always failing."""
        settings.INBOUND_MEDIA_MAX_BYTES = 4096
        fake_internet(serving(PNG + b"\x00" * 512)).install(monkeypatch)

        assert fetch_media(connection, "ME456").mime == "image/png"

    def test_an_unparseable_cap_falls_back_rather_than_raising(self, connection, media_adapter, monkeypatch, settings):
        settings.INBOUND_MEDIA_MAX_BYTES = "not a number"
        fake_internet(serving(PNG)).install(monkeypatch)

        assert fetch_media(connection, "ME456").mime == "image/png"

    def test_an_empty_body(self, connection, media_adapter, monkeypatch):
        fake_internet(serving(b"")).install(monkeypatch)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

    def test_an_adapter_that_cannot_resolve_this_id(self, connection, media_adapter, monkeypatch):
        media_adapter.source = None
        internet = fake_internet(serving(PNG)).install(monkeypatch)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

        assert internet.requests == []

    def test_an_adapter_with_no_media_support_at_all(self, connection, monkeypatch):
        """The base class's default. Most platforms never fill ``media_ids``."""
        plain = fake_adapter_for(Platform.TELEGRAM)
        with swapped_adapter(Platform.TELEGRAM, plain), pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

    def test_a_platform_with_no_adapter(self, connection):
        with unregistered(Platform.TELEGRAM), pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

    @pytest.mark.parametrize("url", ["", "not a url at all", "file:///etc/passwd", "ftp://x.test/a.jpg"])
    def test_a_source_the_guard_will_not_request(self, connection, media_adapter, url):
        """An adapter that builds a bad URL is a 404, not a 500: every one of
        these is a ``BlockedURLError``, which is an ``OutboundError``."""
        media_adapter.source = MediaSource(url=url)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

    @pytest.mark.parametrize("media_id", ["", "   ", None, 42, ["ME456"]])
    def test_an_identifier_that_is_not_one(self, connection, media_adapter, media_id):
        """``body`` is jsonb: its shape is a claim, not a guarantee."""
        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, media_id)

        assert media_adapter.asked == [], "never handed to an adapter"

    @pytest.mark.parametrize("status", [401, 403, 404, 500])
    def test_this_module_logs_the_host_and_never_the_url(self, connection, media_adapter, monkeypatch, caplog, status):
        """SECURITY-BASELINE §5. One platform puts its credential *in* the URL.

        Scoped to this module's own records on purpose. httpx logs a request
        line of its own carrying the full URL, which is a project-wide fact
        already handled by ``apps.common.logging``'s scrubber — and asserting
        against it here would be testing the scrubber rather than this module.
        That the two together hold for a real token is
        ``test_telegram_media.py::TestTheTokenStaysOutOfTheLogs``, which uses a
        token of the shape the scrubber recognises.
        """
        secret_path = "/RC123/Media/ME456"
        media_adapter.source = MediaSource(url=f"https://{MEDIA_HOST}{secret_path}")
        fake_internet(serving(b"nope", status=status)).install(monkeypatch)

        with caplog.at_level(logging.DEBUG), pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

        ours = "\n".join(record.getMessage() for record in caplog.records if record.name == "apps.channels.media")
        assert ours, "the failure is logged at all"
        assert MEDIA_HOST in ours
        assert secret_path not in ours


@pytest.mark.django_db
class TestAnAdapterThatMisbehaves:
    """``media_source`` is a plugin point, and a plugin point is not a promise.

    Its docstring says "return None rather than raising", but the caller of a
    documented contract still owns what happens when it is broken — and what
    used to happen was a 500 from the inbox's most exposed endpoint.
    """

    @pytest.mark.parametrize(
        "boom",
        [
            ValueError("credentials could not be decrypted"),
            RuntimeError("something a platform SDK does"),
            KeyError("config"),
            TypeError("wrong shape"),
        ],
    )
    def test_a_raising_adapter_is_still_only_unavailable(self, connection, media_adapter, monkeypatch, boom):
        def _raise(self: Any, _connection: Any, _media_id: str) -> MediaSource | None:
            raise boom

        monkeypatch.setattr(media_adapter, "media_source", _raise)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

    def test_the_failure_names_the_exception_type_and_not_its_message(
        self, connection, media_adapter, monkeypatch, caplog
    ):
        """A platform's error text quotes the request that caused it, token and
        all (SECURITY-BASELINE §5) — so the type is logged and the text is not."""

        def _raise(self: Any, _connection: Any, _media_id: str) -> MediaSource | None:
            raise RuntimeError("GET https://api.telegram.org/file/bot123:SHOULD-NOT-APPEAR/x.jpg failed")

        monkeypatch.setattr(media_adapter, "media_source", _raise)

        with caplog.at_level(logging.DEBUG), pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

        ours = "\n".join(record.getMessage() for record in caplog.records if record.name == "apps.channels.media")
        assert "RuntimeError" in ours
        assert "SHOULD-NOT-APPEAR" not in ours

    def test_a_keyboard_interrupt_is_not_swallowed(self, connection, media_adapter, monkeypatch):
        """``except Exception`` and not ``except BaseException``: a worker being
        shut down should shut down, not report a missing attachment."""

        def _raise(self: Any, _connection: Any, _media_id: str) -> MediaSource | None:
            raise KeyboardInterrupt

        monkeypatch.setattr(media_adapter, "media_source", _raise)

        with pytest.raises(KeyboardInterrupt):
            fetch_media(connection, "ME456")


class TestTheTimeBudget:
    """The deployment ships four request slots and a 30 s worker timeout.

    ``Procfile`` and ``Dockerfile`` both run ``gunicorn --workers 2 --threads 2``
    with no ``--timeout``. A media request that can outlive gunicorn's default
    is not slow, it is a SIGKILL that takes its worker's other requests with it,
    so the two halves of the budget are asserted together rather than left as a
    sum nobody recomputes when one of them changes.
    """

    #: gunicorn's default ``--timeout``, restated because neither the Procfile
    #: nor the Dockerfile passes one.
    WORKER_TIMEOUT = 30.0

    def test_the_whole_resolution_fits_inside_a_worker_timeout(self):
        assert MEDIA_RESOLVE_TIMEOUT + MEDIA_DOWNLOAD_TIMEOUT < self.WORKER_TIMEOUT

    def test_it_fits_with_room_to_spare(self):
        """Not merely under it: a request that lands at 29 s has still ruined
        the worker's throughput for everyone else sharing it."""
        assert MEDIA_RESOLVE_TIMEOUT + MEDIA_DOWNLOAD_TIMEOUT <= self.WORKER_TIMEOUT / 2

    def test_the_resolve_step_is_not_the_background_budget(self):
        """The bug this replaced: ``BACKGROUND_TIMEOUT`` is 30 s on its own."""
        from apps.channels.providers.base import BACKGROUND_TIMEOUT

        assert MEDIA_RESOLVE_TIMEOUT < BACKGROUND_TIMEOUT


@pytest.mark.django_db
class TestMediaSourceIsReallyFrozen:
    def test_it_can_be_hashed(self):
        """A frozen dataclass generates ``__hash__`` from its fields, so one
        holding a dict claims to be hashable and raises the first time anything
        actually hashes it."""
        assert len({MediaSource(url=MEDIA_URL), MediaSource(url=MEDIA_URL)}) == 1

    def test_headers_survive_a_round_trip_to_the_guard(self, connection, media_adapter, monkeypatch):
        media_adapter.source = MediaSource(url=MEDIA_URL, headers=(("X-A", "1"), ("X-B", "2")))
        internet = fake_internet(serving(PNG)).install(monkeypatch)

        fetch_media(connection, "ME456")

        assert internet.requests[0].headers["x-a"] == "1"
        assert internet.requests[0].headers["x-b"] == "2"


@pytest.mark.django_db
class TestAnAdapterThatReturnsNonsense:
    """The other half of "an adapter is not a promise".

    Raising is contained at the call; returning something malformed is not
    caught there at all — it detonates lines later, inside the guard, on an
    ``AttributeError`` or ``TypeError`` nothing is catching. Same 500, longer
    route, and the more likely of the two: a frozen dataclass does not enforce
    its annotations, so ``MediaSource(url=..., headers={...})`` is built happily
    and only fails when the guard tries to iterate it.
    """

    @pytest.mark.parametrize(
        "returned",
        [
            {"url": "https://media.example.test/x.jpg"},
            "https://media.example.test/x.jpg",
            42,
            object(),
            [("url", "https://media.example.test/x.jpg")],
        ],
    )
    def test_a_value_that_is_not_a_media_source(self, connection, media_adapter, monkeypatch, returned):
        monkeypatch.setattr(media_adapter, "media_source", lambda self, _c, _m: returned)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

    @pytest.mark.parametrize("url", [None, 42, b"https://x.test/a.jpg", "", "   "])
    def test_a_media_source_with_no_usable_url(self, connection, media_adapter, monkeypatch, url):
        media_adapter.source = MediaSource(url=url)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

    @pytest.mark.parametrize("headers", [42, object(), ["not-a-pair"], [("a", "b", "c")], "Authorization: x"])
    def test_headers_that_are_not_name_value_pairs(self, connection, media_adapter, monkeypatch, headers):
        """These reach ``_clean_headers`` and raise there, where nothing catches."""
        media_adapter.source = MediaSource(url=MEDIA_URL, headers=headers)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

    def test_a_mapping_of_headers_is_normalised_rather_than_refused(self, connection, media_adapter, monkeypatch):
        """A dict is the shape an adapter author reaches for first, and the
        guard itself accepts either — refusing it would turn a stylistic
        mismatch into a missing picture."""
        media_adapter.source = MediaSource(url=MEDIA_URL, headers={"Authorization": "Basic QUM6dG9r"})
        internet = fake_internet(serving(PNG)).install(monkeypatch)

        assert fetch_media(connection, "ME456").mime == "image/png"
        assert internet.requests[0].headers["authorization"] == "Basic QUM6dG9r"

    def test_a_malformed_source_never_reaches_the_network(self, connection, media_adapter, monkeypatch):
        media_adapter.source = MediaSource(url=MEDIA_URL, headers=42)
        internet = fake_internet(serving(PNG)).install(monkeypatch)

        with pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

        assert internet.requests == []

    def test_the_failure_is_logged_as_this_deployments_bug(self, connection, media_adapter, monkeypatch, caplog):
        """Unlike a platform saying no, this is our own code being wrong, and
        the reader's 404 is the only other trace it would leave."""
        monkeypatch.setattr(media_adapter, "media_source", lambda self, _c, _m: {"url": MEDIA_URL})

        with caplog.at_level(logging.DEBUG), pytest.raises(MediaUnavailableError):
            fetch_media(connection, "ME456")

        errors = [r for r in caplog.records if r.name == "apps.channels.media" and r.levelno >= logging.ERROR]
        assert errors, "a malformed adapter return is an error, not an info"
