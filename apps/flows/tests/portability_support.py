"""One workspace holding every kind of reference an export has to deal with.

The portability suite keeps asking the same expensive question — "build me a
flow that touches a tag by name and by id, a custom field both ways, a
sequence, a segment, a member, a media asset, another flow, a WhatsApp
template, a request header and three kinds of trigger" — and a fixture written
once is the only way each test can then be about one thing.

:func:`seed` deliberately builds through the same services the product uses
rather than through ``Model.objects.create`` wherever a service exists, so a
fixture cannot drift into a shape the app would never produce.
"""

from dataclasses import dataclass, field
from typing import Any

from apps.flows.models import Flow, Trigger, TriggerType
from apps.flows.services import create_flow, save_draft
from apps.flows.tests.support import edge, graph, node

__all__ = ["Seeded", "answer_channels", "seed", "seed_second_flow"]


@dataclass
class Seeded:
    """The workspace's objects, so a test can assert against real ids."""

    workspace: Any
    flow: Flow
    tag: Any
    field_row: Any
    sequence: Any
    segment: Any
    media: Any
    connection: Any
    member: Any
    other_flow: Flow
    triggers: list[Trigger] = field(default_factory=list)


def seed(tenancy: Any) -> Seeded:
    """A flow exercising every reference kind, plus the objects it points at."""
    from apps.campaigns.services import create_sequence
    from apps.contacts.models import CustomFieldType
    from apps.contacts.services import create_custom_field, create_segment, get_or_create_tag
    from apps.flows.tests.support import connection_for
    from apps.media_library.models import MediaAsset

    workspace = tenancy.workspace
    tag, _ = get_or_create_tag(workspace, "VIP")
    field_row = create_custom_field(workspace, name="Plan", field_type=CustomFieldType.TEXT)
    sequence = create_sequence(workspace, name="Onboarding")
    segment = create_segment(
        workspace,
        name="Engaged",
        filter_json={"match": "all", "rules": [{"source": "tag", "key": str(tag.pk), "op": "has"}]},
    )
    media = MediaAsset.objects.create(
        workspace=workspace, filename="banner.png", kind="image", mime="image/png", size=11, file="media/banner.png"
    )
    connection = connection_for(workspace, external_id=f"bot-{tenancy.slug}")
    member = tenancy.owner
    other_flow = create_flow(workspace=workspace, name="Follow up")
    save_draft(other_flow, graph([node("n1", "send_message", _text("Thanks!"))]))

    flow = create_flow(workspace=workspace, name="Welcome", folder="Starters")
    save_draft(flow, _everything_graph(tag, field_row, sequence, segment, media, member, other_flow))

    triggers = _triggers(flow, connection, tag, field_row)
    return Seeded(
        workspace=workspace,
        flow=flow,
        tag=tag,
        field_row=field_row,
        sequence=sequence,
        segment=segment,
        media=media,
        connection=connection,
        member=member,
        other_flow=other_flow,
        triggers=triggers,
    )


def seed_second_flow(seeded: Seeded) -> Flow:
    """A flow reached only through a sequence step, for the bundle closure."""
    from apps.campaigns.services import add_step

    target = create_flow(workspace=seeded.workspace, name="Day two")
    save_draft(target, graph([node("n1", "send_message", _text("Day two."))]))
    add_step(seeded.sequence, flow=target, delay_value=1, delay_unit="days")
    return target


def _text(body: str) -> dict[str, Any]:
    return {"blocks": [{"type": "text", "text": body}]}


