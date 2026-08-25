"""SPEC §19: "Opt-out is enforced in the compliance engine … so it cannot be bypassed."

Issue #29 asks for this as a *property* rather than a handful of cases, and a
property needs two halves that hold each other up:

**Structural.** ``adapter.send(...)`` is called from exactly one place in the
product, and everything that sends goes through the messaging facade to reach
it. That is what makes "the chokepoint" a fact about the codebase rather than a
convention people follow.

**Behavioural.** That one chokepoint refuses an opted-out identity for every
message source there is, and the refusal is observed at the adapter — not at
``can_send``, which is the decision rather than the enforcement.

Together they are a proof: if the only door to a platform is ``send_outbound``,
and ``send_outbound`` never opens it for an opted-out identity whatever the
source, then no flow, broadcast, API call, sequence step or scheduled reply can.
Neither half is worth much alone, which is why the structural test fails when a
second door appears rather than trusting that nobody will add one.
"""

import ast
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.utils import timezone

from apps.channels.events import OutboundMessage, TextBlock
from apps.common.platforms import Platform
from apps.contacts.services import create_contact
from apps.messaging import services as messaging_services
from apps.messaging.codes import Denial
from apps.messaging.models import ContactChannelIdentity, MessageSource, MessageStatus, OptInSource
from apps.messaging.tests.conftest import make_connection
from tests.optout import AdapterReached, adapter_tripwire
from tests.support import Tenancy

pytestmark = pytest.mark.django_db

APPS = Path(__file__).resolve().parents[1] / "apps"

#: The facade's send functions. Anything that puts a message on a platform calls
#: one of these; ``apps/messaging/tests/test_facade_contract.py``'s subject.
SEND_FACADE = {"send_outbound", "send_as_agent", "send_via_api", "send_compliance_reply"}

#: Modules allowed to contain a literal ``adapter.send(...)``, with the reason.
#: One entry, and that is the point of the test below.
ADAPTER_CALL_SITES = {
    "messaging/services.py": (
        "The chokepoint. SPEC §8: can_send is 'called by the send pipeline for every outbound "
        "message, no exceptions', and this is that pipeline."
    ),
}

#: Modules that call the facade, and what they are. A new one is not a problem —
#: it is the *point* of the facade — but it has to be a known send source, so
#: adding one here is how an author says which.
SEND_SOURCES = {
    "messaging/services.py": (
        "The facade itself. send_as_agent, send_via_api and send_compliance_reply all delegate to "
        "send_outbound in-module; they are doors onto one pipeline, not separate sources."
    ),
    "flows/engine/sending.py": "The send_message node (source=automation).",
    "flows/engine/nodes/send_sms.py": "SPEC §11.9's send_sms node (source=automation).",
    "channels/nodes/send_email.py": "SPEC §11.10's send_email node (source=automation).",
    "broadcasts/handlers.py": "Broadcast fanout (source=broadcast).",
    "api/routers/messages.py": "POST /api/v1/messages (source=api).",
    "inbox/views.py": "An agent typing a reply in the inbox (source=agent).",
    "inbox/handlers.py": "A scheduled reply firing off the queue (source=agent).",
    "channels/sms_compliance.py": (
        "SPEC §6.6's mandated STOP/HELP confirmations, through send_compliance_reply — the one "
        "sanctioned door past the compliance verdict, and past *only* the verdict: it still "
        "resolves an identity, still records a Message and still cannot reach an adapter for a "
        "contact who is gone. Replying to STOP is what makes the opt-out auditable, so refusing "
        "it would be worse than allowing it. Covered by apps/messaging/tests/test_compliance_doors.py."
    ),
}

#: Sequences do not appear above, and that is not an omission: a sequence step
#: starts a flow (``apps/campaigns/handlers.py``), so it reaches a platform
#: through ``flows/engine/sending.py`` like any other automation. ``source`` is
#: still stamped ``sequence`` on the message, which is why the behavioural half
#: below parametrises over ``MessageSource`` rather than over this table.


def _sources() -> list[Path]:
    """Every non-test, non-migration module under ``apps/``.

    The same selection ``apps/messaging/tests/test_write_sites.py`` makes, and
    for the same reasons it gives.
    """
    return [path for path in APPS.rglob("*.py") if "migrations" not in path.parts and "tests" not in path.parts]


def _modules_calling(names: set[str], *, attribute: str | None = None) -> dict[str, list[str]]:
    """``{module: [called names]}``, over the AST rather than a grep.

    A grep cannot tell a call from a docstring, and these names appear in plenty
    of prose — ``apps/broadcasts/handlers.py`` and ``apps/messaging/models.py``
    both discuss ``send_outbound`` without calling it.
    """
    found: dict[str, list[str]] = {}
    for path in _sources():
        text = path.read_text()
        if not any(name in text for name in names):
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in names:
                if attribute is not None and not (isinstance(func.value, ast.Name) and func.value.id == attribute):
                    continue
                found.setdefault(str(path.relative_to(APPS)), []).append(func.attr)
            elif attribute is None and isinstance(func, ast.Name) and func.id in names:
                found.setdefault(str(path.relative_to(APPS)), []).append(func.id)
    return found


