"""SPEC §11.7's node, end to end through the real guard.

Nothing here stubs :func:`apps.common.outbound.guarded_request`. Stubbing it
would test the node's branching and leave the interesting claim — that this node
cannot reach ``127.0.0.1`` — asserted nowhere, which is exactly the gap
``tests/ssrf.py`` exists to close. So :class:`FakeInternet` replaces DNS and the
HTTP transport underneath the guard instead, and the guard itself runs for real:
resolution, address rules, pinning, redirects and the size cap all execute on
the path these tests exercise.

Flows are published and run through :func:`apps.flows.engine.start_flow` against
the database for the same reason ``test_runner.py`` does it — the node's output
is a handle, and a handle only means something once an edge has been followed.
"""

from typing import Any

import httpx
import pytest

from apps.common import outbound
from apps.contacts.services import create_custom_field, field_values_for
from apps.flows.engine import start_flow, synchronous_safe
from apps.flows.engine.nodes.external_request import _timeout as node_timeout
from apps.flows.engine.nodes.external_request import parse_path, summary_key
from apps.flows.models import ExecutionStatus, StartedBy
from apps.flows.tests.support import contact_for, edge, graph, node, published_flow
from apps.queueing.models import ScheduledAction
from tests.ssrf import FakeInternet, deployment_cache_cleared, guard_required, serving

PUBLIC = "93.184.216.34"
API = "api.example.test"
SINK = {"actions": [{"verb": "remove_tag", "tag": "not-a-tag-here"}]}


@pytest.fixture(autouse=True)
def _clear_deployment_cache() -> Any:
    """The guard caches its own host's addresses; these tests swap the resolver."""
    with deployment_cache_cleared():
        yield


# ``FakeInternet``/``serving`` moved to tests/ssrf.py when the media fetch
# (#12 follow-up) became the third call site to need them. ``names`` defaults
# here to this module's one API host so the call sites below stay one-liners.
def fake_internet(handler: Any, names: dict[str, list[str]] | None = None) -> FakeInternet:
    return FakeInternet(handler, names if names is not None else {API: [PUBLIC]})


def request_node(config: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"method": "GET", "url": f"https://{API}/orders"}
    base.update(config or {})
    base.update(overrides)
    return node("req", "external_request", base)


def branching_flow(workspace: Any, config: dict[str, Any] | None = None, **overrides: Any) -> Any:
    """An external_request node with a distinct node on each of its two handles."""
    return published_flow(
        workspace,
        graph(
            [
                request_node(config, **overrides),
                node("ok", "action", SINK, x=200),
                node("bad", "action", SINK, x=400),
            ],
            [edge("req", "default", "ok"), edge("req", "error", "bad")],
        ),
    )


def run(workspace: Any, flow: Any, **fields: Any) -> Any:
    return start_flow(contact_for(workspace, **fields), flow, started_by=StartedBy.API)


