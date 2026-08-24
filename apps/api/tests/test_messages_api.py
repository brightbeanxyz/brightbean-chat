"""``POST /api/v1/messages`` — the send endpoint.

Two behaviours worth pinning:

* **The send goes through the facade**, so a message row exists with
  ``source="api"`` and the compliance engine has had its say. Nothing here
  touches the ORM.
* **A compliance refusal is a 422 with a machine-readable reason**, not a 500
  and not a cheerful 201 with a failed row buried in it. ROADMAP contract 1
  makes a denial a *value*; SPEC §17 makes it a status code.
"""

import json

import pytest

from apps.api.tests.conftest import bearer, make_key
from apps.channels.tests.fake_adapter import fake_adapter_for, swapped_adapter
from apps.common.platforms import Platform
from apps.contacts.models import Contact
from apps.messaging.models import Message, MessageSource, MessageStatus
from apps.messaging.services import upsert_contact_identity
from apps.messaging.tests.conftest import make_connection

MESSAGES = "/api/v1/messages"


def send(client, auth, **payload):
    return client.post(MESSAGES, data=json.dumps(payload), content_type="application/json", **auth)


@pytest.fixture
def connection(db, tenancy):
    return make_connection(tenancy.workspace)


@pytest.fixture
def contact(db, tenancy):
    return Contact.objects.create(workspace=tenancy.workspace, first_name="Ada")


@pytest.fixture
def reachable(contact, connection):
    """A contact with a consented identity on ``connection``.

    Without this the compliance engine has nothing to send to and refuses with
    ``no_identity`` — which is a case worth testing too, just not this one.
    """
    upsert_contact_identity(contact, Platform.TELEGRAM, "u-api-1", source="api", opt_in=True, connection=connection)
    return contact


@pytest.mark.django_db
class TestSending:
    def test_it_sends_through_the_facade_with_source_api(self, client, tenancy, auth, reachable, connection):
        adapter = fake_adapter_for(Platform.TELEGRAM)
        with swapped_adapter(Platform.TELEGRAM, adapter):
            response = send(
                client,
                auth,
                contact_id=str(reachable.pk),
                connection_id=str(connection.pk),
                body={"text": "hello from the API"},
            )

        assert response.status_code == 201
        body = response.json()
        assert body["contact_id"] == str(reachable.pk)
        assert body["source"] == MessageSource.API

        message = Message.objects.for_workspace(tenancy.workspace).get(pk=body["id"])
        assert message.source == MessageSource.API
        assert message.status == MessageStatus.SENT
        assert [block["text"] for block in message.body["blocks"]] == ["hello from the API"]

    def test_the_same_idempotency_key_does_not_send_twice(self, client, tenancy, auth, reachable, connection):
        """SPEC §9.4. The key is the caller's whole retry story."""
        adapter = fake_adapter_for(Platform.TELEGRAM)
        payload = {
            "contact_id": str(reachable.pk),
            "connection_id": str(connection.pk),
            "body": {"text": "once"},
            "idempotency_key": "order-4711",
        }
        with swapped_adapter(Platform.TELEGRAM, adapter):
            first = send(client, auth, **payload)
            second = send(client, auth, **payload)

        assert first.status_code == second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert Message.objects.for_workspace(tenancy.workspace).count() == 1

    def test_an_over_long_idempotency_key_is_refused(self, client, tenancy, auth, reachable, connection):
        response = send(
            client,
            auth,
            contact_id=str(reachable.pk),
            connection_id=str(connection.pk),
            body={"text": "hi"},
            idempotency_key="x" * 500,
        )

        assert response.status_code == 422


