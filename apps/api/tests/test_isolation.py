"""The cross-workspace sweep that stands in for the IDOR suite on ``/api/v1/``.

``tests/idor.py`` walks the URL conf and hits every tenant route as a fully
privileged member of *another* organization, asserting 404. It cannot do that
here: it authenticates with a session, and this API answers 401 to a sessionless
caller — which is the correct answer, and is neither the 404 nor the 405 the
sweep accepts. Those routes therefore carry a ``WAIVED_ROUTES`` entry, and
``_API_V1_WAIVER`` names this module as what replaces it.

So this is the same sweep with the right credential: every registered operation,
called with a **valid key for workspace A** against **workspace B's object ids**,
must answer 404 and nothing else. A 403 would confirm the id names something
real, and over a UUID space that confirmation is the only thing the caller was
missing (SECURITY-BASELINE §1).

``test_the_waiver_covers_exactly_the_operations_this_class_sweeps`` is what stops
this from rotting: an endpoint added without both a waiver and coverage here
turns the suite red rather than escaping both.
"""

import json

import pytest

from apps.api.tests.conftest import bearer, make_key
from apps.contacts.models import Contact, ContactErasure, CustomField, CustomFieldType, ErasureSource, Tag
from apps.contacts.services import add_tag
from apps.flows.models import Trigger, TriggerType
from apps.flows.tests.support import graph, node, published_flow
from apps.messaging.tests.conftest import make_connection
from tests.idor import WAIVED_ROUTES

NOOP_ACTION = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


def victim_objects(workspace):
    """One of everything the API can name, all owned by the victim."""
    contact = Contact.objects.create(workspace=workspace, first_name="Victim")
    tag = Tag.objects.create(workspace=workspace, name="victim-tag")
    add_tag(contact, tag)
    field = CustomField.objects.create(workspace=workspace, name="Victim field", type=CustomFieldType.TEXT)
    flow = published_flow(workspace, graph([node("a", "action", NOOP_ACTION)]), name="Victim flow")
    Trigger(flow=flow, type=TriggerType.API).save()
    connection = make_connection(workspace, suffix="victim")
    erasure = ContactErasure.objects.create(workspace=workspace, contact_id=contact.pk, source=ErasureSource.UI)
    return {
        "contact": contact,
        "tag": tag,
        "field": field,
        "flow": flow,
        "connection": connection,
        "erasure": erasure,
    }


def object_routes(victim):
    """Every operation that names an object, with the victim's ids in it.

    ``(url_name, method, path, body)``. The ``url_name`` is the string
    ``WAIVED_ROUTES`` is keyed by, so the completeness assertion below can
    compare the two sets directly rather than by eye.
    """
    contact = victim["contact"]
    return [
        ("api_v1:contacts_detail", "get", f"/api/v1/contacts/{contact.pk}", None),
        ("api_v1:contacts_update", "patch", f"/api/v1/contacts/{contact.pk}", {"first_name": "Stolen"}),
        ("api_v1:contacts_field_list", "get", f"/api/v1/contacts/{contact.pk}/fields", None),
        ("api_v1:contacts_tag_add", "post", f"/api/v1/contacts/{contact.pk}/tags", {"name": "mine"}),
        (
            "api_v1:contacts_tag_remove",
            "delete",
            f"/api/v1/contacts/{contact.pk}/tags/{victim['tag'].pk}",
            None,
        ),
        (
            "api_v1:contacts_field_set",
            "put",
            f"/api/v1/contacts/{contact.pk}/fields/{victim['field'].pk}",
            {"value": "stolen"},
        ),
        (
            "api_v1:contacts_flow_start",
            "post",
            f"/api/v1/contacts/{contact.pk}/flows/{victim['flow'].pk}/start",
            {},
        ),
        # ``confirm`` is supplied so the refusal has to come from tenancy. The
        # route checks the workspace before the parameter (see the operation's
        # comment), so a 404 here would be reachable either way — but a sweep
        # that got its 404 from a missing query parameter would keep passing on
        # the day the tenancy check was removed.
        ("api_v1:contacts_delete", "delete", f"/api/v1/contacts/{contact.pk}?confirm=erase", None),
        ("api_v1:erasures_detail", "get", f"/api/v1/erasures/{victim['erasure'].pk}", None),
    ]


