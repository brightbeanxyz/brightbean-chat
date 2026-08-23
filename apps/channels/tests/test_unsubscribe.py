"""The ``/u/<token>`` route: one click, forever-valid tokens, generic 404s.

**This class also stands in for the IDOR sweep on this route**, and that is a
deliberate, stated position rather than an oversight.

``tests/idor.py::iter_tenant_routes`` skips any route carrying no *registered*
tenant kwarg — before the unknown-kwarg check, so ``/u/<str:token>/`` neither
raises ``UnregisteredRouteKwargError`` nor gets swept, and no ``WAIVED_ROUTES``
entry is required or would do anything. It is the same position ``/m/<token>/``
(media delivery) already occupies, and the reasoning is the same: the route
identifies no tenant object. It takes a signed capability, and the only question
worth asking of it is whether holding a *different* tenant's capability, a
tampered one, or none at all is distinguishable from holding a valid one. That
is what ``TestTheTokenIsTheWholeCredential`` asserts. If it is ever deleted, this
route needs a different answer.
"""

from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.channels.models import ChannelConnection, EmailSuppression, SuppressionReason
from apps.channels.unsubscribe import ACCEPTED_VERSIONS, IDENTITY_KEY, PURPOSE, mint_token, unsubscribe_url
from apps.common.platforms import Platform
from apps.common.signing import sign
from apps.contacts.models import Contact
from apps.messaging.models import ContactChannelIdentity

pytestmark = pytest.mark.django_db


@pytest.fixture
def email_connection(tenancy: Any) -> ChannelConnection:
    connection = ChannelConnection(
        workspace=tenancy.workspace,
        platform=Platform.EMAIL.value,
        display_name="Sender",
        external_id="sender.test",
    )
    connection.credentials = {"provider": "smtp", "from_address": "hello@sender.test"}  # type: ignore[assignment]
    connection.save()
    return connection


@pytest.fixture
def identity(tenancy: Any, email_connection: ChannelConnection) -> ContactChannelIdentity:
    contact = Contact.objects.create(workspace=tenancy.workspace, email="reader@example.test")
    return ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=email_connection,
        platform=Platform.EMAIL.value,
        platform_user_id="reader@example.test",
        opt_in=True,
        opt_in_at=timezone.now(),
        opt_in_source="data_collection",
    )


def suppressed(workspace: Any) -> set[str]:
    return set(EmailSuppression.objects.for_workspace(workspace).values_list("address", flat=True))


class TestOneClick:
    """SPEC §21: "unsubscribe link suppresses email within one click"."""

    def test_the_get_shows_a_confirm_page_and_changes_nothing(
        self, client: Client, identity: ContactChannelIdentity
    ) -> None:
        """A destructive GET would fire on every link scanner that prefetches.

        Corporate gateways, Outlook Safe Links and antivirus plugins fetch every
        URL in a message before a human sees it, so a GET that unsubscribed
        would silently unsubscribe a share of every list on delivery.
        """
        response = client.get(f"/u/{mint_token(identity)}/")

        assert response.status_code == 200
        assert b"reader@example.test" in response.content
        identity.refresh_from_db()
        assert identity.opted_out_at is None
        assert suppressed(identity.workspace) == set()

    def test_the_post_unsubscribes(self, client: Client, identity: ContactChannelIdentity) -> None:
        response = client.post(f"/u/{mint_token(identity)}/")

        assert response.status_code == 200
        identity.refresh_from_db()
        assert identity.opted_out_at is not None
        assert identity.opt_in is False
        assert suppressed(identity.workspace) == {"reader@example.test"}

    def test_the_suppression_records_why(self, client: Client, identity: ContactChannelIdentity) -> None:
        client.post(f"/u/{mint_token(identity)}/")
        row = EmailSuppression.objects.for_workspace(identity.workspace).get(address="reader@example.test")
        assert row.reason == SuppressionReason.UNSUBSCRIBE
        assert row.connection_id == identity.channel_connection_id

    def test_a_second_click_is_idempotent(self, client: Client, identity: ContactChannelIdentity) -> None:
        """The first refusal is the one that counts — the audit must not move."""
        token = mint_token(identity)
        client.post(f"/u/{token}/")
        identity.refresh_from_db()
        first = identity.opted_out_at

        assert client.post(f"/u/{token}/").status_code == 200
        identity.refresh_from_db()
        assert identity.opted_out_at == first
        assert EmailSuppression.objects.for_workspace(identity.workspace).count() == 1

    def test_the_page_says_so_when_already_unsubscribed(self, client: Client, identity: ContactChannelIdentity) -> None:
        token = mint_token(identity)
        client.post(f"/u/{token}/")
        assert b"Already unsubscribed" in client.get(f"/u/{token}/").content

    def test_a_get_needs_no_csrf_token_to_post_back(self, client: Client, identity: ContactChannelIdentity) -> None:
        """The recipient has no session here; the signed token is the credential."""
        enforcing = Client(enforce_csrf_checks=True)
        assert enforcing.post(f"/u/{mint_token(identity)}/").status_code == 200