def _everything_graph(tag: Any, field_row: Any, sequence: Any, segment: Any, media: Any, member: Any, other: Flow):
    """A graph naming every workspace-local thing a flow can name."""
    return graph(
        [
            node(
                "welcome",
                "send_message",
                {
                    "blocks": [
                        {"type": "text", "text": "Hello {{first_name}}, welcome."},
                        {"type": "image", "media_id": str(media.pk), "caption": "Our banner"},
                        {
                            "type": "card",
                            "title": "Read this",
                            "image": str(media.pk),
                            "url_button": {"label": "Open", "url": "https://example.com/guide"},
                        },
                    ],
                    "buttons": [{"id": "yes", "label": "Yes please", "action": "postback"}],
                    "whatsapp_template": {"template_id": str(media.pk), "reference": "welcome_note/en_US"},
                },
            ),
            node(
                "tagit",
                "action",
                {
                    "actions": [
                        {"verb": "add_tag", "tag": "VIP"},
                        {"verb": "set_field", "field": "Plan", "value": "trial"},
                        {"verb": "subscribe_sequence", "sequence": str(sequence.pk)},
                        {"verb": "assign_conversation", "member": str(member.pk)},
                        {
                            "verb": "notify_members",
                            "member_ids": [str(member.pk)],
                            "via": "in_app",
                            "text": "A new lead arrived.",
                        },
                    ]
                },
                x=200,
            ),
            node(
                "check",
                "condition",
                {
                    "match": "any",
                    "rules": [
                        {"source": "tag", "key": str(tag.pk), "op": "has"},
                        {"source": "custom_field", "key": str(field_row.pk), "op": "has_value"},
                        {"source": "segment", "key": str(segment.pk), "op": "in"},
                        {"source": "sequence", "key": str(sequence.pk), "op": "subscribed"},
                    ],
                },
                x=400,
            ),
            node(
                "ask",
                "data_collection",
                {
                    "question": "What is your plan?",
                    "reply_type": "text",
                    "target": {"type": "custom_field", "key": "Plan"},
                },
                x=600,
            ),
            node(
                "call",
                "external_request",
                {
                    "method": "POST",
                    "url": "https://api.example.com/leads",
                    "headers": [
                        {"name": "Authorization", "value": "Bearer " + "deadbeef" * 4},
                        {"name": "Content-Type", "value": "application/json"},
                    ],
                    "body": {"email": "{{email}}"},
                    "response_mappings": [{"json_path": "$.plan", "target_type": "custom_field", "target": "Plan"}],
                },
                x=800,
            ),
            node("mail", "send_email", {"subject": "Hi", "html_body": "<p>Hi</p>", "from_override": "sales@acme.test"}),
            node("go", "start_flow", {"flow_id": str(other.pk)}, x=1000),
        ],
        [
            edge("welcome", "btn:yes", "tagit"),
            edge("tagit", "default", "check"),
            edge("check", "cond:true", "ask"),
            edge("check", "cond:false", "call"),
            edge("ask", "default", "mail"),
            edge("call", "default", "go"),
        ],
    )


def _triggers(flow: Flow, connection: Any, tag: Any, field_row: Any) -> list[Trigger]:
    """Three trigger types: a bound keyword, an unbound rule, and a ref URL."""
    from apps.flows.triggers.services import create_trigger

    return [
        create_trigger(
            flow,
            trigger_type=TriggerType.KEYWORD,
            config={"keywords": [{"text": "hi", "mode": "exact"}]},
            connection=connection,
        ),
        create_trigger(
            flow,
            trigger_type=TriggerType.RULE,
            config={
                "event": "tag_added",
                "tag_id": str(tag.pk),
                "filters": {
                    "match": "all",
                    "rules": [{"source": "custom_field", "key": str(field_row.pk), "op": "has_value"}],
                },
            },
        ),
        create_trigger(
            flow,
            trigger_type=TriggerType.REF_URL,
            config={"ref": "welcome-2026", "link_handle": "acme_support_bot"},
            connection=connection,
        ),
    ]


def answer_channels(
    document: Any, mapping: dict[str, Any], *, connections: bool = False, workspace: Any = None
) -> dict[str, Any]:
    """Answer every channel question, which has no default on purpose.

    A blank channel is a legal answer and a widening one — SPEC §5 makes a null
    connection mean every platform the trigger *type* supports — so
    ``default_mapping`` deliberately leaves the question open rather than
    choosing for you. Tests therefore have to say which they want, and saying so
    is the point: ``connections=False`` is what a workspace with no channels at
    all can answer, and ``connections=True`` is like-for-like with the export.
    """
    from apps.flows import portability

    for requirement in portability.requirements_for(document):
        if requirement.kind != "platform":
            continue
        if connections:
            from apps.flows.tests.support import connection_for

            connection = connection_for(
                workspace, platform=requirement.key, external_id=f"{requirement.key}-{workspace.pk}"
            )
            answer = {"id": str(connection.pk)}
        else:
            answer = {"id": portability.ANY_CONNECTION}
        mapping.setdefault("platform", {})[requirement.key] = answer
    return mapping
