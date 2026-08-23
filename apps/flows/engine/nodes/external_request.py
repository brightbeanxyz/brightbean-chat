"""SPEC §11.7 — the one node that makes an outbound call, and the only one.

    Config: method, url (placeholders allowed), headers[], body (json template),
    timeout_s (max 10), response_mappings[] {json_path, target custom_field or
    variable}, fallback_handle_on_error bool. Handles: default, error. Runs in
    worker only (never inline). SSRF guard: deny requests resolving to
    private/loopback/link-local ranges and the deployment's own host.

Every interesting decision in this module comes from one fact: the URL is
written by a flow author and fetched by the server. Three consequences, and they
run in both directions.

**Outbound, the request is not trusted.** It goes through
:func:`apps.common.outbound.guarded_request` — SECURITY-BASELINE §6's mandatory
path, which resolves and validates the host, pins the address so DNS cannot
rebind, re-checks every redirect and caps the body. Nothing here opens a socket
itself. The URL's ``{{placeholders}}`` render in ``url`` mode, so a contact whose
first name is ``../admin?token=`` percent-encodes into a path segment rather than
becoming one.

**Inbound, the response is not trusted either.** SECURITY-BASELINE §3: "Variables
sourced from External Request responses are untrusted like contact input." They
are written straight into ``variables``, where the shared renderer escapes them
in HTML contexts and never evaluates them anywhere. That is the same treatment a
stranger's message text gets, which is the correct comparison — the far end of
this request is a stranger's server.

**The node never raises.** :mod:`apps.flows.engine.runner` deliberately does not
catch, so an exception here would roll the step back and hand it to the queue's
five-attempt backoff ladder — a failing endpoint would then be called six times
per contact instead of once. SPEC §9.5 already settles the shape for a provider
failure ("fails the message and follows the edge onward"), and this is the same
case: a timeout is a *result*, routed through a handle, not a broken run. Only a
graph that could never work — a node with no URL at all — is a ``Fail``.

**Never inline.** ``synchronous_safe = False``, which SPEC §7.1 requires and
which L4-A's budget reads through
:func:`apps.flows.engine.registry.synchronous_safe`. A node that may wait ten
seconds cannot run inside a webhook's 1.5-second budget. Worth knowing that the
worker holds the contact's advisory lock for the duration (SPEC §9.6), so the
``timeout_s`` clamp is also the bound on how long one contact is blocked — the
same trade ``SEND_BUCKET_MAX_WAIT_SECONDS`` documents.

**Headers are never logged or stored.** A header value is where the API key
lives. The global scrubber (:mod:`apps.common.logging`) would catch most of
them, but "most" is not a security control: the summary this node records omits
headers entirely, and no log line in this module formats one.
"""

import logging
import time
from typing import Any

from apps.common.logging import scrub
from apps.common.outbound import GuardedResponse, OutboundError, guarded_request
from apps.contacts.errors import ContactsError
from apps.contacts.services import set_field_value
from apps.flows.engine.context import NodeContext
from apps.flows.engine.fields import custom_field_by_name, typed_for
from apps.flows.engine.nodes.base import Node
from apps.flows.engine.registry import register_node
from apps.flows.engine.results import Continue, Fail, StepResult

__all__ = ["ExternalRequestNode", "summary_key"]

logger = logging.getLogger(__name__)

#: SPEC §11.7: "timeout_s (max 10)". The schema already bounds the field; this
#: clamp is what makes the bound true for a graph that reached the runner some
#: other way — a draft preview, a fixture, a hand-edited document.
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 10

#: How much of the response body the debugging summary keeps. Small on purpose:
#: it lands in a JSON column that is read back on every step of every run, and
#: two kilobytes is enough to see which error the far end returned.
SUMMARY_MAX_BODY_CHARS = 2000

#: A path deeper than this is not a mapping anybody wrote by hand.
MAX_PATH_STEPS = 32

#: Methods that carry the configured body. GET is excluded because a GET with a
#: JSON body is a request many servers and every intermediary mishandle, and an
#: author who wrote one meant to put the values in the query string.
_METHODS_WITH_BODY = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: "Not found", distinct from a JSON ``null`` that really was at that path.
_MISSING = object()


