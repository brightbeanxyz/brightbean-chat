"""The "test on Telegram" flow preview (SPEC §16, issue #12).

    Preview: a "test on Telegram" action that links the editor's user to a test
    conversation and runs the draft version against it (Telegram only in v1;
    cheapest real-channel test loop).

Three moving parts, and the interesting one is the middle:

1. :func:`mint` writes a :class:`~apps.channels.models.FlowPreviewLink` and
   returns the handle that goes in the ``t.me/<bot>?start=preview-<handle>``
   deep link. The builder's Test button calls it.
2. The tester taps the link. Telegram delivers ``/start preview-<handle>``,
   which :class:`~apps.channels.providers.telegram.TelegramAdapter` parses into
   a ``referral`` event — the same event type an ordinary ref link produces,
   because that is what it is.
3. :func:`preview_events`, registered on ROADMAP contract 6's dispatch seam,
   recognises the ``preview-`` prefix, claims the link for that chat, and starts
   the flow's **draft** version.

Nothing else in the product knows the preview exists. The runner already
accepts an explicit ``flow_version`` and already sets ``execution.preview`` from
``not version.published`` (``apps.flows.engine.runner.start_flow``), so
"preview runs are excluded from stats" costs nothing here.

--------------------------------------------------------------------------
Why this stage runs last
--------------------------------------------------------------------------

It has to follow persistence and L4-A's routing tail, twice over:

* the contact and identity this preview runs against are created by
  persistence, so running first would mean resolving them ourselves and racing
  the stage that owns them;
* routing's ``resume`` stage hands the inbound event to the contact's live
  execution. If the preview had already started one, the very ``/start`` that
  triggered the preview would immediately be offered to it as a reply — and a
  draft waiting on a button would fall straight through its own first question.

That is declared, not arranged: it registers in
:data:`apps.channels.ingest.LATE_ORDER`, so the position is a property of the
stage rather than of where its app happens to sit in ``INSTALLED_APPS``. An
earlier version of this module leaned on app-loading order instead and had to be
registered from ``apps.flows`` to get it — which put the reason for the
registration in a different app from the code.

The resulting order — ``persistence → routing → preview`` — also states the
semantics correctly: starting the draft **supersedes** whatever the event would
otherwise have done, which is what "while linked, the draft version runs" means.
"""

import logging
from collections.abc import Sequence
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.channels.events import EventType, NormalizedEvent
from apps.channels.models import (
    PREVIEW_LINK_TTL,
    ChannelConnection,
    ConnectionStatus,
    FlowPreviewLink,
    generate_preview_handle,
)
from apps.common.encryption import hmac_digest
from apps.common.platforms import Platform

logger = logging.getLogger(__name__)

__all__ = [
    "PREVIEW_PROCESSOR",
    "PREVIEW_REF_PREFIX",
    "handle_from_ref",
    "mint",
    "preview_events",
    "prune_expired_links",
    "register_processors",
    "start_payload",
]

#: The contract-6 stage name.
PREVIEW_PROCESSOR = "preview"

#: What marks a ``/start`` payload as a preview link rather than an operator's
#: ref-URL trigger. Inside Telegram's ``[A-Za-z0-9_-]`` deep-link alphabet, and
#: distinctive enough that a real ref colliding with it would have to be trying.
PREVIEW_REF_PREFIX = "preview-"


def start_payload(handle: str) -> str:
    """The ``?start=`` payload for ``handle``."""
    return f"{PREVIEW_REF_PREFIX}{handle}"


def handle_from_ref(ref: str) -> str:
    """The handle inside a ``preview-…`` ref, or "" when it is not one."""
    if not ref.startswith(PREVIEW_REF_PREFIX):
        return ""
    return ref[len(PREVIEW_REF_PREFIX) :]


def mint(*, flow: Any, connection: ChannelConnection, user: Any, now: Any = None) -> tuple[FlowPreviewLink, str]:
    """Create a preview link. Returns the row and the handle, which is shown once.

    The handle is the credential and is never stored in a readable form — only
    its HMAC is — so this is the only moment it exists. That is the same
    discipline ``ChannelConnection.rotate_webhook_secret`` follows, and the same
    reason: a database snapshot should not hand over working links.
    """
    handle = generate_preview_handle()
    moment = now or timezone.now()
    link = FlowPreviewLink.objects.create(
        workspace=flow.workspace,
        flow=flow,
        channel_connection=connection,
        created_by=user,
        handle_digest=hmac_digest(handle),
        expires_at=moment + PREVIEW_LINK_TTL,
    )
    return link, handle


def register_processors() -> None:
    """Register the preview stage on contract 6's seam, in the late band."""
    from apps.channels import ingest

    ingest.register_processor(preview_events, name=PREVIEW_PROCESSOR, order=ingest.LATE_ORDER)


def preview_events(connection: ChannelConnection, events: Sequence[NormalizedEvent]) -> None:
    """Start a draft run for any preview link this batch presents.

    Everything about a link that does not work is deliberately indistinguishable
    from a ``/start`` payload that was never a preview link at all
    (SECURITY-BASELINE §4): expired, tampered, unknown, already claimed by
    somebody else's chat — all of them return here having done nothing, logged
    nothing specific, and sent nothing. There is no HTTP response to make
    generic, because the token arrives inside a webhook that always answers 200;
    what stands in for the generic 404 is that no observable behaviour differs.
    """
    if connection.platform != Platform.TELEGRAM:
        return
    for event in events:
        if event.type != EventType.REFERRAL:
            continue
        handle = handle_from_ref(event.payload.ref)
        if not handle:
            continue
        _start_preview(connection, handle, event.platform_user_id)


