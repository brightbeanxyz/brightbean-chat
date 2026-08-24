"""The IDOR fuzz suite (SECURITY-BASELINE §1).

**Every PR that adds an endpoint extends this file.** That is not a convention
kept by good intentions: :func:`iter_tenant_routes` walks the *registered URL
patterns*, and a route carrying a tenant-shaped id it does not know how to build
raises rather than being skipped. Adding ``/w/<uuid:workspace_id>/contacts/<uuid:contact_id>/``
without registering a ``contact_id`` resolver turns the suite red, which is the
whole mechanism.

What it proves: hitting a route with **another tenant's** object ids, as a fully
privileged member of a different organization, answers **404** — never 403, never
200. A 403 would confirm the id names something real, which over a UUID space is
the only information an attacker was missing.

The contract per route, per method:

* every method answers 404 or 405, and
* at least one method answers 404.

The 405 allowance is for POST-only views: the stacking convention puts
``@require_POST`` innermost, so a GET is rejected on the method before the view
body runs — and a 405 there is returned for real and fake ids alike, so it tells
an attacker nothing. Requiring at least one 404 is what stops a route from
passing merely because nothing ever reached it.

Routes with no tenant-identifying kwarg (``/organization/members/``, say) are
not URL-addressable across tenants at all: they operate on ``request.org``,
which the middleware resolves from the signed-in user. They are skipped here and
covered by the per-app permission tests.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from django.urls import URLPattern, URLResolver, get_resolver, reverse

from tests.support import Tenancy

# ---------------------------------------------------------------------------
# Registry — the part later PRs extend
# ---------------------------------------------------------------------------

#: URL kwargs whose value names an object owned by one tenant. A route carrying
#: any of these is fuzzed. Each resolver returns the **victim's** value.
TENANT_KWARG_RESOLVERS: dict[str, Callable[[Tenancy], Any]] = {
    "workspace_id": lambda t: t.workspace.pk,
    # apps.organizations.views.set_workspace_archived deliberately avoids the
    # name ``workspace_id`` so RBACMiddleware does not 404 archived workspaces
    # before the view can restore them; it scopes to request.org instead.
    "target_id": lambda t: t.workspace.pk,
    "membership_id": lambda t: t.org_membership.pk,
    "invitation_id": lambda t: _victim_invitation(t).pk,
    "tag_id": lambda t: _victim_tag(t).pk,
    "field_id": lambda t: _victim_custom_field(t).pk,
    # Issue #13's CRM. `identity_id` names a messaging row rather than a contacts
    # one — the identity table lives in apps.messaging — but it is reached through
    # a contacts URL nested under its contact, so the victim it needs is a
    # contact's.
    "contact_id": lambda t: _victim_contact(t).pk,
    "segment_id": lambda t: _victim_segment(t).pk,
    "identity_id": lambda t: _victim_identity(t).pk,
    "import_id": lambda t: _victim_contact_import(t).pk,
    # Issue #27's flow templates. Deliberately **not** spelled ``import_id``:
    # that kwarg is already the contacts CSV import's, and reusing it would send
    # a ContactImport pk at a flows route, which 404s because the row is the
    # wrong type rather than because of tenancy — the sweep passing for exactly
    # the reason this file keeps warning about.
    "flow_import_id": lambda t: _victim_flow_import(t).pk,
    "connection_id": lambda t: _victim_connection(t).pk,
    # Issue #19's WhatsApp template manager. Workspace-scoped like the rest of
    # the channels app, so the sweep's ordinary rules apply.
    "template_id": lambda t: _victim_whatsapp_template(t).pk,
    "asset_id": lambda t: _victim_media_asset(t).pk,
    "folder_id": lambda t: _victim_media_folder(t).pk,
    "flow_id": lambda t: _victim_flow(t).pk,
    "conversation_id": lambda t: _victim_conversation(t).pk,
    "message_id": lambda t: _victim_message(t).pk,
    "trigger_id": lambda t: _victim_trigger(t).pk,
    # Notifications (issue #7) are keyed by user, not by workspace, so "the
    # victim" here is a person rather than a tenant. Registering it is an
    # opt-in: iter_tenant_routes() skips a route carrying no *registered*
    # kwarg before it ever reaches the unknown-kwarg check, so this route
    # would otherwise be neither swept nor reported. The per-user boundary is
    # also covered directly in apps/notifications/tests/test_views.py.
    "notification_id": lambda t: _victim_notification(t).pk,
    # Issue #25. `api_key_id` names an org-tier row — the API keys page spans
    # every workspace in the organization (SPEC §4.1) — but the row it names is
    # still a workspace's, so the victim it needs is the victim's workspace's.
    "api_key_id": lambda t: _victim_api_key(t).pk,
    "webhook_id": lambda t: _victim_outbound_webhook(t).pk,
    # Issue #22's sequences. All three are workspace-scoped rows reached through
    # the campaigns app's routes, so the sweep's ordinary rules apply.
    "sequence_id": lambda t: _victim_sequence(t).pk,
    "step_id": lambda t: _victim_sequence_step(t).pk,
    "enrollment_id": lambda t: _victim_enrollment(t).pk,
    # Issue #23's broadcasts. Workspace-scoped like the rest of the app, so the
    # sweep's ordinary rules apply.
    "broadcast_id": lambda t: _victim_broadcast(t).pk,
    # Issue #24's inbox v2. `label_id` is registered rather than treated as
    # neutral for a reason worth stating: `inbox:bulk_label` carries no
    # conversation_id — it posts a *set* of them — so this kwarg is the only
    # thing that makes iter_tenant_routes() look at that route at all.
    "label_id": lambda t: _victim_conversation_label(t).pk,
    "rule_id": lambda t: _victim_inbox_rule(t).pk,
    "reminder_id": lambda t: _victim_reminder(t).pk,
    "scheduled_reply_id": lambda t: _victim_scheduled_reply(t).pk,
}

#: Kwargs that need *a* value but do not identify a tenant. A route made only of
#: these is not fuzzed.
NEUTRAL_KWARG_VALUES: dict[str, Any] = {
    "platform": "instagram",
    # The email webhook's provider segment (resend / ses / smtp). It selects a
    # payload shape, not a tenant's object, and is not used for lookup.
    "provider": "resend",
    # The block position inside a message body (inbox:media). A position, not an
    # id: it identifies nothing on its own and is read only *after* the message
    # it indexes has been scoped to the caller's workspace, which is the
    # message_id resolver's job. Zero is as good as any — the route 404s for an
    # outsider at the conversation lookup, long before it looks at this.
    "index": 0,
}

#: Why the inbound webhook routes cannot answer 404 and are therefore not
#: sweepable. Shared by both, because the reasoning is identical.
_WEBHOOK_WAIVER = (
    "Unauthenticated public endpoint (SPEC §7.1). There is no session tenant to "
    "compare the connection against — the caller is a messaging platform, not a "
    "user — so 'belongs to another workspace' is not a question it can ask, and "
    "404 is not an answer it can give without breaking ingestion. What stands in "
    "for the sweep is that the route answers the SAME status to every connection "
    "id, real or not: 403 for both an unknown connection and a bad signature once "
    "the platform has an adapter, and 503 for every id while it has none — which "
    "is why apps/channels/views_webhooks.py resolves the adapter before it looks "
    "a connection up. Both halves are asserted by "
    "apps/channels/tests/test_webhooks.py::TestIdIndistinguishability; if that "
    "class is ever deleted, this waiver must be too."
)

#: Why the public API's routes cannot be swept from a session, and what stands
#: in for the sweep. Shared by every /api/v1/ operation that names a tenant
#: object, because the reasoning is identical for all of them.
_API_V1_WAIVER = (
    "Key-authenticated, not session-authenticated (SPEC §17). This sweep signs "
    "in as a fully privileged member of another organization and sends no "
    "Authorization header, so every /api/v1/ route answers 401 — which is the "
    "correct answer to a sessionless caller and is neither the 404 nor the 405 "
    "check_route_is_isolated accepts. Making the API answer 404 to an "
    "unauthenticated request instead would be worse, not better: it would hide "
    "'your key is missing' behind 'no such object' for every legitimate "
    "integration. What stands in for the sweep is the same sweep run with the "
    "right credential — "
    "apps/api/tests/test_isolation.py::TestApiV1CrossWorkspaceIsolation "
    "enumerates every registered operation from the router itself, calls each "
    "one with a valid key for workspace A against workspace B's object ids, and "
    "asserts 404 on all of them. That class also asserts that the set of "
    "operations it covers is exactly the set waived here, so an endpoint added "
    "without either a waiver or coverage turns the suite red. If it is ever "
    "deleted, these waivers must be too."
)

#: Why the two tracking routes cannot be swept, and what stands in for the sweep.
#: Shared by both, because the reasoning is identical.
#:
#: Note these carry only a ``token`` kwarg, so ``iter_tenant_routes`` would skip
#: them on its own — they are named here anyway. A route that escapes the sweep
#: because of how its URL happens to be shaped is exactly the kind this file
#: exists to stop being invisible, and "public by design" is a position somebody
#: decided rather than a fact about a kwarg list.
_TRACKING_WAIVER = (
    "Public by design (SECURITY-BASELINE §4, issue #26). The signed token IS the "
    "credential and the caller is a browser following a button in a message or a "
    "mail client fetching an image — neither has a session, so 'does this belong "
    "to another workspace' is not a question these routes can ask and 404 is not "
    "an answer they can give without breaking every link already sent. What "
    "stands in for the sweep is that they are indistinguishable in the other "
    "direction: every rejection — tampered signature, a token minted for a "
    "different purpose, an unknown version, a malformed blob, a flow deleted "
    "since the message went out — is the same bare 404 with no body detail, "
    "constant-time underneath; and the redirect's destination is read from the "
    "verified payload, never from the query string, so no id in a request can "
    "point it anywhere. Both halves are asserted by "
    "apps/analytics/tests/test_tracking_routes.py::TestTokenIndistinguishability "
    "and ::TestNoOpenRedirect; if either class is deleted, these waivers must be "
    "too."
)

#: Routes exempt from the sweep, each with the reason. A waiver is a reviewed
#: line in this dict; there is no silent skip.
WAIVED_ROUTES: dict[str, str] = {
    "click_redirect": _TRACKING_WAIVER,
    "open_pixel": _TRACKING_WAIVER,
    "webhook_sms": _WEBHOOK_WAIVER,
    "webhook_email": _WEBHOOK_WAIVER,
    "api_v1:contacts_detail": _API_V1_WAIVER,
    "api_v1:contacts_update": _API_V1_WAIVER,
    "api_v1:contacts_tag_add": _API_V1_WAIVER,
    "api_v1:contacts_tag_remove": _API_V1_WAIVER,
    "api_v1:contacts_field_set": _API_V1_WAIVER,
    "api_v1:contacts_field_list": _API_V1_WAIVER,
    "api_v1:contacts_flow_start": _API_V1_WAIVER,
    "accept_invite": (
        "Public by design: the invitation token IS the credential, and the page "
        "renders the same 404 body for unknown, expired and accepted tokens. "
        "Covered by apps/members/tests/test_invitations.py."
    ),
}


def _victim_connection(tenancy: Tenancy) -> Any:
    """A channel connection owned by the victim, created on demand.

    ``external_id`` is namespaced by slug because SPEC §5's unique constraint on
    ``(platform, external_id)`` is deployment-wide: a fixed literal here would
    make the victim's and the attacker's tenancies collide.
    """
    from apps.channels.models import ChannelConnection
    from apps.common.platforms import Platform

    connection = ChannelConnection.objects.for_workspace(tenancy.workspace).first()
    if connection is None:
        connection = ChannelConnection.objects.create(
            workspace=tenancy.workspace,
            platform=Platform.TELEGRAM,
            display_name=f"{tenancy.slug} bot",
            external_id=f"bot-{tenancy.slug}",
        )
    return connection


def _victim_whatsapp_template(tenancy: Tenancy) -> Any:
    """A WhatsApp template owned by the victim, created on demand (issue #19).

    Built on ``_victim_connection`` so the template and the connection it names
    belong to the same workspace — a template whose connection was somebody
    else's would make the sweep pass for the wrong reason. The connection is
    reused as-is even though it is a Telegram row: the routes under test resolve
    the template by workspace and never look at its platform, and forcing a
    second connection here would collide with SPEC §5's deployment-wide unique
    ``(platform, external_id)``.
    """
    from apps.channels.models import WhatsAppTemplate

    template = WhatsAppTemplate.objects.for_workspace(tenancy.workspace).first()
    if template is None:
        template = WhatsAppTemplate(
            workspace=tenancy.workspace,
            channel_connection=_victim_connection(tenancy),
            name=f"victim_{tenancy.slug}".replace("-", "_"),
            language="en_US",
            category="utility",
            body_structure={"body": {"text": "Hello"}},
        )
        template.save()
    return template


def _victim_broadcast(tenancy: Tenancy) -> Any:
    """A broadcast owned by the victim, created on demand (issue #23).

    Built on ``_victim_connection`` so the broadcast and the channel it names
    belong to the same workspace — a broadcast whose connection was somebody
    else's would make the sweep pass for the wrong reason.

    Left as a ``draft`` with no content. Every route taking a ``broadcast_id``
    resolves it by workspace before it looks at anything else, so the status is
    not what any of them 404s on; and a scheduled one would put queue rows in the
    database on every sweep.
    """
    from apps.broadcasts.models import Broadcast

    broadcast = Broadcast.objects.for_workspace(tenancy.workspace).first()
    if broadcast is None:
        broadcast = Broadcast(
            workspace=tenancy.workspace,
            name=f"Victim broadcast ({tenancy.slug})",
            channel_connection=_victim_connection(tenancy),
        )
        broadcast.save()
    return broadcast


def _victim_flow(tenancy: Tenancy) -> Any:
    """A flow owned by the victim, created on demand.

    The sweep reaches these routes through the victim's ``workspace_id`` too, so
    the middleware answers first; ``apps/flows/tests/test_api.py`` covers the
    sharper case this cannot — the attacker's *own* workspace id paired with the
    victim's flow id, where only ``get_scoped_object_or_404`` stands in the way.
    """
    from apps.flows.models import Flow
    from apps.flows.services import create_flow

    flow = Flow.objects.for_workspace(tenancy.workspace).first()
    if flow is None:
        flow = create_flow(workspace=tenancy.workspace, name="Victim onboarding")
    return flow


def _victim_conversation(tenancy: Tenancy) -> Any:
    """A conversation owned by the victim, created on demand (issue #14).

    Through ``messaging.services.open_conversation`` rather than
    ``Conversation.objects.create``: it is the facade's own get-or-create, and a
    fixture that writes the row directly is a fixture that can drift from what
    the product produces.
    """
    from apps.messaging.models import Conversation
    from apps.messaging.services import open_conversation

    conversation = Conversation.objects.for_workspace(tenancy.workspace).first()
    if conversation is not None:
        return conversation
    return open_conversation(
        workspace=tenancy.workspace,
        contact=_victim_contact(tenancy),
        connection=_victim_connection(tenancy),
    )


def _victim_conversation_label(tenancy: Tenancy) -> Any:
    """A conversation label owned by the victim, created on demand (issue #24)."""
    from apps.inbox.models import ConversationLabel

    label = ConversationLabel.objects.for_workspace(tenancy.workspace).first()
    if label is None:
        label = ConversationLabel.objects.create(workspace=tenancy.workspace, name=f"{tenancy.slug} label")
    return label


def _victim_inbox_rule(tenancy: Tenancy) -> Any:
    """An inbox rule owned by the victim, created on demand (issue #24)."""
    from apps.inbox.models import InboxRule

    rule = InboxRule.objects.for_workspace(tenancy.workspace).first()
    if rule is None:
        rule = InboxRule(
            workspace=tenancy.workspace,
            name=f"{tenancy.slug} rule",
            condition_json={"keywords": [{"text": "refund", "mode": "contains"}]},
            actions_json=[{"type": "mark_done"}],
        )
        rule.save()
    return rule


def _victim_reminder(tenancy: Tenancy) -> Any:
    """A pending reminder on the victim's thread (issue #24).

    Straight through the model rather than ``services.schedule_reminder``,
    unlike ``_victim_conversation`` above: the service also enqueues a
    ``ScheduledAction``, and the sweep wants a row for a route to 404 on, not a
    queue side effect in every one of these tests.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.inbox.models import InboxReminder

    reminder = InboxReminder.objects.for_workspace(tenancy.workspace).first()
    if reminder is None:
        reminder = InboxReminder(
            conversation=_victim_conversation(tenancy),
            recipient=tenancy.owner,
            remind_at=timezone.now() + timedelta(hours=1),
        )
        reminder.save()
    return reminder


def _victim_scheduled_reply(tenancy: Tenancy) -> Any:
    """A pending scheduled reply on the victim's thread (issue #24)."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.inbox.models import ScheduledReply

    reply = ScheduledReply.objects.for_workspace(tenancy.workspace).first()
    if reply is None:
        reply = ScheduledReply(
            conversation=_victim_conversation(tenancy),
            body={"blocks": [{"type": "text", "text": "later"}]},
            send_at=timezone.now() + timedelta(hours=1),
        )
        reply.save()
    return reply


def _victim_message(tenancy: Tenancy) -> Any:
    """A failed outbound message in the victim's thread, created on demand.

    Failed on purpose: the only route taking a ``message_id`` is the inbox's
    retry, which 404s anything that is not a failed outbound send. A queued row
    would make the sweep pass for the wrong reason.
    """
    from apps.messaging.models import Message, MessageDirection, MessageSource, MessageStatus

    conversation = _victim_conversation(tenancy)
    # Narrowed to what the route accepts, not to "any message in the thread".
    # An unfiltered .first() would hand back an inbound or a sent row the moment
    # anything else seeded one, and inbox:retry would start answering 404
    # because of the message's status rather than because of tenant isolation —
    # the sweep passing for exactly the reason this docstring warns about.
    message = (
        Message.objects.for_workspace(tenancy.workspace)
        .filter(
            conversation=conversation,
            status=MessageStatus.FAILED,
            direction=MessageDirection.OUT,
            source=MessageSource.AGENT,
            internal=False,
        )
        .first()
    )
    if message is not None:
        return message
    return Message.objects.create(
        conversation=conversation,
        direction=MessageDirection.OUT,
        source=MessageSource.AGENT,
        status=MessageStatus.FAILED,
        error="opted_out",
        idempotency_key=f"idor:{tenancy.slug}",
        body={"blocks": [{"type": "text", "text": "hello"}]},
    )


def _victim_trigger(tenancy: Tenancy) -> Any:
    """A ref-URL trigger owned by the victim, created on demand.

    Built on ``_victim_flow`` and ``_victim_connection`` rather than on fresh
    objects, so the flow the route names and the trigger it names belong to the
    same workspace — a trigger whose flow was somebody else's would make the
    sweep pass for the wrong reason.

    ``ref_url`` specifically, because the QR endpoint 404s any other type and a
    route that 404s for a reason other than tenancy proves nothing.
    """
    from apps.flows.models import Trigger, TriggerType

    trigger = Trigger.objects.for_workspace(tenancy.workspace).first()
    if trigger is None:
        trigger = Trigger(
            flow=_victim_flow(tenancy),
            channel_connection=_victim_connection(tenancy),
            type=TriggerType.REF_URL,
            config_json={"ref": f"ref-{tenancy.slug}"},
        )
        trigger.save()
    return trigger


def _victim_flow_import(tenancy: Tenancy) -> Any:
    """An unconfirmed flow-template import owned by the victim (issue #27).

    Built by exporting ``_victim_flow`` and parsing it back, rather than by
    hand-writing a document: the routes under test read ``document`` and render
    a mapping form from it, so a fixture that was not a real export would make
    the review route 404 on its own content instead of on tenancy.

    Left ``pending``, which is the state the wizard is in for all three routes.
    Tenancy is checked first either way — every one of them resolves the row
    through ``get_scoped_object_or_404`` before it looks at ``status`` — so this
    is about the fixture being the shape the routes are written for, not about
    what makes the sweep pass.
    """
    from apps.flows import portability
    from apps.flows.models import FlowImport

    record = FlowImport.objects.for_workspace(tenancy.workspace).first()
    if record is not None:
        return record
    document = portability.export_document(_victim_flow(tenancy))
    record = FlowImport(
        workspace=tenancy.workspace,
        document=document,
        mapping=portability.default_mapping(tenancy.workspace, document, user=tenancy.owner),
        original_filename="victim.flow.json",
        created_by=tenancy.owner,
    )
    record.save()
    return record


def _victim_sequence(tenancy: Tenancy) -> Any:
    """A sequence owned by the victim, created on demand (issue #22)."""
    from apps.campaigns.models import Sequence

    sequence = Sequence.objects.for_workspace(tenancy.workspace).first()
    if sequence is None:
        sequence = Sequence.objects.create(workspace=tenancy.workspace, name=f"Victim onboarding {tenancy.slug}")
    return sequence


def _victim_sequence_step(tenancy: Tenancy) -> Any:
    """A step of the victim's sequence, created on demand.

    Built on ``_victim_sequence`` and ``_victim_flow`` so the step, the sequence
    it belongs to and the flow it starts are all one workspace's — a step whose
    flow was somebody else's would make the sweep pass for the wrong reason.
    """
    from apps.campaigns.models import SequenceStep

    sequence = _victim_sequence(tenancy)
    step = SequenceStep.objects.for_workspace(tenancy.workspace).filter(sequence=sequence).first()
    if step is None:
        step = SequenceStep.objects.create(
            workspace=tenancy.workspace,
            sequence=sequence,
            position=1,
            flow=_victim_flow(tenancy),
            delay_value=1,
            delay_unit="days",
        )
    return step


def _victim_enrollment(tenancy: Tenancy) -> Any:
    """The victim's contact, enrolled in the victim's sequence.

    Through the model rather than ``campaigns.services.subscribe``: the sweep is
    about tenancy, and going through the service would emit a
    ``sequence.subscribed`` event and queue a step per route for a row nothing
    reads.
    """
    from apps.campaigns.models import SequenceEnrollment

    sequence = _victim_sequence(tenancy)
    enrollment = SequenceEnrollment.objects.for_workspace(tenancy.workspace).filter(sequence=sequence).first()
    if enrollment is None:
        enrollment = SequenceEnrollment(sequence=sequence, contact=_victim_contact(tenancy))
        enrollment.save()
    return enrollment


def _victim_invitation(tenancy: Tenancy) -> Any:
    """A pending invitation owned by the victim, created on demand."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.members.models import Invitation

    invitation = Invitation.objects.filter(organization=tenancy.organization).first()
    if invitation is None:
        invitation = Invitation.objects.create(
            organization=tenancy.organization,
            email=f"pending@{tenancy.slug}.test",
            invited_by=tenancy.owner,
            expires_at=timezone.now() + timedelta(days=7),
        )
    return invitation


def _victim_api_key(tenancy: Tenancy) -> Any:
    """An API key owned by the victim's workspace, created on demand.

    Minted through the same token helper the real issuance path uses, so the
    row satisfies the unique digest constraint rather than carrying a
    placeholder that a later constraint would reject.
    """
    from apps.api import keys as key_tokens
    from apps.api.models import ApiKey

    existing = ApiKey.objects.for_workspace(tenancy.workspace).first()
    if existing is not None:
        return existing
    minted = key_tokens.mint()
    return ApiKey.objects.create(
        workspace=tenancy.workspace,
        name="victim key",
        scopes=["read"],
        lookup_prefix=minted.lookup_prefix,
        token_digest=minted.token_digest,
    )


def _victim_outbound_webhook(tenancy: Tenancy) -> Any:
    """An outbound webhook owned by the victim, created on demand."""
    from apps.api.models import OutboundWebhook

    existing = OutboundWebhook.objects.for_workspace(tenancy.workspace).first()
    if existing is not None:
        return existing
    webhook = OutboundWebhook(
        workspace=tenancy.workspace,
        url="https://victim.example.com/hooks",
        events=["contact.created"],
    )
    webhook.rotate_secret()
    webhook.save()
    return webhook


def _victim_tag(tenancy: Tenancy) -> Any:
    """A tag owned by the victim, created on demand."""
    from apps.contacts.models import Tag

    tag = Tag.objects.for_workspace(tenancy.workspace).first()
    return tag or Tag.objects.create(workspace=tenancy.workspace, name="vip")


def _victim_custom_field(tenancy: Tenancy) -> Any:
    """A custom field owned by the victim, created on demand."""
    from apps.contacts.models import CustomField, CustomFieldType

    field = CustomField.objects.for_workspace(tenancy.workspace).first()
    return field or CustomField.objects.create(workspace=tenancy.workspace, name="Plan", type=CustomFieldType.TEXT)


def _victim_contact(tenancy: Tenancy) -> Any:
    """A contact owned by the victim, created on demand.

    Built through the model rather than ``services.create_contact``: the sweep is
    about tenancy, and going through the service would fire a ``contact.created``
    signal per route for a row nothing reads.
    """
    from apps.contacts.models import Contact

    contact = Contact.objects.for_workspace(tenancy.workspace).first()
    return contact or Contact.objects.create(
        workspace=tenancy.workspace, first_name="Victim", email=f"victim@{tenancy.slug}.test"
    )


def _victim_segment(tenancy: Tenancy) -> Any:
    """A saved segment owned by the victim, created on demand."""
    from apps.contacts.models import Segment

    segment = Segment.objects.for_workspace(tenancy.workspace).first()
    return segment or Segment.objects.create(
        workspace=tenancy.workspace, name="Victim segment", filter_json={"match": "all", "rules": []}
    )


def _victim_identity(tenancy: Tenancy) -> Any:
    """A channel identity on the victim's contact, created on demand.

    Connection-less — ROADMAP contract 1's "pending" shape — so the sweep does not
    have to build a ``ChannelConnection`` as well. The opt-out route resolves it
    by workspace *and* contact, which is the pairing under test.
    """
    from apps.messaging.models import ContactChannelIdentity

    contact = _victim_contact(tenancy)
    identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).filter(contact=contact).first()
    if identity is None:
        identity = ContactChannelIdentity(
            contact=contact, platform="telegram", platform_user_id=f"victim-{tenancy.slug}"
        )
        identity.save()
    return identity


def _victim_contact_import(tenancy: Tenancy) -> Any:
    """A CSV import run owned by the victim, created on demand.

    No file is attached: the sweep never reads one, and writing a CSV to storage
    per route would make an isolation test do IO.
    """
    from apps.contacts.models import ContactImport

    run = ContactImport.objects.for_workspace(tenancy.workspace).first()
    return run or ContactImport.objects.create(workspace=tenancy.workspace, original_filename="victim.csv")


def _victim_media_asset(tenancy: Tenancy) -> Any:
    """A media asset owned by the victim, created on demand.

    Built through the model rather than the upload view: the sweep is about
    tenancy, not about validation, and going through ``create_asset`` would make
    every IDOR run write a file to storage and sniff it.
    """
    from apps.media_library.models import MediaAsset

    asset = MediaAsset.objects.for_workspace(tenancy.workspace).first()
    if asset is None:
        asset = MediaAsset.objects.create(
            workspace=tenancy.workspace,
            filename="victim.png",
            kind="image",
            mime="image/png",
            size=1,
            file="media/victim.png",
        )
    return asset


def _victim_media_folder(tenancy: Tenancy) -> Any:
    """A media folder owned by the victim, created on demand."""
    from apps.media_library.models import MediaFolder

    folder = MediaFolder.objects.for_workspace(tenancy.workspace).first()
    if folder is None:
        folder = MediaFolder.objects.create(workspace=tenancy.workspace, name="Victim folder")
    return folder


def _victim_notification(tenancy: Tenancy) -> Any:
    """A notification belonging to the victim's owner, created on demand."""
    from apps.notifications.models import Notification

    notification = Notification.objects.filter(user=tenancy.owner).first()
    if notification is None:
        notification = Notification.objects.create(
            user=tenancy.owner,
            event_type="flow_loop_cap_hit",
            title="Victim notification",
            payload={"workspace_id": str(tenancy.workspace.pk)},
        )
    return notification


# ---------------------------------------------------------------------------
# Route discovery
# ---------------------------------------------------------------------------


class UnregisteredRouteKwargError(AssertionError):
    """A tenant route carries a kwarg the suite does not know how to build."""


class UnnamedTenantRouteError(AssertionError):
    """A tenant route has no ``name=``, so the suite cannot reverse it."""


@dataclass(frozen=True)
class TenantRoute:
    name: str
    kwargs: tuple[str, ...]

    def url_for(self, tenancy: Tenancy) -> str:
        values = {}
        for kwarg in self.kwargs:
            resolver = TENANT_KWARG_RESOLVERS.get(kwarg)
            values[kwarg] = resolver(tenancy) if resolver else NEUTRAL_KWARG_VALUES[kwarg]
        return reverse(self.name, kwargs=values)


def _pattern_kwargs(pattern: Any) -> tuple[str, ...]:
    converters = getattr(pattern, "converters", None)
    if converters is not None:
        return tuple(converters)
    regex = getattr(pattern, "regex", None)
    return tuple(regex.groupindex) if regex is not None else ()


def _walk(resolver: URLResolver, prefix: tuple[str, ...], namespace: str | None) -> Iterator[TenantRoute]:
    for entry in resolver.url_patterns:
        kwargs = prefix + _pattern_kwargs(entry.pattern)
        if isinstance(entry, URLResolver):
            child_ns = ":".join(part for part in (namespace, entry.namespace) if part) or None
            yield from _walk(entry, kwargs, child_ns)
        elif isinstance(entry, URLPattern):
            if not entry.name:
                # Skipping it would be the one silent hole in a mechanism whose
                # whole point is that nothing escapes quietly: an endpoint
                # nothing reverses is exactly the kind that gets registered
                # without a name.
                if any(kwarg in TENANT_KWARG_RESOLVERS for kwarg in kwargs):
                    raise UnnamedTenantRouteError(
                        f"Route {entry.pattern!s} takes {sorted(kwargs)} but has no name=, so the IDOR "
                        f"suite cannot reverse it. Give it a name (and waive it in WAIVED_ROUTES if it "
                        f"genuinely must not be swept). See docs/SECURITY-BASELINE.md §1."
                    )
                continue
            yield TenantRoute(name=":".join(part for part in (namespace, entry.name) if part), kwargs=kwargs)


def iter_tenant_routes(urlconf: str | None = None) -> list[TenantRoute]:
    """Every registered route that names a tenant object in its URL.

    Raises :class:`UnregisteredRouteKwargError` when such a route carries a kwarg
    with no resolver — the mechanism that makes new endpoints extend this file
    instead of quietly escaping it.
    """
    routes: list[TenantRoute] = []
    for route in _walk(get_resolver(urlconf), (), None):
        if route.name in WAIVED_ROUTES:
            continue
        if not any(kwarg in TENANT_KWARG_RESOLVERS for kwarg in route.kwargs):
            continue
        unknown = [k for k in route.kwargs if k not in TENANT_KWARG_RESOLVERS and k not in NEUTRAL_KWARG_VALUES]
        if unknown:
            raise UnregisteredRouteKwargError(
                f"Route {route.name!r} takes {unknown}, which the IDOR suite cannot build. "
                f"Register a resolver in tests/idor.py (TENANT_KWARG_RESOLVERS for an id that "
                f"identifies a tenant's object, NEUTRAL_KWARG_VALUES otherwise), or waive the "
                f"route in WAIVED_ROUTES with a reason. See docs/SECURITY-BASELINE.md §1."
            )
        routes.append(route)
    return sorted(routes, key=lambda r: r.name)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

METHODS = ("get", "post")


def check_route_is_isolated(client: Any, route: TenantRoute, victim: Tenancy) -> list[str]:
    """Hit one route with the victim's ids. Returns a list of failure strings."""
    url = route.url_for(victim)
    failures: list[str] = []
    statuses: list[int] = []

    for method in METHODS:
        response = getattr(client, method)(url)
        statuses.append(response.status_code)
        if response.status_code not in (404, 405):
            failures.append(
                f"{route.name} [{method.upper()} {url}] returned {response.status_code}; "
                f"cross-tenant access must be indistinguishable from 'no such thing' (404)."
            )

    if not failures and 404 not in statuses:
        failures.append(
            f"{route.name} [{url}] never returned 404 (saw {statuses}); the request was rejected "
            f"before tenancy was ever checked, so this route is not actually covered."
        )
    return failures


def assert_cross_tenant_isolation(client: Any, victim: Tenancy, *, urlconf: str | None = None) -> None:
    """Sweep every tenant route with ``victim``'s ids using an outsider's client.

    ``client`` must be logged in as a user with **maximum** privilege in a
    different organization — an org owner and workspace admin. Testing with a
    low-privilege outsider would prove nothing: they would be refused on their
    role before tenancy was ever consulted.
    """
    failures: list[str] = []
    routes = iter_tenant_routes(urlconf)
    assert routes, "The IDOR sweep found no tenant routes at all — the walker is broken."
    for route in routes:
        failures.extend(check_route_is_isolated(client, route, victim))

    assert not failures, "Cross-tenant isolation failures:\n  " + "\n  ".join(failures)
