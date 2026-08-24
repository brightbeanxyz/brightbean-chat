"""``/c/`` and ``/o/`` — the two public token routes (SECURITY-BASELINE §4).

These are the routes ``tests/idor.py`` waives, and the two classes named in that
waiver are here. If either is deleted, the waivers must go with it.
"""

from typing import Any
from urllib.parse import urlsplit

import pytest
from django.urls import reverse

from apps.analytics import tracking
from apps.analytics.models import NodeStatDaily
from apps.analytics.tests.conftest import ENTRY_NODE, TEXT, make_execution
from apps.channels.tests.fake_adapter import registered
from apps.common.platforms import Platform
from apps.common.signing import sign
from apps.flows.messaging import message_idempotency_key
from apps.messaging import services
from apps.messaging.models import Message, MessageStatus

pytestmark = pytest.mark.django_db

TARGET = "https://example.test/pricing?plan=pro"


def click_path(flow: Any, node_id: str = ENTRY_NODE, target: str = TARGET) -> str:
    """The path part of a freshly minted click URL."""
    return urlsplit(tracking.click_url(flow_id=flow.pk, node_id=node_id, target=target)).path


def clicks(workspace: Any, flow: Any, node_id: str = ENTRY_NODE) -> int:
    row = NodeStatDaily.objects.for_workspace(workspace).filter(flow=flow, node_id=node_id).first()
    return 0 if row is None else row.clicked


class TestClickRedirect:
    def test_it_redirects_to_the_signed_destination(self, client: Any, tenancy: Any, flow: Any) -> None:
        response = client.get(click_path(flow))

        assert response.status_code == 302
        assert response["Location"] == TARGET

    def test_it_counts_the_click_once(self, client: Any, tenancy: Any, flow: Any) -> None:
        client.get(click_path(flow))

        assert clicks(tenancy.workspace, flow) == 1

    def test_two_clicks_count_twice(self, client: Any, tenancy: Any, flow: Any) -> None:
        """SPEC §18 keeps no per-contact history, so ``clicked`` is total clicks
        rather than unique clickers — two presses are two."""
        client.get(click_path(flow))
        client.get(click_path(flow))

        assert clicks(tenancy.workspace, flow) == 2

    def test_the_incoming_query_string_is_carried_through(self, client: Any, tenancy: Any, flow: Any) -> None:
        response = client.get(f"{click_path(flow)}?utm_source=telegram")

        assert response["Location"] == f"{TARGET}&utm_source=telegram"

    def test_a_destination_with_no_query_string_gains_the_request_s(self, client: Any, tenancy: Any, flow: Any) -> None:
        response = client.get(f"{click_path(flow, target='https://example.test/docs')}?ref=abc")

        assert response["Location"] == "https://example.test/docs?ref=abc"

    def test_a_deleted_flow_still_redirects(self, client: Any, tenancy: Any, flow: Any) -> None:
        """The link is in somebody's chat forever. Breaking it because the
        workspace tidied up would punish the reader for a housekeeping action."""
        path = click_path(flow)
        flow.delete()

        response = client.get(path)

        assert response.status_code == 302
        assert response["Location"] == TARGET


class TestNoOpenRedirect:
    """Named in ``tests/idor.py``'s waiver. A click redirect is an open redirect
    unless it refuses to be."""

    def test_the_destination_cannot_be_supplied_on_the_query_string(self, client: Any, tenancy: Any, flow: Any) -> None:
        response = client.get(f"{click_path(flow)}?url=https://attacker.test&next=https://attacker.test")

        assert response.status_code == 302
        assert urlsplit(response["Location"]).netloc == "example.test"

    @pytest.mark.parametrize(
        "target",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "file:///etc/passwd",
            "vbscript:msgbox(1)",
            # Scheme-relative: inherits whatever scheme the page was served over
            # and reads as a path to anyone skimming the stored value.
            "//attacker.test/phish",
            # No host at all.
            "https:///nowhere",
            "",
        ],
    )
    def test_a_non_http_destination_is_a_bare_404(self, client: Any, tenancy: Any, flow: Any, target: str) -> None:
        """Checked at *redirect* time, not only when the token was minted, so a
        token from an older release cannot become one."""
        token = sign(
            {tracking.FLOW_KEY: str(flow.pk), tracking.NODE_KEY: ENTRY_NODE, tracking.URL_KEY: target},
            purpose=tracking.CLICK_PURPOSE,
        )

        response = client.get(reverse("click_redirect", kwargs={"token": token}))

        assert response.status_code == 404
        assert clicks(tenancy.workspace, flow) == 0