class TestRFC8058OneClick:
    """``List-Unsubscribe-Post`` is what makes Gmail show the one-click button."""

    def test_a_one_click_post_unsubscribes_with_no_page(self, client: Client, identity: ContactChannelIdentity) -> None:
        response = client.post(f"/u/{mint_token(identity)}/", data={"List-Unsubscribe": "One-Click"})

        assert response.status_code == 200
        # RFC 8058 §3.2: the client wants a 2xx and nothing else, and some treat
        # a large body as a failure.
        assert response.content == b""
        identity.refresh_from_db()
        assert identity.opted_out_at is not None

    def test_it_still_suppresses_the_address(self, client: Client, identity: ContactChannelIdentity) -> None:
        client.post(f"/u/{mint_token(identity)}/", data={"List-Unsubscribe": "One-Click"})
        assert suppressed(identity.workspace) == {"reader@example.test"}


class TestTheTokenIsTheWholeCredential:
    """The stand-in for the IDOR sweep. See this module's docstring."""

    @pytest.mark.parametrize(
        "token",
        [
            "",
            "not-a-token",
            "aaaa.bbbb.cccc",
            "x" * 500,
            "../../etc/passwd",
            "%2e%2e%2f",
        ],
    )
    def test_a_junk_token_is_a_bare_404(self, client: Client, token: str) -> None:
        assert client.get(f"/u/{token}/").status_code == 404

    def test_a_tampered_token_is_a_404(self, client: Client, identity: ContactChannelIdentity) -> None:
        token = mint_token(identity)
        tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        assert client.get(f"/u/{tampered}/").status_code == 404
        identity.refresh_from_db()
        assert identity.opted_out_at is None

    def test_a_token_minted_for_another_purpose_is_a_404(
        self, client: Client, identity: ContactChannelIdentity
    ) -> None:
        """The purpose is the signer salt, so a media token cannot be replayed here."""
        foreign = sign({IDENTITY_KEY: str(identity.pk)}, purpose="media-delivery")
        assert client.get(f"/u/{foreign}/").status_code == 404

    def test_an_unknown_payload_version_is_a_404(self, client: Client, identity: ContactChannelIdentity) -> None:
        future = sign({IDENTITY_KEY: str(identity.pk)}, purpose=PURPOSE, version=99)
        assert client.get(f"/u/{future}/").status_code == 404

    def test_a_v1_token_for_a_deleted_identity_is_a_404(self, client: Client, identity: ContactChannelIdentity) -> None:
        """v1 carried the identity alone, so nothing is left to act on.

        A v2 token deliberately survives this — see
        ``TestTokensNeverExpire::test_a_link_outlives_the_connection_it_was_sent_from``.
        """
        token = sign({IDENTITY_KEY: str(identity.pk)}, purpose=PURPOSE, version=1)
        identity.delete()
        assert client.get(f"/u/{token}/").status_code == 404

    def test_a_token_naming_a_non_email_identity_is_a_404(self, client: Client, tenancy: Any) -> None:
        """An unsubscribe page speaks about a mailbox, so it refuses anything else."""
        telegram = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM.value,
            display_name="Bot",
            external_id="bot-1",
        )
        contact = Contact.objects.create(workspace=tenancy.workspace, email="a@b.test")
        other = ContactChannelIdentity.objects.create(
            contact=contact,
            channel_connection=telegram,
            platform=Platform.TELEGRAM.value,
            platform_user_id="555",
        )
        assert client.get(f"/u/{mint_token(other)}/").status_code == 404

    def test_a_signed_token_whose_payload_is_not_a_uuid_is_a_404(self, client: Client) -> None:
        """Only reachable with our own key, so it is a bug — but never a 500."""
        token = sign({IDENTITY_KEY: "not-a-uuid"}, purpose=PURPOSE, version=1)
        assert client.get(f"/u/{token}/").status_code == 404

    def test_every_rejection_looks_the_same(self, client: Client, identity: ContactChannelIdentity) -> None:
        """No error text, no distinguishable status: a caller learns nothing."""
        token = mint_token(identity)
        rejections = [
            client.get("/u/garbage/"),
            client.get(f"/u/{sign({IDENTITY_KEY: str(identity.pk)}, purpose='flow-preview')}/"),
            client.get(f"/u/{token[:-3]}zzz/"),
        ]
        assert {response.status_code for response in rejections} == {404}
        assert len({response.content for response in rejections}) == 1