@pytest.mark.django_db
class TestTheHappyPath:
    def test_a_2xx_continues_on_the_default_handle(self, tenancy, monkeypatch):
        fake_internet(serving({"id": "abc"})).install(monkeypatch)
        execution = run(tenancy.workspace, branching_flow(tenancy.workspace))

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "ok"

    def test_the_request_goes_through_the_ssrf_guard(self, tenancy, monkeypatch):
        """SECURITY-BASELINE §6's "a test proving the guard is in the path"."""
        internet = fake_internet(serving({"id": "abc"})).install(monkeypatch)
        flow = branching_flow(tenancy.workspace)

        with guard_required() as guarded:
            run(tenancy.workspace, flow)

        assert len(guarded) == 1
        assert internet.requests[0].url.host == PUBLIC, "pinned to the checked address"
        assert internet.requests[0].headers["host"] == API

    def test_a_private_url_never_leaves_the_process(self, tenancy, monkeypatch):
        """The whole point of the node existing behind a guard."""
        internet = fake_internet(serving({}), names={"internal.example.test": ["169.254.169.254"]}).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            url="https://internal.example.test/latest/meta-data/",
            fallback_handle_on_error=True,
        )

        execution = run(tenancy.workspace, flow)

        assert internet.requests == [], "no connection may be attempted at all"
        assert execution.current_node_id == "bad"
        assert execution.status == ExecutionStatus.COMPLETED

    def test_the_configured_method_and_body_are_sent(self, tenancy, monkeypatch):
        internet = fake_internet(serving({})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            method="POST",
            body={"name": "{{first_name}}", "nested": {"tag": "{{last_name}}"}},
        )

        run(tenancy.workspace, flow, first_name="Ada", last_name="Lovelace")

        request = internet.requests[0]
        assert request.method == "POST"
        assert request.read() == b'{"name":"Ada","nested":{"tag":"Lovelace"}}'

    def test_a_get_carries_no_body(self, tenancy, monkeypatch):
        """A GET with a JSON body is mishandled by servers and intermediaries alike."""
        internet = fake_internet(serving({})).install(monkeypatch)
        flow = branching_flow(tenancy.workspace, method="GET", body={"a": 1})

        run(tenancy.workspace, flow)

        assert internet.requests[0].read() == b""

    def test_a_header_configured_twice_says_so(self, tenancy, monkeypatch, caplog):
        """Last one wins, but silently losing an API key header is undebuggable."""
        internet = fake_internet(serving({})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            headers=[{"name": "X-Key", "value": "first"}, {"name": "X-Key", "value": "second"}],
        )

        with caplog.at_level("INFO"):
            run(tenancy.workspace, flow)

        assert internet.requests[0].headers["x-key"] == "second"
        assert "more than once" in caplog.text
        assert "first" not in caplog.text and "second" not in caplog.text, "names are logged, values never"

    def test_a_header_whose_name_renders_empty_says_so(self, tenancy, monkeypatch, caplog):
        internet = fake_internet(serving({})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            headers=[{"name": "{{nothing_here}}", "value": "x"}, {"name": "X-Fine", "value": "y"}],
        )

        with caplog.at_level("INFO"):
            run(tenancy.workspace, flow)

        assert internet.requests[0].headers["x-fine"] == "y"
        assert "rendered empty" in caplog.text

    def test_a_header_rendered_from_an_accented_name_does_not_crash_the_step(self, tenancy, monkeypatch):
        """A contact called Jörg in a header value used to raise
        ``UnicodeEncodeError`` out of httpx, past ``except OutboundError``."""
        internet = fake_internet(serving({})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            headers=[{"name": "X-Customer", "value": "{{first_name}}"}, {"name": "X-Fine", "value": "plain"}],
        )

        execution = run(tenancy.workspace, flow, first_name="Jörg")

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "ok"
        assert "x-customer" not in internet.requests[0].headers
        assert internet.requests[0].headers["x-fine"] == "plain"
        assert not ScheduledAction.objects.for_workspace(tenancy.workspace.pk).exists()

    def test_headers_are_rendered_and_sent(self, tenancy, monkeypatch):
        internet = fake_internet(serving({})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            headers=[{"name": "X-Customer", "value": "{{first_name}}"}, {"name": "Accept", "value": "text/json"}],
        )

        run(tenancy.workspace, flow, first_name="Ada")

        assert internet.requests[0].headers["x-customer"] == "Ada"
        assert internet.requests[0].headers["accept"] == "text/json"