class TestTheChokepointIsStructural:
    def test_only_one_module_calls_an_adapter(self) -> None:
        """The claim SPEC §19 rests on, asserted rather than assumed.

        If a second module ever calls ``adapter.send(...)``, the compliance
        engine has been routed around and nothing else in this file matters.
        """
        callers = set(_modules_calling({"send"}, attribute="adapter"))

        assert callers == set(ADAPTER_CALL_SITES), (
            f"adapter.send() is called from {sorted(callers)}. SPEC §19 puts opt-out in the compliance "
            f"engine so it cannot be bypassed; a second call site is a bypass. If this is deliberate, "
            f"add it to ADAPTER_CALL_SITES with the reason."
        )

    def test_every_module_that_sends_is_a_known_source(self) -> None:
        """A new caller of the facade is fine. A new *unclassified* one is not:
        this table is how the behavioural test below knows what to cover."""
        callers = set(_modules_calling(SEND_FACADE))

        assert callers == set(SEND_SOURCES), (
            f"Modules calling the send facade changed: {sorted(callers ^ set(SEND_SOURCES))}. "
            f"Add it to SEND_SOURCES saying which send source it is."
        )

    def test_the_scan_would_catch_a_second_adapter_call(self, tmp_path: Path) -> None:
        """A test that can only pass is not a test."""
        module = tmp_path / "rogue.py"
        module.write_text("def go(adapter, c, i, o):\n    return adapter.send(c, i, o)\n")

        tree = ast.parse(module.read_text())
        calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "adapter"
        ]

        assert calls == ["send"]


# ---------------------------------------------------------------------------
# Behavioural: the chokepoint refuses, for every source
# ---------------------------------------------------------------------------


def _identity(workspace: Any, *, opted_out: bool) -> tuple[Any, Any]:
    contact = create_contact(workspace, first_name="Subject", source="manual")
    connection = make_connection(workspace, suffix="optout")
    ContactChannelIdentity.objects.create(
        contact=contact,
        channel_connection=connection,
        platform=Platform.TELEGRAM.value,
        platform_user_id="tg-optout",
        opt_in=not opted_out,
        opt_in_at=None if opted_out else timezone.now(),
        opt_in_source="" if opted_out else OptInSource.MESSAGE_IN,
        opted_out_at=timezone.now() if opted_out else None,
        window_expires_at=timezone.now() + timedelta(hours=12),
    )
    return contact, connection


def _send(workspace: Any, contact: Any, connection: Any, source: str) -> Any:
    return messaging_services.send_outbound(
        workspace=workspace,
        contact=contact,
        connection=connection,
        outbound=OutboundMessage(blocks=(TextBlock(text="hello"),)),
        source=source,
        idempotency_key=f"optout-{source}",
    )


class TestNoSourceReachesAnAdapter:
    @pytest.mark.parametrize("source", sorted(MessageSource.values))
    def test_an_opted_out_identity_is_never_reached(self, tenancy: Tenancy, source: str) -> None:
        """Every source SPEC §5 defines: automation, agent, api, broadcast, sequence."""
        contact, connection = _identity(tenancy.workspace, opted_out=True)

        with adapter_tripwire() as reached:
            message = _send(tenancy.workspace, contact, connection, source)

        assert reached == []
        assert message.status == MessageStatus.FAILED
        assert message.error == Denial.OPTED_OUT.value

    @pytest.mark.parametrize("source", sorted(MessageSource.values))
    def test_a_consenting_identity_is_reached(self, tenancy: Tenancy, source: str) -> None:
        """The positive control.

        Without it the assertions above pass on the day the fixture stops
        producing a sendable message, and the suite reports a bypass-free
        product because it never tried to send anything.
        """
        contact, connection = _identity(tenancy.workspace, opted_out=False)

        with adapter_tripwire(permissive=True) as reached:
            message = _send(tenancy.workspace, contact, connection, source)

        assert [row["address"] for row in reached] == ["tg-optout"]
        assert message.status == MessageStatus.SENT

    def test_the_tripwire_escapes_the_facades_own_except_clause(self, tenancy: Tenancy) -> None:
        """The reason :class:`AdapterReached` subclasses ``BaseException``.

        ``send_outbound`` catches ``Exception`` around the adapter call and turns
        it into a deferred send, so a tripwire raising ``AssertionError`` would
        be swallowed and every test above would go green while reporting the
        bypass it exists to catch.
        """
        contact, connection = _identity(tenancy.workspace, opted_out=False)

        with pytest.raises(AdapterReached), adapter_tripwire():
            _send(tenancy.workspace, contact, connection, MessageSource.AUTOMATION)

    def test_a_deleted_contact_is_not_reached_either(self, tenancy: Tenancy) -> None:
        """The sibling refusal, and the one erasure leans on while a queued
        teardown is still in flight."""
        from apps.contacts.services import delete_contact

        contact, connection = _identity(tenancy.workspace, opted_out=False)
        delete_contact(contact)

        with adapter_tripwire() as reached:
            message = _send(tenancy.workspace, contact, connection, MessageSource.BROADCAST)

        assert reached == []
        assert message.error == Denial.CONTACT_DELETED.value