class TestTokensNeverExpire:
    """``max_age=None``: an unsubscribe link that 404s is a compliance problem."""

    def test_the_route_does_not_pass_a_max_age(self, identity: ContactChannelIdentity) -> None:
        from apps.common import signing

        seen: dict[str, Any] = {}
        original = signing.unsign

        def recording(token: str, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return original(token, **kwargs)

        signing.unsign = recording  # type: ignore[assignment]
        try:
            from apps.channels.unsubscribe import target_from_token

            target_from_token(mint_token(identity))
        finally:
            signing.unsign = original  # type: ignore[assignment]
        assert seen["max_age"] is None

    def test_a_v1_token_still_resolves_after_the_payload_shape_changes(
        self, client: Client, identity: ContactChannelIdentity, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Layer-5 gate item, asserted rather than promised.

        A token minted today sits in an inbox forever. v2 has shipped, so this
        simulates the day *after* the next shape change: minting moves forward,
        ``ACCEPTED_VERSIONS`` grows, and the links somebody received under v1 and
        v2 both still work. A cutover would turn every one of them into a 404 on
        the day of the deploy.
        """
        v1_token = sign({IDENTITY_KEY: str(identity.pk)}, purpose=PURPOSE, version=1)
        v2_token = mint_token(identity)

        from apps.channels import unsubscribe as unsubscribe_module

        monkeypatch.setattr(unsubscribe_module, "MINT_VERSION", 3)
        monkeypatch.setattr(unsubscribe_module, "ACCEPTED_VERSIONS", (1, 2, 3))
        v3_token = mint_token(identity)
        assert len({v1_token, v2_token, v3_token}) == 3

        for token in (v1_token, v2_token, v3_token):
            assert client.get(f"/u/{token}/").status_code == 200

    def test_a_link_outlives_the_connection_it_was_sent_from(
        self, client: Client, identity: ContactChannelIdentity
    ) -> None:
        """Disconnecting a channel cascades its identities away.

        Every unsubscribe link already in an inbox would then 404 — the exact
        failure `max_age=None` exists to prevent, arriving by a different door.
        """
        workspace = identity.workspace
        token = mint_token(identity)
        connection = identity.channel_connection
        assert connection is not None
        connection.delete()
        assert not ContactChannelIdentity.objects.for_workspace(workspace).exists()

        assert client.get(f"/u/{token}/").status_code == 200
        assert client.post(f"/u/{token}/").status_code == 200
        assert suppressed(workspace) == {"reader@example.test"}

    def test_a_v1_token_still_resolves(self, client: Client, identity: ContactChannelIdentity) -> None:
        """Minted before the payload grew, and never expiring."""
        token = sign({IDENTITY_KEY: str(identity.pk)}, purpose=PURPOSE, version=1)

        assert client.get(f"/u/{token}/").status_code == 200
        assert client.post(f"/u/{token}/").status_code == 200
        identity.refresh_from_db()
        assert identity.opted_out_at is not None

    def test_the_accepted_versions_include_the_minted_one(self) -> None:
        from apps.channels.unsubscribe import MINT_VERSION

        assert MINT_VERSION in ACCEPTED_VERSIONS


class TestTheLink:
    def test_it_is_absolute_and_built_from_app_url(self, identity: ContactChannelIdentity, settings: Any) -> None:
        """The send path runs in a worker, where there is no request to read."""
        settings.APP_URL = "https://mail.example.test"
        assert unsubscribe_url(identity).startswith("https://mail.example.test/u/")

    def test_the_token_carries_the_mailbox(self, identity: ContactChannelIdentity) -> None:
        """Deliberate, and the reason a link survives its connection being deleted.

        Signed, not encrypted, so this is readable by anyone holding the link —
        which is already anyone who can read the message it was delivered in.
        """
        from apps.channels.unsubscribe import target_from_token

        target = target_from_token(mint_token(identity))
        assert target.address == "reader@example.test"
        assert target.workspace_id == str(identity.workspace_id)