def _start_preview(connection: ChannelConnection, handle: str, chat_id: str) -> None:
    link = _claim(connection, handle, chat_id)
    if link is None:
        return

    from apps.flows import services as flow_services
    from apps.flows.engine import start_flow
    from apps.flows.models import StartedBy

    version = flow_services.latest_version(link.flow)
    if version is None:
        logger.info("Preview link %s names a flow with no version to run.", link.pk)
        return

    contact = _contact_for(connection, chat_id)
    if contact is None:
        logger.warning("Preview link %s: no contact resolved for the tester's chat.", link.pk)
        return

    try:
        start_flow(
            contact,
            link.flow,
            started_by=StartedBy.stamp(StartedBy.PREVIEW, link.pk),
            # The whole point: the *latest* version, published or not.
            flow_version=version,
            # Said explicitly rather than left to the runner's derivation. A
            # flow that was published and not edited since has the published
            # version as its latest, and deriving from `not version.published`
            # would then record a deliberate test as a production run — moving
            # the numbers the tester is about to go and read.
            preview=True,
            connection=connection,
        )
    except Exception:
        # Nothing this stage does may escape. A processor that raises makes
        # `process_events` return False, which marks **every** event-log row in
        # the delivery FAILED — so one bad preview link would have an operator
        # investigating messages that were persisted perfectly well.
        #
        # The expected member of this set is FlowNotRunnableError: a draft is
        # allowed to be half-wired, that is what a draft is, and a graph with no
        # single entry node is an ordinary outcome here. The builder's
        # validation panel already says so, and the tester gets silence — the
        # same thing every other unmatched /start payload gets.
        logger.info("Preview link %s could not start its draft.", link.pk, exc_info=True)


def _claim(connection: ChannelConnection, handle: str, chat_id: str) -> FlowPreviewLink | None:
    """Bind this link to ``chat_id``, or return None if it is not ours to use.

    A conditional ``UPDATE`` rather than a read followed by a write, so two
    chats presenting the same handle at the same moment cannot both be told they
    won it. The predicate carries every rule at once, and each clause is load
    bearing:

    ``handle_digest``
        The handle is the credential. An equality match on a fixed-length HMAC
        keyed on ``SECRET_KEY``.
    ``channel_connection``
        **The link only works on the bot it was minted for.** Without this the
        lookup spans every connection in the deployment — the query has to be
        unscoped, because an inbound webhook has no session and therefore no
        workspace — so a handle presented to a *different* bot, including one in
        another tenant, would claim the row, burn it for the tester it was
        minted for, and then fail deeper in ``start_flow`` on a workspace
        mismatch. Failing closed here means the wrong bot sees an unrecognised
        ``/start`` payload and nothing else happens.
    ``expires_at``
        Enforced in SQL rather than after the read, so an expired link is not
        claimed on its way to being rejected.
    ``chat_id``
        Either the first chat to arrive or the one that already holds it.
        Re-claiming from the same chat is deliberate — an editor pressing Test
        twice, or a tester tapping the link again to restart the draft, is the
        normal way this is used.
    """
    if not handle or not chat_id:
        return None
    now = timezone.now()
    digest = hmac_digest(handle)
    # Cross-tenant by necessity: this runs on the inbound webhook path, which
    # has no session and therefore no workspace. What bounds it is the pair of
    # the handle — 192 bits of urandom, digest keyed on SECRET_KEY — and the
    # connection the delivery was verified against.
    rows = FlowPreviewLink.objects.unscoped().filter(
        handle_digest=digest,
        channel_connection=connection,
        expires_at__gt=now,
    )
    with transaction.atomic():
        claimed = rows.filter(Q(chat_id="") | Q(chat_id=chat_id)).update(
            chat_id=chat_id,
            claimed_at=now,
            updated_at=now,
        )
        if not claimed:
            return None
        # Cross-tenant for the same reason as the filter above. Re-read through
        # the same predicate, so a row that stopped matching between the two
        # statements is not resurrected here.
        link = rows.select_related("flow", "flow__workspace", "channel_connection").first()
    if link is None or link.channel_connection.status == ConnectionStatus.DISABLED:
        return None
    return link


def _contact_for(connection: ChannelConnection, chat_id: str) -> Any:
    """The contact behind this Telegram chat, as L3-A resolved it.

    Imported late and by name rather than at module scope: ``apps.messaging``
    depends on ``apps.channels``, so a top-level import here would be a cycle.
    ``resolve_identity`` is the function the Layer-4 notes name for exactly this,
    and it is idempotent — persistence has already run by the time this stage
    does (see the module docstring), so this finds the row rather than making
    one.
    """
    from apps.messaging.identities import resolve_identity

    try:
        return resolve_identity(connection, chat_id).contact
    except Exception:
        logger.exception("Preview: could not resolve the tester's contact on connection %s.", connection.pk)
        return None


def prune_expired_links(*, now: Any = None, keep_for: Any = PREVIEW_LINK_TTL) -> int:
    """Delete preview links that expired more than ``keep_for`` ago.

    Kept a while past expiry rather than deleted on the dot so an operator
    debugging "the test link did nothing" can still see that the link existed
    and when it lapsed.
    """
    cutoff = (now or timezone.now()) - keep_for
    # Cross-tenant by design: a housekeeping sweep is deployment-wide.
    deleted, _ = FlowPreviewLink.objects.unscoped().filter(expires_at__lt=cutoff).delete()
    return int(deleted)
