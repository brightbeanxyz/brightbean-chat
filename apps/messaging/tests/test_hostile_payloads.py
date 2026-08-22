"""Untrusted inbound content (SECURITY-BASELINE §2 and §3).

Two obligations, and they pull in opposite directions:

* **Store it as delivered.** Escaping on write corrupts the data and hides the
  bug from the renderer that actually owes the escaping — and it makes the row a
  poor record of what the platform really sent.
* **Never render it unescaped.** There is no UI in this issue, so the render
  half is proved two ways: the stored value is escaped when put through Django's
  default autoescape, and this app contains no code that could bypass it.
"""

import ast
from pathlib import Path
from typing import Any

import pytest
from django.template import Context, Template

from apps.channels.events import EventPayload, EventType
from apps.contacts.models import Contact
from apps.messaging.ingest import MAX_TEXT_CHARS, persist_events
from apps.messaging.models import ContactChannelIdentity, Message
from apps.messaging.tests.conftest import make_event
from apps.messaging.tests.hostile import INJECTIONS, OVERSIZED, WRONG_TYPES

pytestmark = pytest.mark.django_db

MESSAGING = Path(__file__).resolve().parents[1]


def only_message(workspace: Any) -> Message:
    return Message.objects.for_workspace(workspace).get()


class TestStoredVerbatim:
    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_hostile_text_is_stored_exactly_as_delivered(self, tenancy: Any, connection: Any, payload: str) -> None:
        persist_events(connection, [make_event(connection, text=payload)])
        stored = only_message(tenancy.workspace).body["blocks"][0]["text"]
        assert stored == payload.replace("\x00", "")

    def test_a_template_expression_survives_as_a_literal(self, tenancy: Any, connection: Any) -> None:
        """The SSTI ban: contact-supplied content is never *evaluated*. If this
        ever comes back "49", something is rendering user input through a
        template engine."""
        persist_events(connection, [make_event(connection, text="{{ 7*7 }}")])
        assert only_message(tenancy.workspace).body["blocks"][0]["text"] == "{{ 7*7 }}"

    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_a_hostile_platform_user_id_still_resolves_an_identity(
        self, tenancy: Any, connection: Any, payload: str
    ) -> None:
        persist_events(connection, [make_event(connection, user=payload)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert "\x00" not in identity.platform_user_id


class TestBounds:
    @pytest.mark.parametrize("payload", OVERSIZED)
    def test_oversized_values_are_capped_not_rejected(self, tenancy: Any, connection: Any, payload: str) -> None:
        """A caller-controlled string in a jsonb column is an unbounded row
        (SECURITY-BASELINE §7). Capping keeps the message; rejecting loses it."""
        persist_events(connection, [make_event(connection, text=payload)])
        blocks = only_message(tenancy.workspace).body["blocks"]
        assert len(blocks[0]["text"]) <= MAX_TEXT_CHARS

    def test_a_flood_of_attachments_is_capped(self, tenancy: Any, connection: Any) -> None:
        payload = EventPayload(text="", attachments=tuple(f"https://x.test/{n}" for n in range(500)))
        persist_events(connection, [make_event(connection, payload=payload)])
        assert len(only_message(tenancy.workspace).body["blocks"]) <= 20

    def test_a_nul_byte_never_reaches_postgres(self, tenancy: Any, connection: Any) -> None:
        """Postgres cannot store NUL in a text column at all, so it is scrubbed
        rather than escaped."""
        persist_events(connection, [make_event(connection, text="before\x00after")])
        assert only_message(tenancy.workspace).body["blocks"][0]["text"] == "beforeafter"


class TestWrongTypes:
    @pytest.mark.parametrize("value", WRONG_TYPES)
    def test_a_wrongly_typed_text_field_does_not_raise(self, tenancy: Any, connection: Any, value: Any) -> None:
        """An adapter should never emit this, and "should never" is exactly the
        assumption a defensive layer does not make."""
        payload = EventPayload(text=value)  # type: ignore[arg-type]
        persist_events(connection, [make_event(connection, payload=payload)])
        assert only_message(tenancy.workspace).body["blocks"] == []

    def test_a_malformed_event_does_not_lose_the_rest_of_the_batch(self, tenancy: Any, connection: Any) -> None:
        """The one case the endpoint's own fixtures cannot reach, and it matters
        because the seam swallows our exceptions: a batch is several unrelated
        people."""
        broken = make_event(connection, user="u-bad", event_id="bad")
        object.__setattr__(broken, "payload", None)
        persist_events(connection, [broken, make_event(connection, user="u-ok", event_id="ok", text="fine")])
        assert Message.objects.for_workspace(tenancy.workspace).count() == 1

    def test_an_event_naming_another_connection_writes_nothing(
        self, tenancy: Any, other_tenancy: Any, connection: Any
    ) -> None:
        """A cheap tenancy backstop on the one path where a wrong answer writes
        another workspace's data."""
        from apps.messaging.tests.conftest import make_connection

        rival = make_connection(other_tenancy.workspace, suffix="rival")
        persist_events(connection, [make_event(rival)])
        assert not Contact.objects.for_workspace(tenancy.workspace).exists()
        assert not Contact.objects.for_workspace(other_tenancy.workspace).exists()


class TestNeverRenderedUnescaped:
    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_stored_content_escapes_under_default_autoescape(self, tenancy: Any, connection: Any, payload: str) -> None:
        """Proves both halves at once: stored verbatim (no double-escaping on
        write) and safe when a template renders it."""
        persist_events(connection, [make_event(connection, text=payload)])
        stored = only_message(tenancy.workspace).body["blocks"][0]["text"]
        rendered = Template("{{ value }}").render(Context({"value": stored}))
        assert "<script>" not in rendered
        assert "<img" not in rendered

    def test_this_app_cannot_bypass_autoescaping(self) -> None:
        """The real proof that no unescaped render surface exists yet: there is
        no code here that could make one. A later layer that needs one has to
        change this test deliberately."""
        banned = {"mark_safe", "format_html", "SafeString"}
        offenders: list[str] = []
        for path in MESSAGING.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in banned:
                    offenders.append(f"{path.name}:{node.id}")
                if isinstance(node, ast.Attribute) and node.attr in banned:
                    offenders.append(f"{path.name}:{node.attr}")
        assert offenders == []


class TestOptOutIsNotBypassable:
    @pytest.mark.parametrize("payload", INJECTIONS)
    def test_a_hostile_message_never_re_consents_an_opted_out_identity(
        self, tenancy: Any, connection: Any, payload: str
    ) -> None:
        """SPEC §19 puts opt-out here so it cannot be bypassed. The content of
        the next message is not a way around it."""
        persist_events(connection, [make_event(connection, event_id="o", kind=EventType.OPT_OUT)])
        persist_events(connection, [make_event(connection, event_id="m", text=payload)])
        identity = ContactChannelIdentity.objects.for_workspace(tenancy.workspace).get()
        assert identity.opted_out_at is not None
        assert identity.opt_in is False
