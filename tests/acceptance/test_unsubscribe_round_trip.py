"""SPEC §21 phase 2: "unsubscribe link suppresses email within one click".

Both halves of this were already tested, and they were never joined.

``apps/channels/tests/test_unsubscribe.py`` proves that POSTing a ``/u/`` token
opts the identity out and writes an ``EmailSuppression`` row — but the token it
posts was minted by the test, with ``mint_token(identity)``, and never travelled
in a message. ``apps/channels/tests/test_email_suppression.py`` proves that a
suppressed address never reaches ``email_backends.deliver`` — but it starts from
``suppress_and_opt_out()``, not from a click.

So nothing proved the sentence the spec actually writes: that the link *in a
real message* is the one that stops the next one. The gap is not theoretical.
The header could name a token minted for a different identity, the footer could
disagree with the header, or the URL could point somewhere the URLConf does not
route, and every existing test would stay green.

This joins them, end to end and in that order:

    send a real message -> harvest the link off the envelope -> click it
    -> send again -> refused, with the transport never reached.

The one thing standing in for real infrastructure is the SMTP/API transport,
replaced at ``email_backends.deliver`` with a **recording** spy rather than a
raising one — the envelope it captures is the whole point of step two, and a
spy that explodes could not hand it back. The compliance engine, the token
signer, the suppression list and the messaging facade are all the production
paths.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import pytest
from django.test import Client
from django.utils import timezone

from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.models import ChannelConnection, EmailSuppression, SuppressionReason
from apps.channels.providers import email_backends
from apps.channels.suppression import is_suppressed
from apps.common.platforms import Platform
from apps.contacts.services import create_contact
from apps.messaging import services
from apps.messaging.codes import Denial
from apps.messaging.models import ContactChannelIdentity, MessageSource, MessageStatus

pytestmark = pytest.mark.django_db

READER = "reader@example.test"


@pytest.fixture
def connection(tenancy: Any) -> ChannelConnection:
    row = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.EMAIL.value,
        display_name="Sender",
        external_id="sender.test",
    )
    row.credentials = {  # type: ignore[assignment]
        "provider": "smtp",
        "host": "mail.test",
        "security": "none",
        "from_address": "hello@sender.test",
    }
    row.save()
    return row


def identity_for(workspace: Any, connection: ChannelConnection, address: str) -> ContactChannelIdentity:
    contact = create_contact(workspace, source="manual", email=address)
    return ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=connection,
        platform=Platform.EMAIL.value,
        platform_user_id=address,
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source="data_collection",
    )


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[email_backends.Envelope]:
    """Everything that reached the transport, in order.

    Patched on the module object because ``EmailAdapter.send`` calls
    ``email_backends.deliver(...)`` through it, so this is the same door the
    real send uses.
    """
    captured: list[email_backends.Envelope] = []

    def spy(connection: Any, envelope: email_backends.Envelope) -> str:
        captured.append(envelope)
        return f"spy-{len(captured)}"

    monkeypatch.setattr(email_backends, "deliver", spy)
    return captured


def send(workspace: Any, contact: Any, connection: ChannelConnection, *, key: str) -> Any:
    """One automated email through contract 1's facade — never the adapter directly.

    Going through ``send_outbound`` is what makes the refusal in step four mean
    something: it runs ``can_send`` before anything touches the adapter, which is
    the chokepoint SPEC §19 puts opt-out behind so "it cannot be bypassed".
    """
    return services.send_outbound(
        workspace=workspace,
        contact=contact,
        connection=connection,
        outbound=OutboundMessage(blocks=(TextBlock(text="Your weekly digest."),), subject="Digest"),
        source=MessageSource.AUTOMATION.value,
        idempotency_key=key,
    )


class TestOneClickUnsubscribe:
    """The whole criterion, as one scenario."""

    @pytest.mark.parametrize(
        ("data", "label"),
        [
            ({"List-Unsubscribe": "One-Click"}, "rfc8058"),
            ({}, "human"),
        ],
    )
    def test_the_link_in_a_sent_message_suppresses_the_next_one(
        self,
        client: Client,
        tenancy: Any,
        connection: ChannelConnection,
        posted: list[email_backends.Envelope],
        data: dict[str, str],
        label: str,
    ) -> None:
        """Both click shapes: a mail client's one-click POST, and a human's.

        RFC 8058 answers a bare 200 with an empty body and a human gets a page,
        but the effect on the mailbox is identical — which is worth pinning,
        because a divergence there is a compliance failure that only shows up in
        one of the two paths people actually use.
        """
        identity = identity_for(tenancy.workspace, connection, READER)
        contact = identity.contact

        first = send(tenancy.workspace, contact, connection, key=f"acceptance-{label}-1")
        assert first.status == MessageStatus.SENT
        assert len(posted) == 1, "the first send must actually reach the transport"

        # --- The link, as a recipient receives it -------------------------
        envelope = posted[0]
        assert envelope.to == READER
        assert envelope.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        link = envelope.headers["List-Unsubscribe"].strip("<>")
        assert link in envelope.html, (
            "the footer link and the List-Unsubscribe header must name the same token — a recipient who "
            "clicks the visible link and one whose client uses the header have to reach the same place"
        )

        # ``.path`` rather than the whole URL: the token is what is being
        # tested, and the host comes from APP_URL, which a deployment sets.
        path = urlparse(link).path

        # --- One click ----------------------------------------------------
        response = client.post(path, data=data)
        assert response.status_code == 200

        identity.refresh_from_db()
        assert identity.opted_out_at is not None, "the harvested token did not opt out the identity it was minted for"
        assert identity.opt_in is False
        assert is_suppressed(tenancy.workspace.pk, READER)
        row = EmailSuppression.objects.for_workspace(tenancy.workspace).get()
        assert row.address == READER
        assert row.reason == SuppressionReason.UNSUBSCRIBE

        # --- And the next message does not go ------------------------------
        # A *different* idempotency key. Reusing the first one would be
        # deduplicated by the send pipeline and return the original row, and the
        # test would pass while proving nothing at all.
        second = send(tenancy.workspace, contact, connection, key=f"acceptance-{label}-2")
        assert second.status == MessageStatus.FAILED
        assert second.error == Denial.OPTED_OUT.value
        assert len(posted) == 1, "a suppressed recipient must not reach the transport a second time"

    def test_the_click_also_stops_a_second_contact_at_the_same_address(
        self,
        client: Client,
        tenancy: Any,
        connection: ChannelConnection,
        posted: list[email_backends.Envelope],
    ) -> None:
        """The reason suppression is keyed on the mailbox and not on the identity.

        This one is refused a layer lower than the test above — the second
        contact's identity knows nothing about the click, so ``can_send`` has no
        opinion and the adapter's own suppression check is what stops it. Both
        doors matter: a CSV re-import, a merge or a GDPR erasure all produce
        exactly this state, and the person who unsubscribed does not care which
        row we happened to look at.
        """
        first = identity_for(tenancy.workspace, connection, READER)
        send(tenancy.workspace, first.contact, connection, key="acceptance-shared-1")
        link = posted[0].headers["List-Unsubscribe"].strip("<>")
        assert client.post(urlparse(link).path, data={"List-Unsubscribe": "One-Click"}).status_code == 200

        other = identity_for(tenancy.workspace, connection, READER.upper())
        assert other.contact != first.contact

        result = send(tenancy.workspace, other.contact, connection, key="acceptance-shared-2")
        assert result.status == MessageStatus.FAILED
        assert len(posted) == 1

        # Not the bare ``opted_out`` the test above gets. That one is the
        # compliance engine's verdict before the adapter is reached; this one is
        # the adapter's own, relayed by the send pipeline with the prefix it
        # gives anything a provider decided. Asserting the two separately is
        # what proves both doors are shut rather than one door twice.
        assert result.error == f"provider_rejected:{Denial.OPTED_OUT.value}"

        # And the refusal repairs the identity on its way out, so the *next*
        # send is caught by the compliance chokepoint instead of the adapter.
        other.refresh_from_db()
        assert other.opted_out_at is not None
        third = send(tenancy.workspace, other.contact, connection, key="acceptance-shared-3")
        assert third.error == Denial.OPTED_OUT.value
        assert len(posted) == 1
