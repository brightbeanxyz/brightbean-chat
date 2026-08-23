"""The two doors L5-D added to contract 1, and what each of them is *not*.

``record_opt_in``
    The way back in after a hard opt-out. It exists because
    ``record_consent`` deliberately never clears ``opted_out_at`` and because
    ROADMAP contract 3 gives that column one write site — so re-consent had to
    arrive through ``ingest.apply_opt_in`` or not at all. Reachable from a
    channel adapter's re-subscribe keyword and pointedly not from the CRM: SPEC
    §19 puts opt-out at a chokepoint so it cannot be bypassed, and a team member
    who could un-say it would be a bypass with a nicer name.

``send_compliance_reply``
    The one sanctioned way past ``can_send``. A carrier requires an SMS ``STOP``
    to be confirmed and a ``HELP`` to be answered, both after the identity is
    already suppressed. It skips the verdict and **nothing else** — the message
    row, the idempotency key, the token bucket and the tombstone check all still
    apply, which is what these tests are mostly about.

Neither carries any platform knowledge. ``grep -rn sms apps/messaging/`` is
asserted clean by ``test_no_platform_branches`` below, which is contract 4's
promise that a Layer-5 platform costs one module and one registry line.
"""

from pathlib import Path
from typing import Any

import pytest
from django.utils import timezone

from apps.channels.events import OutboundMessage, TextBlock
from apps.channels.tests.fake_adapter import registered
from apps.common.platforms import Platform
from apps.messaging import services
from apps.messaging.ingest import apply_opt_in, apply_opt_out
from apps.messaging.models import (
    ContactChannelIdentity,
    Message,
    MessageDirection,
    MessageSource,
    MessageStatus,
    OptInSource,
)
from apps.messaging.tests.conftest import make_connection

pytestmark = pytest.mark.django_db

HELLO = OutboundMessage(blocks=(TextBlock(text="You are unsubscribed."),))


@pytest.fixture
def sms(tenancy: Any) -> Any:
    return make_connection(tenancy.workspace, platform=Platform.SMS, suffix="doors")


@pytest.fixture
def identity(tenancy: Any, sms: Any) -> ContactChannelIdentity:
    from apps.messaging.identities import resolve_identity

    resolution = resolve_identity(sms, "+15557778888")
    row = resolution.identity
    row.opt_in = True
    row.opt_in_at = timezone.now()
    row.opt_in_source = OptInSource.MESSAGE_IN
    row.save(update_fields=["opt_in", "opt_in_at", "opt_in_source", "updated_at"])
    return row


class TestRecordOptIn:
    def test_it_clears_a_hard_opt_out(self, identity: ContactChannelIdentity) -> None:
        apply_opt_out(identity)

        assert services.record_opt_in(identity) is True

        identity.refresh_from_db()
        assert identity.opted_out_at is None
        assert identity.opt_in is True

    def test_it_re_stamps_the_consent_audit(self, identity: ContactChannelIdentity) -> None:
        """Unlike the first time round. ``record_consent`` writes ``opt_in_at``
        once because "when was permission given" does not change when permission
        is merely exercised again — but here it was withdrawn and given afresh,
        and a regulator asking why we are messaging this number after a STOP
        wants today's date."""
        original = identity.opt_in_at
        assert original is not None, "the fixture stamps it"
        apply_opt_out(identity)

        services.record_opt_in(identity, source=OptInSource.MESSAGE_IN)

        identity.refresh_from_db()
        assert identity.opt_in_at is not None
        assert identity.opt_in_at > original
        assert identity.opt_in_source == OptInSource.MESSAGE_IN

    def test_it_is_idempotent(self, identity: ContactChannelIdentity) -> None:
        apply_opt_out(identity)
        services.record_opt_in(identity)

        assert services.record_opt_in(identity) is False

    def test_it_reports_no_change_for_an_identity_that_never_opted_out(self, identity: ContactChannelIdentity) -> None:
        assert services.record_opt_in(identity) is False

    def test_it_also_repairs_an_identity_that_was_merely_not_opted_in(self, identity: ContactChannelIdentity) -> None:
        """A follow creates one of these (``opt_in=False``, never opted out).
        A contact who then texts START has given consent."""
        identity.opt_in = False
        identity.save(update_fields=["opt_in", "updated_at"])

        assert services.record_opt_in(identity) is True
        identity.refresh_from_db()
        assert identity.opt_in is True

    def test_it_is_the_only_thing_that_can_undo_an_opt_out(self) -> None:
        """``record_opt_out`` has no matching toggle, deliberately (SPEC §19).
        Stated as a test so a later PR adding one has to argue with it."""
        assert not hasattr(services, "clear_opt_out")
        assert not hasattr(services, "undo_opt_out")

    def test_the_column_still_has_exactly_one_writer(self) -> None:
        """``apps/messaging/tests/test_write_sites.py`` runs the real AST scan;
        this is the human-readable half of the same claim."""
        from apps.messaging.tests.test_write_sites import WRITE_SITES

        assert WRITE_SITES["opted_out_at"] == {"messaging/ingest.py"}
        assert "opted_out_at" in Path(apply_opt_in.__code__.co_filename).read_text()