def summary_key(node_id: str) -> str:
    """The variable slot this node's request/response summary is kept in.

    The colon is doing real work, exactly as it does in
    :func:`apps.flows.engine.nodes.randomizer.variable_key`: the renderer's
    token grammar (``PLACEHOLDER_PATTERN``) has no ``:`` in its alphabet, so
    this key is unreachable from any ``{{placeholder}}`` and cannot collide with
    a variable an author named. Debug state is therefore visible to an admin
    reading the execution row and invisible to the flow itself.
    """
    return f"external_request:{node_id}"


@register_node
class ExternalRequestNode(Node):
    """Call somebody else's API, map the answer back into the run."""

    type = "external_request"
    # SPEC §7.1's inline-safe list is "send message, action, condition,
    # randomizer, start flow". §11.7 says this one runs "in worker only, never
    # inline", and this attribute is the whole enforcement.
    synchronous_safe = False

    def execute(self, ctx: NodeContext) -> StepResult:
        raw_url = ctx.config.get("url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            # Static, and the schema requires it — a graph in this state cannot
            # be fixed by retrying, so it fails rather than routing.
            return Fail(f"external_request node {ctx.node_id} has no URL to request")

        method = str(ctx.config.get("method") or "GET").upper()
        url = ctx.render(raw_url, mode="url").strip()
        started = time.monotonic()

        if not url:
            # Runtime, not static: the URL was a placeholder that resolved to
            # nothing. That is data, so it routes like any other failure.
            return self._failed(ctx, "the URL rendered empty", started=started)

        body = ctx.render_json(ctx.config.get("body")) if method in _METHODS_WITH_BODY else None
        try:
            response = guarded_request(
                method,
                url,
                headers=_headers(ctx),
                json=body,
                timeout=_timeout(ctx.config),
            )
        except OutboundError as exc:
            return self._failed(ctx, str(exc), started=started)

        if not response.ok:
            return self._failed(
                ctx,
                f"the request was answered {response.status_code}",
                started=started,
                response=response,
            )

        self._record(ctx, response=response, elapsed_ms=response.elapsed_ms, error="")
        _apply_mappings(ctx, response)
        return Continue("default")

    # -- outcomes -----------------------------------------------------------

    def _failed(
        self,
        ctx: NodeContext,
        reason: str,
        *,
        started: float,
        response: GuardedResponse | None = None,
    ) -> StepResult:
        """Record what went wrong and route on it. Never raises, never fails."""
        elapsed_ms = response.elapsed_ms if response is not None else int((time.monotonic() - started) * 1000)
        self._record(ctx, response=response, elapsed_ms=elapsed_ms, error=reason)
        logger.warning(
            "Execution %s: external_request node %s did not succeed: %s",
            ctx.execution.pk,
            ctx.node_id,
            reason,
        )
        return Continue(_error_handle(ctx.config))

    def _record(
        self,
        ctx: NodeContext,
        *,
        response: GuardedResponse | None,
        elapsed_ms: int,
        error: str,
    ) -> None:
        """Issue #15's "request/response summary recorded on the execution".

        Status, duration and a truncated body — and the **host**, not the URL,
        because a user-authored URL's query string is a routine place for an API
        key to be (SECURITY-BASELINE §5). Headers appear nowhere at all. The body
        is scrubbed on the way in as well: the far end can echo a credential back
        in an error message, and this column is read in the admin.

        Written through :meth:`NodeContext.set_variable`, so the runner persists
        it with the rest of ``variables`` at the next pause or terminal state —
        no second write path to the execution row, and no migration.
        """
        body = ""
        if response is not None and response.content:
            # ``text_prefix`` rather than ``text[:n]``: the latter decodes the
            # whole body — up to the guard's megabyte cap — to keep two
            # kilobytes of it, inside the worker transaction with the contact's
            # advisory lock held.
            body = scrub(response.text_prefix(SUMMARY_MAX_BODY_CHARS))
        ctx.set_variable(
            summary_key(ctx.node_id),
            {
                "host": response.final_host if response is not None else "",
                "status": response.status_code if response is not None else None,
                "duration_ms": elapsed_ms,
                "body": body,
                "truncated": bool(response.truncated) if response is not None else False,
                "error": scrub(error)[:SUMMARY_MAX_BODY_CHARS],
            },
        )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _error_handle(config: dict[str, Any]) -> str:
    """Which handle a failure leaves by.

    ``fallback_handle_on_error`` is labelled "Follow the error handle on
    failure" in the builder (``frontend/builder/src/inspector/copy.ts``), so
    true means ``error`` and anything else means ``default``. Either way SPEC
    §9.2 applies to what happens next — "missing edge for a handle -> End" — so
    an author who drew no error branch gets a run that finishes rather than one
    that hangs.
    """
    return "error" if config.get("fallback_handle_on_error") else "default"


def _timeout(config: dict[str, Any]) -> float:
    """``timeout_s`` clamped into SPEC §11.7's range, defaulting to the cap."""
    try:
        value = int(config.get("timeout_s"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return float(MAX_TIMEOUT_S)
    return float(min(max(value, MIN_TIMEOUT_S), MAX_TIMEOUT_S))


def _headers(ctx: NodeContext) -> dict[str, str]:
    """The configured headers, placeholders rendered.

    Rendered in ``text`` mode, not ``url``: a header value is not a URL, and
    percent-encoding an API key would send the wrong key. The guard drops
    anything unsafe to forward — control characters, ``Host`` — so this does not
    re-check it.

    No header **value** is logged, at any level. See the module docstring — the
    names are the author's own text and are safe to name; the values are where
    the API key lives.
    """
    rendered: dict[str, str] = {}
    for entry in ctx.config.get("headers") or []:
        if not isinstance(entry, dict):
            continue
        name = ctx.render(entry.get("name")).strip()
        if not name:
            # A header whose name renders empty — a non-string in the graph
            # document, or a placeholder that resolved to nothing. Logged rather
            # than dropped in silence: "my API key header never arrived" is
            # otherwise a debugging session with no evidence in it.
            logger.info(
                "Execution %s: external_request node %s has a header whose name rendered empty; skipping it.",
                ctx.execution.pk,
                ctx.node_id,
            )
            continue
        if name in rendered:
            logger.info(
                "Execution %s: external_request node %s configures %r more than once; the last one wins.",
                ctx.execution.pk,
                ctx.node_id,
                name,
            )
        rendered[name] = ctx.render(entry.get("value"))
    return rendered


# ---------------------------------------------------------------------------
# Response mapping (SPEC §11.7's response_mappings[])
# ---------------------------------------------------------------------------


def _apply_mappings(ctx: NodeContext, response: GuardedResponse) -> None:
    """Write the configured pieces of the response into the run.

    Every failure here is a **skip with a log**, never an exception and never a
    change of handle. The request succeeded; that is what the ``default`` edge
    means. A mapping that cannot be applied — the body was not JSON, the path is
    not in it, the custom field was deleted last week — is a configuration
    problem for an admin to see in the log, not a reason to route a contact down
    an error branch they have nothing to do with.
    """
    mappings = [entry for entry in (ctx.config.get("response_mappings") or []) if isinstance(entry, dict)]
    if not mappings:
        return

    if response.truncated:
        logger.info(
            "Execution %s: external_request node %s got a response over the size cap; mappings skipped.",
            ctx.execution.pk,
            ctx.node_id,
        )
        return
    try:
        document = response.json()
    except ValueError:
        logger.info(
            "Execution %s: external_request node %s got a non-JSON body; %s mapping(s) skipped.",
            ctx.execution.pk,
            ctx.node_id,
            len(mappings),
        )
        return

    for mapping in mappings:
        raw_path = mapping.get("json_path")
        steps = parse_path(raw_path) if isinstance(raw_path, str) else None
        if steps is None:
            logger.info(
                "Execution %s: external_request node %s cannot read %r; supported paths look like "
                "$.a.b, a[0].b or $['a b'].",
                ctx.execution.pk,
                ctx.node_id,
                raw_path,
            )
            continue

        value = _extract(document, steps)
        if value is _MISSING:
            logger.info(
                "Execution %s: external_request node %s found nothing at %r in the response.",
                ctx.execution.pk,
                ctx.node_id,
                raw_path,
            )
            continue

        target = mapping.get("target")
        if not isinstance(target, str) or not target.strip():
            continue
        if mapping.get("target_type") == "custom_field":
            _write_custom_field(ctx, target, value)
        else:
            ctx.set_variable(target.strip(), value)


def _write_custom_field(ctx: NodeContext, name: str, value: Any) -> None:
    """Store one mapped value on the contact, typed.

    Through ``set_field_value`` and :mod:`apps.flows.engine.fields`, the same
    two functions the action node's ``set_field`` verb uses — a response value
    is not a second kind of write and does not get a second code path into
    ``custom_field_value``.
    """
    try:
        field = custom_field_by_name(ctx, name)
        if field is None:
            return
        set_field_value(ctx.contact, field, typed_for(field, value))
    except (ContactsError, ValueError, TypeError) as exc:
        logger.info(
            "Execution %s: external_request node %s could not store the response value in %r: %s",
            ctx.execution.pk,
            ctx.node_id,
            name,
            exc,
        )


def parse_path(raw: str) -> tuple[str | int, ...] | None:
    """Parse a ``json_path`` into steps, or ``None`` when it is not one.

    A deliberately small subset of JSONPath — ``$`` for the root, ``.key``,
    ``["key"]``, ``['key']`` and ``[0]`` — which is every shape a mapping into a
    single field can need. The leading ``$`` is optional, so ``data.id`` and
    ``$.data.id`` are the same path.

    The subset is the security argument, not a shortcut. Full JSONPath has
    filter expressions (``$[?(@.price < 10)]``), recursive descent and, in most
    implementations, an expression evaluator — a language, run over a string a
    flow author typed, against a document a stranger's server returned. This
    grammar has no operators at all, so there is nothing in it to evaluate; it
    also means no dependency to audit (SECURITY-BASELINE §10).

    Anything outside the grammar returns ``None`` and is reported as a skipped
    mapping, which is the same outcome as a path that simply is not in the
    document — an author's mistake, logged, with the run carrying on.
    """
    text = raw.strip()
    if not text:
        return None
    if text.startswith("$"):
        text = text[1:]

    steps: list[str | int] = []
    index = 0
    while index < len(text):
        if len(steps) >= MAX_PATH_STEPS:
            return None
        char = text[index]

        if char == ".":
            index += 1
            start = index
            while index < len(text) and (text[index].isalnum() or text[index] in "_-"):
                index += 1
            if index == start:
                return None
            steps.append(text[start:index])
            continue

        if char == "[":
            close = text.find("]", index)
            if close == -1:
                return None
            inner = text[index + 1 : close].strip()
            index = close + 1
            if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "\"'":
                steps.append(inner[1:-1])
            elif inner.lstrip("-").isdigit():
                steps.append(int(inner))
            else:
                return None
            continue

        if not steps and (char.isalnum() or char in "_-"):
            start = index
            while index < len(text) and (text[index].isalnum() or text[index] in "_-"):
                index += 1
            steps.append(text[start:index])
            continue

        return None

    return tuple(steps)


def _extract(document: Any, steps: tuple[str | int, ...]) -> Any:
    """Walk ``steps`` through ``document``, or :data:`_MISSING`.

    A missing key and a stored ``null`` are different answers: the sentinel is
    what lets a mapping whose value really is ``null`` write ``None`` into a
    variable, while a typo'd path is skipped and logged.
    """
    current = document
    for step in steps:
        if isinstance(step, int):
            if not isinstance(current, list):
                return _MISSING
            try:
                current = current[step]
            except IndexError:
                return _MISSING
            continue
        if not isinstance(current, dict) or step not in current:
            return _MISSING
        current = current[step]
    return current
