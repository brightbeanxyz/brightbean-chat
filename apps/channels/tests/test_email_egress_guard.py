"""The two egress paths that are not httpx, and therefore not the guard's.

SECURITY-BASELINE §6.1 says every server-initiated request goes through
``apps.common.outbound.guarded_request``, "no exceptions", and
``tests/test_ssrf_call_sites.py`` asserts that structurally — for httpx. Two
paths in :mod:`apps.channels.providers.email_backends` reach the network without
passing either door, because neither is httpx: Django's SMTP backend and boto3
to SES. Issues #91 and #92 closed the gap; this module is what keeps it closed.

The two are tested together because they now share one classification —
``email_backends._checked_address``, which is ``outbound.refusal_for`` — and the
value of that is precisely that they cannot drift apart.
"""

import socket
from typing import Any

import pytest

from apps.channels.providers import email_backends
from apps.channels.providers.exceptions import APIError

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _closed_by_default(settings: Any) -> None:
    settings.EMAIL_SMTP_ALLOW_INTERNAL = False
    settings.EXTERNAL_REQUEST_ALLOW_PRIVATE = False


class _Events:
    """botocore's event system, reduced to the one call ``guard_boto_client`` makes."""

    def __init__(self) -> None:
        self.handlers: list[tuple[str, Any]] = []

    def register(self, name: str, handler: Any) -> None:
        self.handlers.append((name, handler))


class _Client:
    def __init__(self) -> None:
        self.meta = type("Meta", (), {"events": _Events()})()


class _Request:
    def __init__(self, url: str) -> None:
        self.url = url


def _send(client: Any, url: str) -> Any:
    """Fire the registered ``before-send`` handler the way botocore would."""
    (_name, handler), *_rest = client.meta.events.handlers
    return handler(request=_Request(url))


class TestTheBotoEndpointIsChecked:
    """#91: boto3 owns its transport, so the address rules have to be re-applied."""

    def test_the_handler_registers_on_before_send(self) -> None:
        client = email_backends.guard_boto_client(_Client())

        assert [name for name, _ in client.meta.events.handlers] == ["before-send.*"]

    def test_a_public_endpoint_proceeds(self) -> None:
        """Returning None is how a botocore handler says "carry on"."""
        client = email_backends.guard_boto_client(_Client())

        assert _send(client, "https://email.eu-west-1.amazonaws.com/v2/email/outbound-emails") is None

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "169.254.169.254", "10.0.0.1", "0.0.0.0"],  # noqa: S104 - a host to refuse, not to bind
        ids=["loopback", "cloud metadata", "private", "unspecified"],
    )
    def test_an_internal_endpoint_is_refused(self, host: str) -> None:
        client = email_backends.guard_boto_client(_Client())

        with pytest.raises(APIError) as caught:
            _send(client, f"https://{host}/v2/email/outbound-emails")

        assert caught.value.code == "blocked_host"

    def test_the_refusal_does_not_name_the_address(self) -> None:
        client = email_backends.guard_boto_client(_Client())

        with pytest.raises(APIError) as caught:
            _send(client, "https://169.254.169.254/v2/email/outbound-emails")

        assert "169.254" not in str(caught.value)

    def test_a_url_with_no_host_is_refused(self) -> None:
        client = email_backends.guard_boto_client(_Client())

        with pytest.raises(APIError) as caught:
            _send(client, "not-a-url")

        assert caught.value.code == "blocked_host"

    def test_the_real_client_factory_installs_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard is worthless if ``ses_client`` can hand back an unguarded client."""
        built = _Client()
        monkeypatch.setattr(
            email_backends,
            "credentials_of",
            lambda _c: {
                "access_key_id": "AKIA",
                "secret_access_key": "s",
                "region": "eu-west-1",
            },
        )
        monkeypatch.setitem(
            __import__("sys").modules, "boto3", type("boto3", (), {"client": staticmethod(lambda *a, **k: built)})
        )

        client = email_backends.ses_client(object())

        assert [name for name, _ in client.meta.events.handlers] == ["before-send.*"]


class TestTheSmtpConnectionIsPinned:
    """#92: the check and the connect must not be able to disagree."""

    def test_the_validated_address_is_handed_back(self) -> None:
        """``resolved_destination`` returns what it checked, which is what makes pinning possible."""
        address = email_backends.resolved_destination("example.com", 587)

        assert address and address != "example.com"

    def test_pinning_is_waived_when_internal_smtp_is_allowed(self, settings: Any) -> None:
        """An operator who opted into a local relay is not asking to be pinned to it."""
        settings.EMAIL_SMTP_ALLOW_INTERNAL = True

        assert email_backends.resolved_destination("127.0.0.1", 25) == ""

    def test_the_socket_goes_to_the_pinned_address_not_the_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The rebinding window: a second lookup must not decide where we connect."""
        attempts: list[tuple[str, int]] = []

        def _record(address: tuple[str, int], timeout: Any = None, source: Any = None) -> Any:
            attempts.append((address[0], address[1]))
            raise OSError("not connecting in a test")

        monkeypatch.setattr(socket, "create_connection", _record)
        pinned = email_backends._pinned_smtp_class(_BareSMTP, "203.0.113.9")()

        with pytest.raises(OSError, match="not connecting"):
            pinned._get_socket("mail.example.com", 587, 5)

        assert attempts == [("203.0.113.9", 587)]

    def test_a_zero_timeout_is_refused_the_way_smtplib_refuses_it(self) -> None:
        pinned = email_backends._pinned_smtp_class(_BareSMTP, "203.0.113.9")()

        with pytest.raises(OSError, match="Non-blocking"):
            pinned._get_socket("mail.example.com", 587, 0)

    def test_the_class_keeps_a_recognisable_name(self) -> None:
        """It appears in tracebacks; "PinnedSMTP" is worth more than "PinnedSMTP.<locals>"."""
        assert email_backends._pinned_smtp_class(_BareSMTP, "203.0.113.9").__name__ == "Pinned_BareSMTP"


class _BareSMTP:
    """Stands in for ``smtplib.SMTP`` — only ``_get_socket``'s surface is used."""

    source_address = None

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass
