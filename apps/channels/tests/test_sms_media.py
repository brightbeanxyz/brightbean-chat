"""Twilio's half of media resolution: a stored ``MediaUrl`` plus Basic auth.

The shared half — the guard, the sniff, the disposition — is
``apps/channels/tests/test_media.py``. What is Twilio's own is that the
identifier already *is* the address, so there is no platform call to make and
the whole job is attaching the account's credentials to it — which is exactly
why the origin has to be checked first.
"""

import base64
import logging
from typing import Any

import pytest

from apps.channels.media import fetch_media
from apps.channels.models import ChannelConnection
from apps.channels.providers.sms import TwilioAdapter, store_credentials
from tests.ssrf import FakeInternet, deployment_cache_cleared, guard_required, serving

pytestmark = pytest.mark.django_db

SID = "AC" + "0" * 32
TOKEN = "tw" + "il" + "io-auth-token-value"  # noqa: S105 - split so push protection sees no credential
MEDIA_URL = f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages/MM1/Media/ME1"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 56
EXPECTED_BASIC = "Basic " + base64.b64encode(f"{SID}:{TOKEN}".encode()).decode()


@pytest.fixture
def sms_connection(tenancy: Any) -> ChannelConnection:
    from apps.common.platforms import Platform
    from apps.messaging.tests.conftest import make_connection

    connection = make_connection(tenancy.workspace, platform=Platform.SMS, suffix="media")
    store_credentials(connection, sid=SID, token=TOKEN)
    connection.save(update_fields=["credentials", "updated_at"])
    return connection


class TestResolvingAMediaUrl:
    def test_the_stored_url_is_the_address(self, sms_connection: ChannelConnection) -> None:
        """No platform call: unlike a Telegram file_id, this needs no getFile."""
        source = TwilioAdapter().media_source(sms_connection, MEDIA_URL)

        assert source is not None
        assert source.url == MEDIA_URL

    def test_the_accounts_credentials_ride_in_a_header(self, sms_connection: ChannelConnection) -> None:
        source = TwilioAdapter().media_source(sms_connection, MEDIA_URL)

        assert source is not None
        assert source.headers == (("Authorization", EXPECTED_BASIC),)
        assert TOKEN not in source.url, "never userinfo; the guard refuses it and a URL reaches logs"

    def test_a_connection_with_no_credentials_resolves_nothing(self, tenancy: Any) -> None:
        from apps.common.platforms import Platform
        from apps.messaging.tests.conftest import make_connection

        bare = make_connection(tenancy.workspace, platform=Platform.SMS, suffix="bare")

        assert TwilioAdapter().media_source(bare, MEDIA_URL) is None


class TestTheOriginIsCheckedBeforeTheCredential:
    """``media_id`` arrived in a webhook body, and the credential is the account.

    Twilio's signature check is what makes a forged body hard. This is what
    makes one useless, which is the property worth having.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.test/Media/ME1",
            "https://api.twilio.com.evil.example.test/Media/ME1",
            "https://evil.example.test/api.twilio.com/Media/ME1",
            "http://api.twilio.com/Media/ME1",
            "https://api.twilio.com@evil.example.test/Media/ME1",
            "//api.twilio.com/Media/ME1",
            "file:///etc/passwd",
            "not a url",
            "",
            "   ",
        ],
    )
    def test_a_url_off_the_api_host_gets_no_credential(self, sms_connection: ChannelConnection, url: str) -> None:
        assert TwilioAdapter().media_source(sms_connection, url) is None

    def test_the_refusal_is_loud(self, sms_connection: ChannelConnection, caplog: Any) -> None:
        """An off-host media URL is not a routine miss — either Twilio changed
        its API or something is trying to collect an auth token."""
        with caplog.at_level(logging.DEBUG):
            TwilioAdapter().media_source(sms_connection, "https://evil.example.test/x.jpg")

        warnings = [r for r in caplog.records if r.name.startswith("apps.channels") and r.levelno >= logging.WARNING]
        assert warnings

    def test_no_credential_reaches_a_log_line(self, sms_connection: ChannelConnection, caplog: Any) -> None:
        with caplog.at_level(logging.DEBUG):
            TwilioAdapter().media_source(sms_connection, "https://evil.example.test/x.jpg")
            TwilioAdapter().media_source(sms_connection, MEDIA_URL)

        assert TOKEN not in caplog.text
        assert EXPECTED_BASIC not in caplog.text


class TestTheWholePathIsGuarded:
    """SECURITY-BASELINE §6, on the real adapter rather than a stand-in.

    Nothing is stubbed here: unlike Telegram, resolution makes no platform call
    of its own, so the download is the only HTTP in the block and
    ``guard_required`` can wrap the whole of ``fetch_media``.
    """

    def test_the_download_goes_through_the_guard(self, sms_connection: ChannelConnection, monkeypatch: Any) -> None:
        internet = FakeInternet(serving(PNG), {"api.twilio.com": [FakeInternet.PUBLIC]}).install(monkeypatch)

        with deployment_cache_cleared(), guard_required() as guarded:
            resolved = fetch_media(sms_connection, MEDIA_URL)

        assert resolved.mime == "image/png"
        assert len(guarded) == 1
        assert internet.requests[0].headers["authorization"] == EXPECTED_BASIC
        assert internet.requests[0].url.host == FakeInternet.PUBLIC, "pinned to the checked address"

    def test_an_off_host_url_never_opens_a_socket(self, sms_connection: ChannelConnection, monkeypatch: Any) -> None:
        from apps.channels.media import MediaUnavailableError

        internet = FakeInternet(serving(PNG), {"evil.example.test": [FakeInternet.PUBLIC]}).install(monkeypatch)

        with deployment_cache_cleared(), pytest.raises(MediaUnavailableError):
            fetch_media(sms_connection, "https://evil.example.test/x.jpg")

        assert internet.requests == []