class TestSendComplianceReply:
    def test_it_sends_to_an_opted_out_identity(self, tenancy: Any, sms: Any, identity: Any) -> None:
        apply_opt_out(identity)

        with registered(Platform.SMS):
            message = services.send_compliance_reply(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=sms,
                outbound=HELLO,
                idempotency_key="stop-1",
            )

        assert message.status == MessageStatus.SENT
        assert message.error == ""

    def test_an_ordinary_send_to_the_same_identity_is_blocked(self, tenancy: Any, sms: Any, identity: Any) -> None:
        """The contrast that makes the door meaningful."""
        apply_opt_out(identity)

        with registered(Platform.SMS):
            blocked = services.send_outbound(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=sms,
                outbound=HELLO,
                source=MessageSource.AUTOMATION.value,
                idempotency_key="ordinary-1",
            )

        assert blocked.status == MessageStatus.FAILED
        assert blocked.error == "opted_out"

    def test_it_writes_a_message_row_in_the_thread(self, tenancy: Any, sms: Any, identity: Any) -> None:
        """Without this the confirmation is invisible: an agent reading the
        conversation would see STOP and no answer."""
        with registered(Platform.SMS):
            services.send_compliance_reply(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=sms,
                outbound=HELLO,
                idempotency_key="stop-1",
            )

        row = Message.objects.for_workspace(tenancy.workspace).get(direction=MessageDirection.OUT)
        assert row.body["blocks"][0]["text"] == "You are unsubscribed."
        assert row.source == MessageSource.AUTOMATION

    def test_it_is_idempotent(self, tenancy: Any, sms: Any, identity: Any) -> None:
        """A redelivered STOP must not produce a second confirmation."""
        with registered(Platform.SMS) as adapter:
            for _ in range(2):
                services.send_compliance_reply(
                    workspace=tenancy.workspace,
                    contact=identity.contact,
                    connection=sms,
                    outbound=HELLO,
                    idempotency_key="stop-1",
                )

            assert len(adapter.sends) == 1
        assert Message.objects.for_workspace(tenancy.workspace).filter(direction=MessageDirection.OUT).count() == 1

    def test_it_still_refuses_a_send_with_no_identity(self, tenancy: Any, sms: Any) -> None:
        """Being mandatory does not conjure an address to send it to."""
        from apps.contacts.services import create_contact

        contact = create_contact(tenancy.workspace, source="manual")

        with registered(Platform.SMS):
            message = services.send_compliance_reply(
                workspace=tenancy.workspace,
                contact=contact,
                connection=sms,
                outbound=HELLO,
                idempotency_key="stop-1",
            )

        assert message.status == MessageStatus.FAILED
        assert message.error == "no_identity"

    def test_it_still_refuses_a_deleted_contact(self, tenancy: Any, sms: Any, identity: Any) -> None:
        from apps.contacts.models import ContactStatus

        identity.contact.status = ContactStatus.DELETED
        identity.contact.save(update_fields=["status"])

        with registered(Platform.SMS):
            message = services.send_compliance_reply(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=sms,
                outbound=HELLO,
                idempotency_key="stop-1",
            )

        assert message.status == MessageStatus.FAILED
        assert message.error == "contact_deleted"

    def test_it_still_takes_a_rate_token(self, tenancy: Any, sms: Any, identity: Any) -> None:
        from apps.messaging.models import SendBucket

        with registered(Platform.SMS):
            services.send_compliance_reply(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=sms,
                outbound=HELLO,
                idempotency_key="stop-1",
            )

        assert SendBucket.objects.filter(connection=sms).exists()

    def test_it_still_refuses_a_blank_idempotency_key(self, tenancy: Any, sms: Any, identity: Any) -> None:
        """The unique constraint is partial, so a blank key deduplicates nothing
        — every call would insert a row and make a fresh provider call."""
        with pytest.raises(ValueError, match="idempotency_key"):
            services.send_compliance_reply(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=sms,
                outbound=HELLO,
                idempotency_key="",
            )

    def test_it_never_pauses_automation(self, tenancy: Any, sms: Any, identity: Any) -> None:
        """``source="agent"`` would pause automation for thirty minutes because
        somebody typed STOP. Nobody has taken anything over."""
        with registered(Platform.SMS):
            services.send_compliance_reply(
                workspace=tenancy.workspace,
                contact=identity.contact,
                connection=sms,
                outbound=HELLO,
                idempotency_key="stop-1",
            )

        conversation = services.open_conversation(workspace=tenancy.workspace, contact=identity.contact, connection=sms)
        assert conversation.automation_paused_until is None


