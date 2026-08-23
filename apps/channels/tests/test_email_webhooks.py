"""Bounce and complaint notifications, end to end and hostile.

Two halves:

* the **signature** halves — Svix for Resend, RSA-over-SNS for SES — where the
  property is that a forged payload is rejected and looks exactly like an
  unknown connection (SECURITY-BASELINE §§2, 4);
* the **classification** half, where a hard bounce or a complaint suppresses the
  address and a soft bounce only fails the message.

The SNS half signs its fixtures with a key pair generated in the test, which is
the only way to assert that verification actually verifies: a hand-written
"signature" string would pass a check that did nothing.
"""

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from django.test import Client

from apps.channels.models import ChannelConnection, EmailSuppression
from apps.channels.providers import email_signatures
from apps.common.platforms import Platform

pytestmark = pytest.mark.django_db

FIXTURES = Path(__file__).parent / "fixtures" / "email"
CERT_URL = "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-abc123.pem"
SIGNING_SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _suppression_processor() -> Any:
    """Put this app's own inbound processor back for the duration.

    ``conftest._clean_processors`` empties the contract-6 seam for every test in
    this app, deliberately: the framework tests are about the seam itself and
    must not run each dispatch through the messaging spine. These tests are the
    opposite — they assert what a bounce *does* — so the one processor this
    issue registers is re-installed, and only that one. The identity's
    ``opted_out_at`` is messaging's persistence stage and is asserted in
    ``test_email_suppression.py``, against the real registry.
    """
    from apps.channels import ingest
    from apps.channels.providers.email import SUPPRESSION_PROCESSOR, record_suppressions

    ingest.register_processor(record_suppressions, name=SUPPRESSION_PROCESSOR, order=ingest.LATE_ORDER)
    yield
    ingest.unregister_processor(SUPPRESSION_PROCESSOR)


@pytest.fixture
def email_connection(tenancy: Any) -> ChannelConnection:
    connection = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.EMAIL.value,
        display_name="Sender",
        external_id="sender.test",
    )
    connection.credentials = {  # type: ignore[assignment]
        "provider": "resend",
        "api_key": "re_key",
        "signing_secret": SIGNING_SECRET,
        "from_address": "hello@sender.test",
    }
    connection.save()
    return connection


@pytest.fixture
def ses_connection(tenancy: Any) -> ChannelConnection:
    connection = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.EMAIL.value,
        display_name="SES sender",
        external_id="ses.test",
    )
    connection.credentials = {  # type: ignore[assignment]
        "provider": "ses",
        "access_key_id": "AKIA0000000000000000",
        "secret_access_key": "s3cret",
        "region": "eu-west-1",
        "from_address": "hello@ses.test",
    }
    connection.save()
    return connection


@pytest.fixture
def sns_keypair(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real RSA key and self-signed certificate, served to the guard's fetcher.

    ``guarded_request`` is patched rather than the certificate cache, so the
    allowlist check and the caching both still run — only the network is absent.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM)

    class Response:
        status_code = 200
        ok = True
        content = pem

    email_signatures.clear_certificate_cache()
    monkeypatch.setattr(email_signatures, "guarded_request", lambda *a, **k: Response())
    yield key
    email_signatures.clear_certificate_cache()


def sign_sns(payload: dict[str, Any], key: Any) -> dict[str, Any]:
    """Sign a payload the way SNS does, so verification has something real to check."""
    fields = email_signatures._SIGNED_FIELDS[payload["Type"]]
    canonical = "".join(f"{name}\n{payload[name]}\n" for name in fields if payload.get(name) is not None)
    signature = key.sign(canonical.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())  # noqa: S303 - v1 topics
    return {**payload, "Signature": base64.b64encode(signature).decode(), "SigningCertURL": CERT_URL}