@pytest.mark.django_db
class TestErrorRouting:
    """SPEC §11.7's ``error`` handle, and the retry storm it must not become."""

    @pytest.mark.parametrize(
        ("handler", "names"),
        [
            (serving(status=500), None),
            (serving(status=404), None),
            (serving({}), {API: ["10.0.0.1"]}),
            (serving({}), {}),
        ],
        ids=["server-error", "not-found", "ssrf-denial", "dns-failure"],
    )
    def test_a_failure_routes_to_the_error_handle(self, tenancy, monkeypatch, handler, names):
        fake_internet(handler, names=names).install(monkeypatch)
        flow = branching_flow(tenancy.workspace, fallback_handle_on_error=True)

        execution = run(tenancy.workspace, flow)

        assert execution.current_node_id == "bad"
        assert execution.status == ExecutionStatus.COMPLETED

    def test_a_timeout_routes_rather_than_failing_the_run(self, tenancy, monkeypatch):
        def _timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow", request=request)

        fake_internet(_timeout).install(monkeypatch)
        flow = branching_flow(tenancy.workspace, fallback_handle_on_error=True)

        execution = run(tenancy.workspace, flow)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "bad"
        assert execution.last_error == ""

    def test_a_timeout_schedules_no_retry(self, tenancy, monkeypatch):
        """The acceptance criterion: "without retry storms".

        A node that raised would roll its step back and hand it to the queue's
        five-attempt ladder — one dead endpoint, six calls per contact.
        """

        def _timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("too slow", request=request)

        fake_internet(_timeout).install(monkeypatch)
        flow = branching_flow(tenancy.workspace, fallback_handle_on_error=True)

        run(tenancy.workspace, flow)

        assert not ScheduledAction.objects.for_workspace(tenancy.workspace.pk).exists()

    def test_a_hostile_redirect_location_routes_rather_than_raising(self, tenancy, monkeypatch):
        """The far end is a stranger's server and picks the ``Location`` header.

        httpx parses it even when told not to follow redirects, and its
        ``InvalidURL`` is not an ``HTTPError`` — so an unguarded version of this
        escapes the node, rolls the step back and lets the *remote server*
        decide when this deployment retries.
        """
        fake_internet(serving(status=302, headers={"Location": "javascript:alert(1)"})).install(monkeypatch)
        flow = branching_flow(tenancy.workspace, fallback_handle_on_error=True)

        execution = run(tenancy.workspace, flow)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "bad"
        assert not ScheduledAction.objects.for_workspace(tenancy.workspace.pk).exists()

    @pytest.mark.parametrize("charset", ["undefined", "idna", "punycode"])
    def test_a_hostile_content_type_charset_does_not_crash_the_step(self, tenancy, monkeypatch, charset):
        """The far end picks ``Content-Type``, and the node decodes every body.

        These three codec names pass a "can this encode?" check and then raise on
        decode. ``UnicodeError`` is not an ``OutboundError``, so it escaped the
        node, rolled the step back and put the request on the queue's retry
        ladder — with a stranger's server choosing when that happened.
        """

        def handler(request):
            return httpx.Response(
                200, content=b'\xff\xfe{"id": "u-1"}', headers={"Content-Type": f"text/plain; charset={charset}"}
            )

        fake_internet(handler).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            fallback_handle_on_error=True,
            response_mappings=[{"json_path": "$.id", "target_type": "variable", "target": "external_id"}],
        )

        execution = run(tenancy.workspace, flow)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "ok", "a 200 is a 200 whatever the charset header says"
        assert not ScheduledAction.objects.for_workspace(tenancy.workspace.pk).exists()

    def test_a_compressed_body_is_declined_rather_than_expanded(self, tenancy, monkeypatch):
        """The guard asks for identity; a body compressed anyway has no bound."""
        import gzip

        bomb = gzip.compress(b"A" * (32 * 1024 * 1024))

        def handler(request):
            return httpx.Response(200, content=bomb, headers={"Content-Encoding": "gzip"})

        fake_internet(handler).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.id", "target_type": "variable", "target": "external_id"}],
        )

        execution = run(tenancy.workspace, flow)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "ok"
        assert "external_id" not in execution.variables, "a declined body maps nothing"
        assert execution.variables[summary_key("req")]["body"] == ""

    def test_without_the_flag_a_failure_takes_the_default_handle(self, tenancy, monkeypatch):
        """``fallback_handle_on_error`` is "Follow the error handle on failure"."""
        fake_internet(serving(status=500)).install(monkeypatch)
        flow = branching_flow(tenancy.workspace)

        assert run(tenancy.workspace, flow).current_node_id == "ok"

    def test_an_error_handle_with_no_edge_ends_the_run(self, tenancy, monkeypatch):
        """SPEC §9.2: "Missing edge for a handle -> End"."""
        fake_internet(serving(status=500)).install(monkeypatch)
        flow = published_flow(
            tenancy.workspace,
            graph(
                [request_node(fallback_handle_on_error=True), node("ok", "action", SINK, x=200)],
                [edge("req", "default", "ok")],
            ),
        )

        execution = run(tenancy.workspace, flow)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "req"

    def test_a_url_that_renders_empty_routes_rather_than_failing(self, tenancy, monkeypatch):
        fake_internet(serving({})).install(monkeypatch)
        flow = branching_flow(tenancy.workspace, url="{{missing_field}}", fallback_handle_on_error=True)

        execution = run(tenancy.workspace, flow)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "bad"

    def test_a_node_with_no_url_at_all_fails_the_run(self, tenancy, monkeypatch):
        """Static and unfixable by retrying, unlike everything else here."""
        fake_internet(serving({})).install(monkeypatch)
        flow = published_flow(
            tenancy.workspace, graph([node("req", "external_request", {"method": "GET", "url": " "})])
        )

        execution = run(tenancy.workspace, flow)

        assert execution.status == ExecutionStatus.FAILED
        assert "no URL" in execution.last_error