class TestNoPlatformBranches:
    """Contract 4, asserted the way the layer prompt words it."""

    def test_the_messaging_app_names_no_platform(self) -> None:
        root = Path(services.__file__).parent
        offenders = []
        for path in root.rglob("*.py"):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            body = path.read_text().lower()
            for word in ("twilio", "telegram", "whatsapp", "messenger", "instagram"):
                # A docstring example naming a platform is not a branch; a
                # *comparison* against one is. Both are caught by grep, so this
                # narrows to the shape that would actually be a branch.
                for form in (f'== "{word}"', f"== '{word}'", f'platform == "{word}"'):
                    if form in body:
                        offenders.append(f"{path.name}: {form}")
        assert offenders == []

    def test_nothing_here_imports_an_adapter(self) -> None:
        """The structural half. ``apps.messaging`` reads contract 4's tables —
        ``policy``, ``capabilities`` — and reaches an adapter only through
        ``registry.adapter_for``. The shared pieces of ``providers`` are fair
        game (``exceptions``, and ``base`` for the ABC); a *concrete* adapter
        module is not, and importing one would be both the cycle the split
        exists to avoid and the first step towards a branch."""
        root = Path(services.__file__).parent
        concrete = [f"providers.{name}" for name in Platform.values]
        offenders = [
            f"{path.name}: {module}"
            for path in root.rglob("*.py")
            if "tests" not in path.parts
            for module in concrete
            if module in path.read_text()
        ]
        assert offenders == []

    def test_the_two_new_doors_name_no_platform(self) -> None:
        """The contract-4 claim for this PR specifically. ``apps/messaging``
        already mentions SMS in prose — ``identities.ADDRESS_PLATFORMS`` has to
        know that a phone number is an address — so a bare grep would be a test
        of the past. What must be true is that the code L5-D added carries no
        platform knowledge at all.
        """
        import inspect

        for door in (services.send_compliance_reply, services.record_opt_in, apply_opt_in):
            body = "\n".join(
                line
                for line in inspect.getsource(door).splitlines()
                # Docstrings may name SMS — they explain which issue needed the
                # door. Executable lines may not.
                if not line.strip().startswith(("#", '"""', "*", "``"))
            )
            for word in ("sms", "twilio", "telegram", "whatsapp"):
                assert f'"{word}"' not in body.lower()
                assert f"Platform.{word.upper()}" not in body