class TestTokenIndistinguishability:
    """Named in ``tests/idor.py``'s waiver. Every rejection looks the same."""

    def test_a_tampered_token_is_a_bare_404(self, client: Any, tenancy: Any, flow: Any) -> None:
        path = click_path(flow)
        tampered = path[:-4] + "AAA/"

        response = client.get(tampered)

        assert response.status_code == 404
        assert clicks(tenancy.workspace, flow) == 0

    def test_a_token_minted_for_another_purpose_is_a_bare_404(self, client: Any, tenancy: Any, flow: Any) -> None:
        """The purpose salt is what stops an unsubscribe link, a media URL or a
        pixel being replayed as a redirect."""
        token = sign({"i": str(flow.pk)}, purpose="unsubscribe")

        assert client.get(reverse("click_redirect", kwargs={"token": token})).status_code == 404

    def test_a_pixel_token_is_not_a_click_token_and_the_reverse(
        self, client: Any, tenancy: Any, flow: Any, execution: Any
    ) -> None:
        pixel = urlsplit(
            tracking.open_url(
                workspace_id=tenancy.workspace.pk,
                idempotency_key=message_idempotency_key(execution, ENTRY_NODE),
            )
        ).path
        click = click_path(flow)

        assert client.get(reverse("click_redirect", kwargs={"token": pixel.split("/")[2]})).status_code == 404
        assert client.get(reverse("open_pixel", kwargs={"token": click.split("/")[2]})).status_code == 404

    def test_an_unknown_payload_version_is_a_bare_404(self, client: Any, tenancy: Any, flow: Any) -> None:
        token = sign(
            {tracking.FLOW_KEY: str(flow.pk), tracking.NODE_KEY: ENTRY_NODE, tracking.URL_KEY: TARGET},
            purpose=tracking.CLICK_PURPOSE,
            version=99,
        )

        assert client.get(reverse("click_redirect", kwargs={"token": token})).status_code == 404

    def test_a_giant_token_is_refused_before_any_signature_work(self, client: Any) -> None:
        assert client.get(f"/c/{'a' * 5000}/").status_code == 404

    def test_every_rejection_has_the_same_empty_body(self, client: Any, tenancy: Any, flow: Any) -> None:
        """A caller must not be able to tell *why* it was refused."""
        bodies = {
            client.get(reverse("click_redirect", kwargs={"token": "garbage"})).content,
            client.get(reverse("click_redirect", kwargs={"token": sign({"x": 1}, purpose="unsubscribe")})).content,
        }

        assert len(bodies) == 1


class TestOpenPixel:
    def test_it_answers_a_gif_and_marks_the_message_read(
        self, client: Any, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        execution = make_execution(flow, contact, connection)
        key = message_idempotency_key(execution, ENTRY_NODE)
        with registered(Platform.TELEGRAM):
            services.send_outbound(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                source="automation",
                idempotency_key=key,
            )

        path = urlsplit(tracking.open_url(workspace_id=tenancy.workspace.pk, idempotency_key=key)).path
        response = client.get(path)

        assert response.status_code == 200
        assert response["Content-Type"] == "image/gif"
        assert "no-store" in response["Cache-Control"]
        message = Message.objects.for_workspace(tenancy.workspace).get(idempotency_key=key)
        assert message.status == MessageStatus.READ

    def test_it_adds_no_click_and_counts_the_delivery_it_proves(
        self, client: Any, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        """SPEC §5 gives ``node_stat_daily`` four columns and "opened" is not one
        of them, so an open adds no counter of its own.

        What it does move is ``delivered``, and only because the message crossed
        that rung on its way to ``read``: a mail client displayed it, which is
        proof it arrived. For email that is often the *only* such proof — SMTP
        has no delivery receipt — so dropping it would leave a channel whose
        delivered column is permanently zero.
        """
        execution = make_execution(flow, contact, connection)
        key = message_idempotency_key(execution, ENTRY_NODE)
        with registered(Platform.TELEGRAM):
            services.send_outbound(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                source="automation",
                idempotency_key=key,
            )
        before = NodeStatDaily.objects.for_workspace(tenancy.workspace).get(flow=flow, node_id=ENTRY_NODE)

        client.get(urlsplit(tracking.open_url(workspace_id=tenancy.workspace.pk, idempotency_key=key)).path)

        after = NodeStatDaily.objects.for_workspace(tenancy.workspace).get(flow=flow, node_id=ENTRY_NODE)
        assert (after.clicked, after.sent, after.failed) == (before.clicked, before.sent, before.failed)
        assert (before.delivered, after.delivered) == (0, 1)

    def test_a_token_naming_no_message_still_answers_a_gif(self, client: Any, tenancy: Any) -> None:
        """A 404 here would tell a fetcher which messages exist."""
        path = urlsplit(
            tracking.open_url(workspace_id=tenancy.workspace.pk, idempotency_key="exec:nope:node:n1:0")
        ).path

        response = client.get(path)

        assert response.status_code == 200
        assert response["Content-Type"] == "image/gif"

    def test_a_second_fetch_does_not_walk_the_message_anywhere(
        self, client: Any, tenancy: Any, contact: Any, connection: Any, identity: Any, flow: Any
    ) -> None:
        execution = make_execution(flow, contact, connection)
        key = message_idempotency_key(execution, ENTRY_NODE)
        with registered(Platform.TELEGRAM):
            services.send_outbound(
                workspace=tenancy.workspace,
                contact=contact,
                connection=connection,
                outbound=TEXT,
                source="automation",
                idempotency_key=key,
            )
        path = urlsplit(tracking.open_url(workspace_id=tenancy.workspace.pk, idempotency_key=key)).path

        client.get(path)
        updated_at = Message.objects.for_workspace(tenancy.workspace).get(idempotency_key=key).updated_at
        client.get(path)

        message = Message.objects.for_workspace(tenancy.workspace).get(idempotency_key=key)
        assert message.status == MessageStatus.READ
        assert message.updated_at == updated_at


class TestClickThrottle:
    def test_the_redirect_survives_a_throttled_counter(
        self, client: Any, tenancy: Any, flow: Any, monkeypatch: Any
    ) -> None:
        """Counting is best-effort; redirecting is not. A link in a real message
        must work even when the caller is spraying."""
        from apps.analytics import views_public

        monkeypatch.setattr(views_public, "CLICK_COUNT_LIMIT", 2)
        path = click_path(flow)

        statuses = [client.get(path).status_code for _ in range(5)]

        assert statuses == [302] * 5
        assert clicks(tenancy.workspace, flow) == 2