@pytest.mark.django_db
class TestResponseMappings:
    def test_a_nested_path_lands_in_a_variable(self, tenancy, monkeypatch):
        fake_internet(serving({"data": {"user": {"id": "u-42"}}})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.data.user.id", "target_type": "variable", "target": "user_id"}],
        )

        execution = run(tenancy.workspace, flow)

        assert execution.variables["user_id"] == "u-42"

    def test_an_array_index_works(self, tenancy, monkeypatch):
        fake_internet(serving({"items": [{"sku": "a"}, {"sku": "b"}]})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.items[1].sku", "target_type": "variable", "target": "sku"}],
        )

        assert run(tenancy.workspace, flow).variables["sku"] == "b"

    def test_a_number_lands_in_a_number_field_typed(self, tenancy, monkeypatch):
        field = create_custom_field(tenancy.workspace, name="Order total", field_type="number")
        fake_internet(serving({"order": {"total": 24.5}})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.order.total", "target_type": "custom_field", "target": "Order total"}],
        )

        execution = run(tenancy.workspace, flow)

        assert float(field_values_for(execution.contact)[field.pk]) == 24.5

    def test_a_json_boolean_lands_in_a_boolean_field(self, tenancy, monkeypatch):
        field = create_custom_field(tenancy.workspace, name="VIP", field_type="boolean")
        fake_internet(serving({"vip": True})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.vip", "target_type": "custom_field", "target": "vip"}],
        )

        execution = run(tenancy.workspace, flow)

        assert field_values_for(execution.contact)[field.pk] is True

    def test_a_json_number_lands_in_a_text_field(self, tenancy, monkeypatch):
        """``$.id`` is a number as often as a string, and refusing it is pedantry."""
        field = create_custom_field(tenancy.workspace, name="External id", field_type="text")
        fake_internet(serving({"id": 4172})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.id", "target_type": "custom_field", "target": "External id"}],
        )

        execution = run(tenancy.workspace, flow)

        assert field_values_for(execution.contact)[field.pk] == "4172"

    def test_a_downstream_node_sees_the_variable(self, tenancy, monkeypatch):
        """The acceptance criterion: "downstream nodes see the variables"."""
        create_custom_field(tenancy.workspace, name="Plan", field_type="text")
        fake_internet(serving({"plan": "gold"})).install(monkeypatch)
        flow = published_flow(
            tenancy.workspace,
            graph(
                [
                    request_node(
                        response_mappings=[{"json_path": "$.plan", "target_type": "variable", "target": "plan"}]
                    ),
                    node(
                        "set",
                        "action",
                        {"actions": [{"verb": "set_field", "field": "Plan", "value": "{{plan}}"}]},
                        x=200,
                    ),
                ],
                [edge("req", "default", "set")],
            ),
        )

        execution = run(tenancy.workspace, flow)

        assert list(field_values_for(execution.contact).values()) == ["gold"]

    @pytest.mark.parametrize(
        ("payload", "path", "reason"),
        [
            ({"a": 1}, "$.b.c", "the path is not in the document"),
            ({"a": [1]}, "$.a[9]", "the index is past the end"),
            ({"a": 1}, "$.a[?(@.b)]", "a filter expression is outside the supported grammar"),
            ({"a": 1}, "$..a", "recursive descent is outside it too"),
        ],
    )
    def test_a_mapping_that_cannot_be_applied_is_skipped(self, tenancy, monkeypatch, payload, path, reason):
        fake_internet(serving(payload)).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": path, "target_type": "variable", "target": "value"}],
        )

        execution = run(tenancy.workspace, flow)

        assert "value" not in execution.variables, reason
        assert execution.current_node_id == "ok", "a skipped mapping is not a failed request"

    def test_a_body_exactly_at_the_size_cap_still_maps(self, tenancy, monkeypatch, settings):
        """A complete response wrongly flagged truncated loses every mapping."""
        payload = b'{"id":"u-1"}'
        settings.EXTERNAL_REQUEST_MAX_RESPONSE_BYTES = len(payload)

        def handler(request):
            def chunks():
                yield payload

            return httpx.Response(200, content=chunks())

        fake_internet(handler).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.id", "target_type": "variable", "target": "external_id"}],
        )

        execution = run(tenancy.workspace, flow)

        assert execution.variables["external_id"] == "u-1"
        assert execution.variables[summary_key("req")]["truncated"] is False

    def test_a_deeply_nested_body_skips_mappings_without_crashing(self, tenancy, monkeypatch):
        """``json.loads`` raises ``RecursionError`` on this, not ``ValueError``.

        A few kilobytes, nowhere near the size cap, and the exception is not
        what the node catches — so it escaped the no-raise contract, rolled the
        step back and had the queue call the endpoint again.
        """
        bomb = b"[" * 10_000 + b"0" + b"]" * 10_000

        def handler(request):
            return httpx.Response(200, content=bomb, headers={"Content-Type": "application/json"})

        fake_internet(handler).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.a", "target_type": "variable", "target": "value"}],
        )

        execution = run(tenancy.workspace, flow)

        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.current_node_id == "ok"
        assert "value" not in execution.variables
        assert not ScheduledAction.objects.for_workspace(tenancy.workspace.pk).exists()

    def test_a_non_json_body_skips_every_mapping(self, tenancy, monkeypatch):
        fake_internet(serving(status=200, text="<html>fine, actually</html>")).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.a", "target_type": "variable", "target": "value"}],
        )

        execution = run(tenancy.workspace, flow)

        assert "value" not in execution.variables
        assert execution.current_node_id == "ok"

    def test_an_unknown_custom_field_is_skipped(self, tenancy, monkeypatch):
        fake_internet(serving({"a": "x"})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.a", "target_type": "custom_field", "target": "Deleted last week"}],
        )

        execution = run(tenancy.workspace, flow)

        assert field_values_for(execution.contact) == {}
        assert execution.current_node_id == "ok"

    def test_a_value_of_the_wrong_type_for_the_field_is_skipped(self, tenancy, monkeypatch):
        create_custom_field(tenancy.workspace, name="Signed up", field_type="date")
        fake_internet(serving({"when": "not a date"})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.when", "target_type": "custom_field", "target": "Signed up"}],
        )

        execution = run(tenancy.workspace, flow)

        assert field_values_for(execution.contact) == {}
        assert execution.current_node_id == "ok"

    def test_mappings_do_not_run_on_a_failed_request(self, tenancy, monkeypatch):
        """SPEC §11.7 maps on 2xx; an error body is not the shape the author mapped."""
        fake_internet(serving({"error": "nope"}, status=500)).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.error", "target_type": "variable", "target": "value"}],
        )

        assert "value" not in run(tenancy.workspace, flow).variables


