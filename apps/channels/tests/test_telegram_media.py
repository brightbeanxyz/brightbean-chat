"""Telegram's half of media resolution: ``file_id`` → a download URL.

The shared half — the guard, the sniff, the disposition — is
``test_media.py``. What is Telegram's own is that a ``file_id`` is not a URL
until ``getFile`` says so, that the path it answers with is somebody else's
string, and that the URL built from it carries the bot token.
"""

import logging
from typing import Any

import pytest

from apps.channels.media import fetch_media
from apps.channels.models import ChannelConnection
from apps.channels.providers import telegram
from apps.channels.providers.telegram import TelegramAdapter, file_download_url, store_bot_token
from apps.channels.tests.telegram_support import BOT_TOKEN, Reply, fake_bot_api
from tests.ssrf import FakeInternet, deployment_cache_cleared, guard_required, serving

pytestmark = pytest.mark.django_db

SECRET_HALF = BOT_TOKEN.split(":", 1)[1]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 56


@pytest.fixture
def telegram_connection(connection: ChannelConnection) -> ChannelConnection:
    store_bot_token(connection, BOT_TOKEN)
    connection.save(update_fields=["credentials", "updated_at"])
    return connection


def resolving(path: Any) -> Any:
    """A fake Bot API whose ``getFile`` answers with ``path``."""
    return lambda fake: fake.reply("getFile", Reply(result={"file_id": "AgAC", "file_path": path}))


class TestResolvingAFileId:
    def test_it_asks_getfile_and_builds_the_download_url(self, telegram_connection: ChannelConnection) -> None:
        with fake_bot_api(resolving("photos/file_42.jpg")) as fake:
            source = TelegramAdapter().media_source(telegram_connection, "AgAC-largest")

        assert fake.payloads("getFile") == [{"file_id": "AgAC-largest"}]
        assert source is not None
        assert source.url == f"https://api.telegram.org/file/bot{BOT_TOKEN}/photos/file_42.jpg"
        assert source.headers == (), "the token is in the path; there is nothing to send"

    def test_a_path_needing_encoding_is_encoded_rather_than_refused(
        self, telegram_connection: ChannelConnection
    ) -> None:
        with fake_bot_api(resolving("voice/note 1.oga")):
            source = TelegramAdapter().media_source(telegram_connection, "AgAC")

        assert source is not None
        assert source.url.endswith("/voice/note%201.oga")

    def test_the_url_is_what_the_shared_fetch_would_receive(self) -> None:
        """The one function that puts a credential in a URL, kept honest."""
        assert file_download_url("123:abc", "a/b.jpg") == "https://api.telegram.org/file/bot123:abc/a/b.jpg"


class TestAPathItWillNotBuildAUrlFrom:
    """``file_path`` comes back from the platform, so it is somebody else's string."""

    @pytest.mark.parametrize(
        "path",
        [
            "/etc/passwd",
            "../../bot999:stolen/getUpdates",
            "photos/../../../getMe",
            "https://evil.example.test/x.jpg",
            "photos/x.jpg?redirect=https://evil.example.test",
            "photos/x.jpg#frag",
            "",
        ],
    )
    def test_it_is_refused(self, telegram_connection: ChannelConnection, path: str) -> None:
        with fake_bot_api(resolving(path)):
            assert TelegramAdapter().media_source(telegram_connection, "AgAC") is None

    def test_a_path_that_is_not_a_string_is_refused(self, telegram_connection: ChannelConnection) -> None:
        with fake_bot_api(resolving({"nested": "object"})):
            assert TelegramAdapter().media_source(telegram_connection, "AgAC") is None

    def test_a_result_that_is_not_an_object_is_refused(self, telegram_connection: ChannelConnection) -> None:
        with fake_bot_api(lambda fake: fake.reply("getFile", Reply(result=["not", "an", "object"]))):
            assert TelegramAdapter().media_source(telegram_connection, "AgAC") is None