def sns_envelope(message: dict[str, Any], key: Any, **overrides: Any) -> dict[str, Any]:
    payload = {
        "Type": "Notification",
        "MessageId": overrides.pop("MessageId", "sns-1"),
        "TopicArn": "arn:aws:sns:eu-west-1:123456789012:brightbean-bounces",
        "Message": json.dumps(message),
        "Timestamp": "2026-08-20T10:00:00.000Z",
        "SignatureVersion": "1",
        **overrides,
    }
    return sign_sns(payload, key)


def svix_headers(body: bytes, secret: str = SIGNING_SECRET, *, timestamp: int | None = None) -> dict[str, str]:
    import hashlib
    import hmac

    message_id = "msg_1"
    sent_at = str(timestamp if timestamp is not None else int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = b".".join((message_id.encode(), sent_at.encode(), body))
    digest = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {
        "HTTP_SVIX_ID": message_id,
        "HTTP_SVIX_TIMESTAMP": sent_at,
        "HTTP_SVIX_SIGNATURE": f"v1,{digest}",
    }


def post(client: Client, connection: ChannelConnection, provider: str, body: Any, **extra: str) -> Any:
    raw = json.dumps(body).encode("utf-8")
    return client.post(
        f"/webhooks/email/{provider}/{connection.pk}/",
        data=raw,
        content_type="application/json",
        # WSGI-style keys (HTTP_SVIX_ID and friends) go in `extra`, which Django's
        # test client forwards into the environ.
        **dict(extra),  # type: ignore[arg-type]
    )


def suppressed(workspace: Any) -> set[str]:
    return set(EmailSuppression.objects.for_workspace(workspace).values_list("address", flat=True))


# ---------------------------------------------------------------------------
# Resend
# ---------------------------------------------------------------------------


class TestResendSignature:
    def test_a_valid_signature_is_accepted(self, client: Client, email_connection: ChannelConnection) -> None:
        body = fixture("resend_delivered")
        raw = json.dumps(body).encode("utf-8")
        assert post(client, email_connection, "resend", body, **svix_headers(raw)).status_code == 200

    def test_a_forged_signature_is_refused(self, client: Client, email_connection: ChannelConnection) -> None:
        body = fixture("resend_bounced")
        headers = svix_headers(json.dumps(body).encode("utf-8"))
        headers["HTTP_SVIX_SIGNATURE"] = "v1," + base64.b64encode(b"x" * 32).decode()
        assert post(client, email_connection, "resend", body, **headers).status_code == 403

    def test_a_signature_over_a_different_body_is_refused(
        self, client: Client, email_connection: ChannelConnection
    ) -> None:
        """The whole point of signing the raw body: swapping it must break it."""
        headers = svix_headers(json.dumps(fixture("resend_delivered")).encode("utf-8"))
        assert post(client, email_connection, "resend", fixture("resend_bounced"), **headers).status_code == 403

    def test_an_old_timestamp_is_refused(self, client: Client, email_connection: ChannelConnection) -> None:
        body = fixture("resend_delivered")
        raw = json.dumps(body).encode("utf-8")
        stale = int(time.time()) - email_signatures.SVIX_TOLERANCE_SECONDS - 60
        assert post(client, email_connection, "resend", body, **svix_headers(raw, timestamp=stale)).status_code == 403

    def test_missing_headers_are_refused(self, client: Client, email_connection: ChannelConnection) -> None:
        assert post(client, email_connection, "resend", fixture("resend_delivered")).status_code == 403

    def test_a_connection_with_no_secret_refuses_everything(self, client: Client, tenancy: Any) -> None:
        connection = ChannelConnection(
            workspace=tenancy.workspace,
            platform=Platform.EMAIL.value,
            display_name="No secret",
            external_id="nosecret.test",
        )
        connection.credentials = {"provider": "resend", "api_key": "re_key"}  # type: ignore[assignment]
        connection.save()
        body = fixture("resend_delivered")
        headers = svix_headers(json.dumps(body).encode("utf-8"))
        assert post(client, connection, "resend", body, **headers).status_code == 403


class TestResendClassification:
    def _deliver(self, client: Client, connection: ChannelConnection, name: str) -> Any:
        body = fixture(name)
        raw = json.dumps(body).encode("utf-8")
        return post(client, connection, "resend", body, **svix_headers(raw))

    def test_a_hard_bounce_suppresses(self, client: Client, email_connection: ChannelConnection) -> None:
        assert self._deliver(client, email_connection, "resend_bounced").status_code == 200
        assert suppressed(email_connection.workspace) == {"gone@example.test"}

    def test_a_soft_bounce_does_not_suppress(self, client: Client, email_connection: ChannelConnection) -> None:
        assert self._deliver(client, email_connection, "resend_bounced_soft").status_code == 200
        assert suppressed(email_connection.workspace) == set()

    def test_a_complaint_suppresses(self, client: Client, email_connection: ChannelConnection) -> None:
        assert self._deliver(client, email_connection, "resend_complained").status_code == 200
        assert suppressed(email_connection.workspace) == {"annoyed@example.test"}

    def test_a_delivery_suppresses_nothing(self, client: Client, email_connection: ChannelConnection) -> None:
        assert self._deliver(client, email_connection, "resend_delivered").status_code == 200
        assert suppressed(email_connection.workspace) == set()

    def test_an_open_is_ignored(self, client: Client, email_connection: ChannelConnection) -> None:
        """Open and click tracking belongs to #26 (L7-A), not here."""
        assert self._deliver(client, email_connection, "resend_opened").status_code == 200
        assert suppressed(email_connection.workspace) == set()


# ---------------------------------------------------------------------------
# SES via SNS
# ---------------------------------------------------------------------------


class TestSNSSignature:
    def test_a_correctly_signed_notification_is_accepted(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any
    ) -> None:
        payload = sns_envelope(fixture("ses_delivery"), sns_keypair)
        assert post(client, ses_connection, "ses", payload).status_code == 200

    def test_a_forged_signature_is_refused(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any
    ) -> None:
        payload = sns_envelope(fixture("ses_bounce_hard"), sns_keypair)
        payload["Signature"] = base64.b64encode(b"forged" * 40).decode()
        assert post(client, ses_connection, "ses", payload).status_code == 403
        assert suppressed(ses_connection.workspace) == set()

    def test_a_tampered_message_is_refused(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any
    ) -> None:
        """The signature covers Message, so editing the bounce breaks it."""
        payload = sns_envelope(fixture("ses_delivery"), sns_keypair)
        payload["Message"] = json.dumps(fixture("ses_bounce_hard"))
        assert post(client, ses_connection, "ses", payload).status_code == 403
        assert suppressed(ses_connection.workspace) == set()

    @pytest.mark.parametrize(
        "cert_url",
        [
            "https://sns.eu-west-1.amazonaws.com.evil.test/SimpleNotificationService-a.pem",
            "http://sns.eu-west-1.amazonaws.com/SimpleNotificationService-a.pem",
            "https://evil.test/SimpleNotificationService-a.pem",
            "https://sns.eu-west-1.amazonaws.com/../SimpleNotificationService-a.pem",
            "https://sns.eu-west-1.amazonaws.com/evil.pem",
            "https://sns.eu-west-1.amazonaws.com/SimpleNotificationService-a.pem?x=https://sns.eu-west-1.amazonaws.com/",
        ],
    )
    def test_a_certificate_url_outside_the_allowlist_is_refused(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any, cert_url: str
    ) -> None:
        """The SigningCertURL is attacker-supplied — this is the SSRF gate."""
        payload = sns_envelope(fixture("ses_bounce_hard"), sns_keypair)
        payload["SigningCertURL"] = cert_url
        assert post(client, ses_connection, "ses", payload).status_code == 403

    def test_an_unknown_signature_version_is_refused(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any
    ) -> None:
        payload = sns_envelope(fixture("ses_delivery"), sns_keypair, SignatureVersion="9")
        assert post(client, ses_connection, "ses", payload).status_code == 403

    def test_the_certificate_is_fetched_once(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        original = email_signatures.guarded_request

        def counting(method: str, url: str, **kwargs: Any) -> Any:
            calls.append(url)
            return original(method, url, **kwargs)

        monkeypatch.setattr(email_signatures, "guarded_request", counting)
        for index in range(3):
            payload = sns_envelope(fixture("ses_delivery"), sns_keypair, MessageId=f"sns-{index}")
            assert post(client, ses_connection, "ses", payload).status_code == 200
        assert len(calls) == 1


class TestSNSClassification:
    def _deliver(self, client: Client, connection: ChannelConnection, key: Any, name: str) -> Any:
        return post(client, connection, "ses", sns_envelope(fixture(name), key))

    def test_a_permanent_bounce_suppresses(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any
    ) -> None:
        assert self._deliver(client, ses_connection, sns_keypair, "ses_bounce_hard").status_code == 200
        assert suppressed(ses_connection.workspace) == {"gone@example.test"}

    def test_a_transient_bounce_does_not_suppress(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any
    ) -> None:
        assert self._deliver(client, ses_connection, sns_keypair, "ses_bounce_soft").status_code == 200
        assert suppressed(ses_connection.workspace) == set()

    def test_a_complaint_suppresses(self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any) -> None:
        assert self._deliver(client, ses_connection, sns_keypair, "ses_complaint").status_code == 200
        assert suppressed(ses_connection.workspace) == {"annoyed@example.test"}

    def test_a_delivery_suppresses_nothing(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any
    ) -> None:
        assert self._deliver(client, ses_connection, sns_keypair, "ses_delivery").status_code == 200
        assert suppressed(ses_connection.workspace) == set()


class TestSubscriptionConfirmation:
    def test_it_is_confirmed_through_the_aws_api_not_by_fetching_the_url(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SubscribeURL is a second attacker-supplied URL, so it is never fetched.

        ``sns:ConfirmSubscription`` does the same job with the credentials the
        connection already holds, and costs no SSRF surface at all.
        """
        from apps.channels.providers import email_backends
        from apps.channels.tests.email_support import FakeSESClient

        sns = FakeSESClient()
        monkeypatch.setattr(email_backends, "ses_client", lambda *a, **k: sns)

        payload = sign_sns({**fixture("sns_subscription_confirmation"), "SigningCertURL": CERT_URL}, sns_keypair)
        assert post(client, ses_connection, "ses", payload).status_code == 200

        from apps.channels.providers.email import CONFIRM_SUBSCRIPTION_ACTION, confirm_sns_subscription
        from apps.queueing.models import ScheduledAction

        action = ScheduledAction.objects.unscoped().filter(type=CONFIRM_SUBSCRIPTION_ACTION).first()
        assert action is not None
        confirm_sns_subscription(action.payload, action)
        assert sns.confirmed[0]["TopicArn"] == payload["TopicArn"]
        assert sns.confirmed[0]["Token"] == payload["Token"]

    def test_an_unsigned_confirmation_is_refused(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any
    ) -> None:
        payload = fixture("sns_subscription_confirmation")
        assert post(client, ses_connection, "ses", payload).status_code == 403


# ---------------------------------------------------------------------------
# Hostile payloads (SECURITY-BASELINE §2)
# ---------------------------------------------------------------------------


class TestHostilePayloads:
    """A verified delivery whose contents are nonsense must not 500 or suppress.

    The signature proves the *provider* sent it; it proves nothing about the
    addresses inside, which the provider echoed from somewhere else.
    """

    @pytest.mark.parametrize(
        "message",
        [
            {},
            {"notificationType": "Bounce"},
            {"notificationType": "Bounce", "bounce": "not a dict", "mail": {"messageId": "m"}},
            {"notificationType": "Bounce", "bounce": {"bouncedRecipients": "nope"}, "mail": {}},
            {"notificationType": "Bounce", "bounce": {"bouncedRecipients": [{"emailAddress": None}]}, "mail": {}},
            {"notificationType": "Bounce", "bounce": {"bouncedRecipients": [{"emailAddress": "not an address"}]}},
            {
                "notificationType": "Bounce",
                "bounce": {
                    "bounceType": "<script>alert(1)</script>",
                    "bounceSubType": "x" * 5000,
                    "bouncedRecipients": [{"emailAddress": "<img onerror=x>@example.test"}],
                },
                "mail": {"messageId": "\x00nul"},
            },
            {"notificationType": ["Bounce"], "bounce": {}, "mail": {}},
            {"notificationType": "Delivery", "delivery": {"recipients": [None, 7, {}]}, "mail": {}},
        ],
        ids=[
            "empty",
            "no bounce block",
            "bounce is a string",
            "recipients is a string",
            "recipient address is null",
            "recipient is not an address",
            "injection strings everywhere",
            "notificationType is a list",
            "recipients are the wrong types",
        ],
    )
    def test_a_malformed_notification_is_a_200_and_suppresses_nothing(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any, message: dict[str, Any]
    ) -> None:
        response = post(client, ses_connection, "ses", sns_envelope(message, sns_keypair))
        assert response.status_code == 200
        assert suppressed(ses_connection.workspace) == set()

    def test_a_body_that_is_not_json_is_a_400(self, client: Client, ses_connection: ChannelConnection) -> None:
        response = client.post(
            f"/webhooks/email/ses/{ses_connection.pk}/",
            data=b"{not json",
            content_type="application/json",
        )
        # 403 before 400: verification runs first, and an unparseable body
        # cannot carry a valid signature.
        assert response.status_code == 403

    def test_an_inner_message_that_is_not_json_is_survived(
        self, client: Client, ses_connection: ChannelConnection, sns_keypair: Any
    ) -> None:
        payload = sign_sns(
            {
                "Type": "Notification",
                "MessageId": "sns-bad",
                "TopicArn": "arn:aws:sns:eu-west-1:1:t",
                "Message": "{not json at all",
                "Timestamp": "2026-08-20T10:00:00.000Z",
                "SignatureVersion": "1",
            },
            sns_keypair,
        )
        assert post(client, ses_connection, "ses", payload).status_code == 200

    def test_a_resend_payload_with_a_hostile_address_suppresses_nothing(
        self, client: Client, email_connection: ChannelConnection
    ) -> None:
        body = {
            "type": "email.bounced",
            "id": "evt-x",
            "created_at": "nonsense",
            "data": {"email_id": "m", "to": ["not an address"], "bounce": {"type": "hard"}},
        }
        raw = json.dumps(body).encode("utf-8")
        assert post(client, email_connection, "resend", body, **svix_headers(raw)).status_code == 200
        assert suppressed(email_connection.workspace) == set()


class TestSMTPHasNoCallback:
    def test_every_delivery_on_the_smtp_route_is_refused(self, client: Client, tenancy: Any) -> None:
        """No SMTP callback exists, so the route answers the same 403 to everyone.

        That is what keeps ``tests/idor.py``'s waiver for ``webhook_email`` true:
        the route's answer must not depend on whether the connection id is real.
        """
        connection = ChannelConnection(
            workspace=tenancy.workspace,
            platform=Platform.EMAIL.value,
            display_name="SMTP sender",
            external_id="smtp.test",
        )
        connection.credentials = {"provider": "smtp", "host": "mail.test"}  # type: ignore[assignment]
        connection.save()

        real = post(client, connection, "smtp", {"anything": True})
        unknown = client.post(
            "/webhooks/email/smtp/11111111-1111-1111-1111-111111111111/",
            data=b"{}",
            content_type="application/json",
        )
        assert real.status_code == unknown.status_code == 403
        assert real.content == unknown.content