def call(client, method, path, body, auth):
    kwargs = dict(auth)
    if body is not None:
        kwargs["data"] = json.dumps(body)
        kwargs["content_type"] = "application/json"
    return getattr(client, method)(path, **kwargs)


@pytest.mark.django_db
class TestApiV1CrossWorkspaceIsolation:
    def test_every_object_route_404s_for_a_key_from_another_workspace(self, client, tenancy, other_tenancy):
        """The sweep proper.

        The attacker's key carries every scope — the refusal has to come from
        tenancy, not from a missing scope, or the test proves nothing. ``erase``
        is included for that reason and no other: without it the two erasure
        routes would answer 403 and the sweep would pass without ever reaching
        the tenancy check it exists to exercise.
        """
        victim = victim_objects(other_tenancy.workspace)
        _, plaintext = make_key(tenancy.workspace, scopes=("read", "write", "erase"))
        auth = bearer(plaintext)

        for name, method, path, body in object_routes(victim):
            response = call(client, method, path, body, auth)
            assert response.status_code == 404, f"{name} answered {response.status_code}"
            assert response.json()["error"]["code"] == "not_found", name

    def test_the_victims_objects_are_untouched(self, client, tenancy, other_tenancy):
        """A 404 that still wrote is not isolation."""
        victim = victim_objects(other_tenancy.workspace)
        _, plaintext = make_key(tenancy.workspace, scopes=("read", "write", "erase"))
        auth = bearer(plaintext)

        for _, method, path, body in object_routes(victim):
            call(client, method, path, body, auth)

        victim["contact"].refresh_from_db()
        assert victim["contact"].first_name == "Victim"
        assert list(victim["contact"].tags.values_list("name", flat=True)) == ["victim-tag"]

    def test_the_send_endpoint_404s_on_another_workspaces_ids(self, client, tenancy, other_tenancy):
        """Not in ``object_routes``: its ids are in the body, not the path.

        The IDOR sweep only ever looks at URL kwargs, so a route that names a
        tenant object in its body is exactly the kind that escapes it. Covered
        explicitly here for that reason.
        """
        victim = victim_objects(other_tenancy.workspace)
        _, plaintext = make_key(tenancy.workspace)

        response = call(
            client,
            "post",
            "/api/v1/messages",
            {
                "contact_id": str(victim["contact"].pk),
                "connection_id": str(victim["connection"].pk),
                "body": {"text": "hello"},
            },
            bearer(plaintext),
        )

        assert response.status_code == 404

    def test_the_list_endpoints_show_nothing_of_the_other_workspace(self, client, tenancy, other_tenancy):
        victim_objects(other_tenancy.workspace)
        _, plaintext = make_key(tenancy.workspace)
        auth = bearer(plaintext)

        for path in ("/api/v1/contacts", "/api/v1/tags", "/api/v1/fields", "/api/v1/flows"):
            assert call(client, "get", path, None, auth).json()["data"] == [], path

    def test_the_waiver_covers_exactly_the_operations_this_class_sweeps(self, tenancy, other_tenancy):
        """The anti-rot assertion the waiver's reason promises.

        Two directions, both of which matter. A waived route with no coverage
        here would be an endpoint nothing checks; a covered route with no waiver
        would mean the session sweep is still trying to reach it and failing.
        """
        from tests.idor import _API_V1_WAIVER

        waived = {name for name, reason in WAIVED_ROUTES.items() if reason is _API_V1_WAIVER}
        swept = {name for name, _, _, _ in object_routes(victim_objects(other_tenancy.workspace))}

        assert waived == swept

    def test_every_api_route_naming_a_tenant_object_is_accounted_for(self):
        """Nothing under /api/v1/ names a tenant object without being waived.

        ``iter_tenant_routes`` skips a waived route, so this walks the raw URL
        conf instead: an operation that grows a ``contact_id`` and no waiver
        would otherwise be swept from a session and fail with a 401 that reads
        like an unrelated bug.
        """
        from django.urls import get_resolver

        from tests.idor import TENANT_KWARG_RESOLVERS, _walk

        for route in _walk(get_resolver(None), (), None):
            if not route.name.startswith("api_v1:"):
                continue
            if not any(kwarg in TENANT_KWARG_RESOLVERS for kwarg in route.kwargs):
                continue
            assert route.name in WAIVED_ROUTES, f"{route.name} names a tenant object but is not waived"