class TestPathParsing:
    """The supported grammar, stated as a table so its edges are visible."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("$.a.b", ("a", "b")),
            ("a.b", ("a", "b")),
            ("$", ()),
            ("$.items[0]", ("items", 0)),
            ("$.items[0].sku", ("items", 0, "sku")),
            ('$["order id"]', ("order id",)),
            ("$['order id'].total", ("order id", "total")),
            ("$.a-b_c", ("a-b_c",)),
        ],
    )
    def test_supported_paths(self, raw, expected):
        assert parse_path(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "$..a",
            "$.a[*]",
            "$[?(@.price < 10)]",
            "$.a[",
            "$.",
            "$.a..b",
            "$.a[b]",
            "a b",
            "$.a()",
            "." * 40,
        ],
    )
    def test_unsupported_paths_are_refused_rather_than_guessed(self, raw):
        assert parse_path(raw) is None


@pytest.mark.django_db
class TestSecrecyAndSummary:
    def test_header_values_never_reach_the_logs_or_the_execution(self, tenancy, monkeypatch, caplog, secret_value):
        """SECURITY-BASELINE §5, and not by leaning on the global scrubber.

        ``secret_value`` is deliberately shapeless — no ``Bearer``, no
        recognisable prefix — so this passes only because the node never formats
        a header anywhere, not because a regex happened to match it.
        """
        fake_internet(serving({"ok": True})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            headers=[
                {"name": "Authorization", "value": f"Bearer {secret_value}"},
                {"name": "X-Api-Key", "value": secret_value},
            ],
        )

        with caplog.at_level("DEBUG"):
            execution = run(tenancy.workspace, flow)

        assert secret_value not in caplog.text
        assert secret_value not in str(execution.variables)
        assert secret_value not in execution.last_error

    def test_a_secret_in_the_url_query_stays_out_of_the_summary(self, tenancy, monkeypatch, secret_value):
        """The summary records the host, not the URL, for exactly this reason."""
        fake_internet(serving(status=500)).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            url=f"https://{API}/orders?api_key={secret_value}",
            fallback_handle_on_error=True,
        )

        execution = run(tenancy.workspace, flow)

        assert secret_value not in str(execution.variables)
        assert execution.variables[summary_key("req")]["host"] == API

    def test_the_summary_records_the_status_duration_and_body(self, tenancy, monkeypatch):
        fake_internet(serving({"detail": "over quota"}, status=429)).install(monkeypatch)
        flow = branching_flow(tenancy.workspace, fallback_handle_on_error=True)

        summary = run(tenancy.workspace, flow).variables[summary_key("req")]

        assert summary["status"] == 429
        assert summary["duration_ms"] >= 0
        assert "over quota" in summary["body"]
        assert "answered 429" in summary["error"]

    def test_the_summary_is_unreachable_from_a_placeholder(self, tenancy, monkeypatch):
        """Its key holds a ``:``, which the renderer's token grammar has no room for."""
        create_custom_field(tenancy.workspace, name="Leak", field_type="text")
        fake_internet(serving({"ok": True})).install(monkeypatch)
        flow = published_flow(
            tenancy.workspace,
            graph(
                [
                    request_node(),
                    node(
                        "set",
                        "action",
                        {"actions": [{"verb": "set_field", "field": "Leak", "value": "[{{external_request:req}}]"}]},
                        x=200,
                    ),
                ],
                [edge("req", "default", "set")],
            ),
        )

        execution = run(tenancy.workspace, flow)

        assert list(field_values_for(execution.contact).values()) == ["[{{external_request:req}}]"]