@pytest.mark.django_db
class TestComplianceDenials:
    def test_no_identity_is_a_422_with_the_reason(self, client, tenancy, auth, contact, connection):
        response = send(
            client,
            auth,
            contact_id=str(contact.pk),
            connection_id=str(connection.pk),
            body={"text": "hello"},
        )

        assert response.status_code == 422
        body = response.json()["error"]
        assert body["code"] == "compliance_denied"
        assert body["detail"]["reason"] == "no_identity"
        # The human sentence comes from the registered copy, not from an
        # f-string at the call site.
        assert body["message"] == "There is no address for this contact on this channel."

    def test_an_opted_out_contact_is_refused(self, client, tenancy, auth, reachable, connection):
        from apps.messaging.models import ContactChannelIdentity
        from apps.messaging.services import record_opt_out

        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get(contact=reachable)
        record_opt_out(identity, source="manual")

        response = send(
            client,
            auth,
            contact_id=str(reachable.pk),
            connection_id=str(connection.pk),
            body={"text": "hello"},
        )

        assert response.status_code == 422
        assert response.json()["error"]["detail"]["reason"] == "opted_out"

    def test_the_denial_still_recorded_a_failed_row(self, client, tenancy, auth, contact, connection):
        """A 422 is not "nothing happened".

        The facade inserts the row first and marks it failed, which is what
        makes the refusal auditable. The error body names the row so a caller
        can correlate it.
        """
        response = send(
            client,
            auth,
            contact_id=str(contact.pk),
            connection_id=str(connection.pk),
            body={"text": "hello"},
        )

        message_id = response.json()["error"]["detail"]["message_id"]
        message = Message.objects.for_workspace(tenancy.workspace).get(pk=message_id)
        assert message.status == MessageStatus.FAILED


@pytest.mark.django_db
class TestWithdrawnMessages:
    """``Failure.WITHDRAWN`` is not a ``Denial`` — it lives in the other code
    vocabulary, for a cancellation rather than a compliance refusal — but it
    means exactly the same thing to a caller polling this endpoint: nobody is
    ever retrying it. A response that told them otherwise (this router's
    documented 201-means-still-retrying contract) would be actively wrong.
    """

    def test_a_withdrawn_message_is_a_422_not_a_201_on_retry(self, client, tenancy, auth, reachable, connection):
        from apps.messaging.services import withdraw_send

        adapter = fake_adapter_for(Platform.TELEGRAM)
        payload = {
            "contact_id": str(reachable.pk),
            "connection_id": str(connection.pk),
            "body": {"text": "hello"},
            "idempotency_key": "order-withdrawn-1",
        }
        with swapped_adapter(Platform.TELEGRAM, adapter):
            first = send(client, auth, **payload)
        assert first.status_code == 201
        message = Message.objects.for_workspace(tenancy.workspace).get(pk=first.json()["id"])

        # Force it to the state withdraw_send targets — queued, unclaimed,
        # which is what a rate-deferred send looks like — without needing to
        # actually drain the token bucket for this test.
        Message.objects.for_workspace(tenancy.workspace).filter(pk=message.pk).update(
            status=MessageStatus.QUEUED, dispatched_at=None
        )
        message.refresh_from_db()
        withdraw_send(message, reason="broadcast_cancelled")

        # Same idempotency key: SPEC §9.4's ordinary retry path, which is how
        # a caller actually discovers this — not the first response, which it
        # never sees, but a later poll on a key it already holds.
        second = send(client, auth, **payload)

        assert second.status_code == 422
        body = second.json()["error"]
        assert body["code"] == "withdrawn"
        assert body["detail"]["reason"] == "withdrawn"
        assert body["detail"]["message_id"] == str(message.pk)
        assert body["message"] == "The work that queued this message was cancelled before it was sent."


@pytest.mark.django_db
class TestTenancyAndScopes:
    def test_another_workspaces_contact_is_a_404(self, client, tenancy, other_tenancy, auth, connection):
        stranger = Contact.objects.create(workspace=other_tenancy.workspace, first_name="Stranger")

        response = send(
            client,
            auth,
            contact_id=str(stranger.pk),
            connection_id=str(connection.pk),
            body={"text": "hello"},
        )

        assert response.status_code == 404

    def test_another_workspaces_connection_is_a_404(self, client, tenancy, other_tenancy, auth, contact):
        theirs = make_connection(other_tenancy.workspace, suffix="theirs")

        response = send(
            client,
            auth,
            contact_id=str(contact.pk),
            connection_id=str(theirs.pk),
            body={"text": "hello"},
        )

        assert response.status_code == 404

    def test_a_read_key_cannot_send(self, client, tenancy, contact, connection):
        _, plaintext = make_key(tenancy.workspace, scopes=("read",))

        response = send(
            client,
            bearer(plaintext),
            contact_id=str(contact.pk),
            connection_id=str(connection.pk),
            body={"text": "hello"},
        )

        assert response.status_code == 403