class TestWhenTelegramWillNotSay:
    def test_a_connection_with_no_token_resolves_nothing(self, connection: ChannelConnection) -> None:
        with fake_bot_api() as fake:
            assert TelegramAdapter().media_source(connection, "AgAC") is None
        assert fake.calls == [], "no call is attempted without a token"

    @pytest.mark.parametrize("status", [400, 401, 429, 500])
    def test_a_getfile_failure_resolves_to_nothing_rather_than_raising(
        self, telegram_connection: ChannelConnection, status: int
    ) -> None:
        """A months-old thread whose media Telegram has expired is not an incident."""
        with fake_bot_api(lambda fake: fake.reply("getFile", Reply(status=status))):
            assert TelegramAdapter().media_source(telegram_connection, "AgAC") is None

    def test_an_ok_false_body_resolves_to_nothing(self, telegram_connection: ChannelConnection) -> None:
        with fake_bot_api(lambda fake: fake.reply("getFile", Reply(body={"ok": False, "error_code": 400}))):
            assert TelegramAdapter().media_source(telegram_connection, "AgAC") is None


class TestTheTokenStaysOutOfTheLogs:
    """SECURITY-BASELINE §5, on the one path that must hold a token in a URL."""

    def test_resolving_logs_no_token(self, telegram_connection: ChannelConnection, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG)

        with fake_bot_api(resolving("photos/file_42.jpg")):
            TelegramAdapter().media_source(telegram_connection, "AgAC")

        assert BOT_TOKEN not in caplog.text
        assert SECRET_HALF not in caplog.text

    def test_a_failure_logs_no_token(self, telegram_connection: ChannelConnection, caplog: Any) -> None:
        caplog.set_level(logging.DEBUG)

        with fake_bot_api(lambda fake: fake.reply("getFile", Reply(status=401))):
            TelegramAdapter().media_source(telegram_connection, "AgAC")

        assert BOT_TOKEN not in caplog.text
        assert SECRET_HALF not in caplog.text


@pytest.mark.django_db
class TestTheWholePathIsGuarded:
    """SECURITY-BASELINE §6, on the real adapter rather than a stand-in.

    ``test_media.py`` proves the shared fetch is guarded using a fake adapter,
    which is the right level for the shared half — but it never runs this one.
    Between the two, an adapter that fetched the bytes itself, or that added a
    HEAD request to sniff a content type before the guarded GET, would be caught
    by neither. ``guard_required()`` exists precisely because "assert
    guarded_request was called" stays green next to a second, unguarded request.

    ``file_path`` is stubbed rather than faked over HTTP because ``getFile``
    legitimately does *not* go through the guard — it is ``request_json``'s case,
    a fixed host built from constants and a stored token — and ``guard_required``
    refuses every unstamped request inside its block, correctly including that
    one. Stubbing it leaves the download as the only HTTP in the block, which is
    exactly the claim under test.
    """

    def test_the_download_goes_through_the_guard(self, telegram_connection, monkeypatch):
        monkeypatch.setattr(telegram, "file_path", lambda _token, _file_id: "photos/file_42.jpg")
        internet = FakeInternet(serving(PNG), {"api.telegram.org": [FakeInternet.PUBLIC]}).install(monkeypatch)

        with deployment_cache_cleared(), guard_required() as guarded:
            resolved = fetch_media(telegram_connection, "AgAC")

        assert resolved.mime == "image/png"
        assert len(guarded) == 1, "one request, and it went through the guard"
        assert internet.requests[0].headers["host"] == "api.telegram.org"
        assert internet.requests[0].url.host == FakeInternet.PUBLIC, "pinned to the checked address"

    def test_the_token_reaches_the_platform_but_not_the_logs(self, telegram_connection, monkeypatch, caplog):
        """The one URL in this project that has to carry a credential."""
        monkeypatch.setattr(telegram, "file_path", lambda _token, _file_id: "photos/file_42.jpg")
        internet = FakeInternet(serving(PNG), {"api.telegram.org": [FakeInternet.PUBLIC]}).install(monkeypatch)

        with caplog.at_level(logging.DEBUG), deployment_cache_cleared():
            fetch_media(telegram_connection, "AgAC")

        assert BOT_TOKEN in str(internet.requests[0].url), "the file endpoint authenticates by the path"
        ours = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("apps.channels"))
        assert BOT_TOKEN not in ours
        assert SECRET_HALF not in ours