@pytest.mark.django_db
class TestUntrustedResponses:
    """SECURITY-BASELINE §3: a mapped value is a stranger's, like message text."""

    def test_a_mapped_value_is_stored_literally_and_never_evaluated(self, tenancy, monkeypatch):
        create_custom_field(tenancy.workspace, name="Nickname", field_type="text")
        fake_internet(serving({"name": "{{email}}"})).install(monkeypatch)
        flow = published_flow(
            tenancy.workspace,
            graph(
                [
                    request_node(
                        response_mappings=[{"json_path": "$.name", "target_type": "variable", "target": "name"}]
                    ),
                    node(
                        "set",
                        "action",
                        {"actions": [{"verb": "set_field", "field": "Nickname", "value": "{{name}}"}]},
                        x=200,
                    ),
                ],
                [edge("req", "default", "set")],
            ),
        )

        execution = run(tenancy.workspace, flow, email="ada@example.test")

        assert list(field_values_for(execution.contact).values()) == ["{{email}}"], "no second substitution pass"

    def test_a_mapped_value_is_escaped_in_an_html_context(self, tenancy, monkeypatch):
        from apps.flows.rendering import context_for, render

        fake_internet(serving({"bio": "<script>alert(1)</script>"})).install(monkeypatch)
        flow = branching_flow(
            tenancy.workspace,
            response_mappings=[{"json_path": "$.bio", "target_type": "variable", "target": "bio"}],
        )

        execution = run(tenancy.workspace, flow)
        context = context_for(execution.contact, execution.variables)

        assert render("<p>{{bio}}</p>", context, mode="html") == "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"
        assert render("{{bio}}", context) == "<script>alert(1)</script>"


@pytest.mark.django_db
class TestUrlEncodingAndTimeout:
    def test_a_placeholder_cannot_add_a_path_or_a_query(self, tenancy, monkeypatch):
        """A contact-supplied value is a value, not a piece of the URL's structure."""
        create_custom_field(tenancy.workspace, name="Ref", field_type="text")
        internet = fake_internet(serving({})).install(monkeypatch)
        flow = branching_flow(tenancy.workspace, url=f"https://{API}/orders/{{{{first_name}}}}")

        run(tenancy.workspace, flow, first_name="../../admin?token=stolen&x=1")

        url = internet.requests[0].url
        # ``raw_path``, not ``path``: httpx decodes the latter for display, and
        # what is on the wire is the claim being made here.
        assert url.raw_path == b"/orders/..%2F..%2Fadmin%3Ftoken%3Dstolen%26x%3D1"
        assert dict(url.params) == {}, "a value may not become a query parameter"

    def test_the_configured_timeout_reaches_the_guard(self, tenancy, monkeypatch):
        """SPEC §11.7 caps it at 10s — and the worker holds the contact lock meanwhile."""
        seen: list[float] = []
        real = outbound.guarded_request

        def _record(method, url, **kwargs):
            seen.append(kwargs["timeout"])
            return real(method, url, **kwargs)

        fake_internet(serving({})).install(monkeypatch)
        monkeypatch.setattr("apps.flows.engine.nodes.external_request.guarded_request", _record)
        run(tenancy.workspace, branching_flow(tenancy.workspace, timeout_s=3))

        assert seen == [3.0]


class TestTheTimeoutClamp:
    """The schema bounds ``timeout_s`` to 1–10, so these values cannot be
    published — which is exactly why the clamp is tested directly. A graph
    reaches the runner from a draft preview and from fixtures as well as from
    ``publish()``, and only one of those three went through validation."""

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [(3, 3.0), (10, 10.0), (1, 1.0), (99, 10.0), (0, 1.0), (-5, 1.0), ("nonsense", 10.0), (None, 10.0)],
    )
    def test_the_timeout_is_clamped_into_specs_range(self, configured, expected):
        assert node_timeout({"timeout_s": configured}) == expected

    def test_an_absent_timeout_is_the_cap(self):
        assert node_timeout({}) == 10.0


class TestTheInlineFlag:
    def test_the_node_is_never_synchronous_safe(self):
        """SPEC §11.7: "Runs in worker only (never inline)". L4-A reads this."""
        assert synchronous_safe("external_request") is False
