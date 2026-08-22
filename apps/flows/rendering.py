"""``{{placeholder}}`` substitution — the one renderer, and the SSTI ban.

SECURITY-BASELINE §3 names this module by path and states the rule it exists to
enforce:

    ``{{placeholder}}`` rendering is **plain token substitution** via the one
    shared renderer. User- or contact-supplied content is **never** evaluated by
    Django/Jinja template engines.

Every node that renders author text goes through :func:`render`, and nothing in
this repository may render flow text any other way. The reason is the threat
model in one line: a flow author writes the template, but a *stranger* supplies
the values — a contact's first name, a comment body, an External Request
response — and every template engine in Python treats its input as a small
programming language. ``Template("{{ x }}").render({"x": user_input})`` is safe;
``Template(user_input).render(...)`` is remote code execution, and the two are
one refactor apart. There is no template engine here at all, so that refactor
has nowhere to land.

**How the substitution is safe, structurally**

* One :func:`re.sub` pass with a *replacement callable*. The callable's return
  value is never rescanned, so a contact called ``{{email}}`` renders as the
  literal text ``{{email}}`` rather than leaking their address — and a value
  containing ``{% … %}``, ``${…}`` or ``#{…}`` is equally inert, because no
  second pass ever looks at it.
* Tokens are matched against a **fixed grammar** and resolved by dictionary
  lookup. There is no attribute access, no indexing, no call syntax and no
  filter pipeline to be abused; ``{{ contact.__class__ }}`` is not a token, so
  it is left alone as literal text.
* Unknown tokens render as empty string. Leaving them literal would echo the
  author's template back to the contact, and echoing ``{{ secret_field }}`` at
  someone is a worse default than a gap in a sentence.

**Two modes.** Plain text everywhere except HTML bodies. In ``html`` mode the
*substituted value* is HTML-escaped and the surrounding template is not: the
template is the author's own markup (SPEC §11.10's ``html_body``), and the value
is the stranger's. Escaping the template would render an email as source code;
not escaping the value is stored XSS. The email node (L5-E) is the only planned
caller of ``html``; it is tested here so the mode cannot arrive untested.

**Where the values come from** (SPEC §9.2's context, in this precedence order):

1. **System fields** — ``first_name``, ``last_name``, ``email``, ``phone``.
2. **Custom fields by name**, case-insensitively.
3. **Variables by key** — including values an External Request wrote, which
   SECURITY-BASELINE §3 declares untrusted exactly like contact input.

The order is fixed and total: a workspace that names a custom field
``first_name`` does not get to shadow the system field, and a variable never
shadows either. Deterministic beats clever here — a flow author debugging a
wrong name should not have to know which of three sources won.

Not to be confused with :mod:`apps.common.placeholders`, which recognises
placeholder *secrets* (``change-me-…``) and has nothing to do with rendering.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.utils.html import escape

__all__ = [
    "MAX_RENDERED_CHARS",
    "PLACEHOLDER_PATTERN",
    "SYSTEM_FIELDS",
    "RenderContext",
    "context_for",
    "render",
    "render_json",
]

logger = logging.getLogger(__name__)

#: SPEC §9.2: "system fields: first_name, last_name, email, phone".
#:
#: Deliberately not every column on ``Contact``. ``status`` is a soft-delete
#: marker and ``last_interaction_at`` is bookkeeping; neither belongs in a
#: sentence addressed to the person they describe, and an allowlist is the only
#: thing standing between "render a field" and "render any attribute".
SYSTEM_FIELDS: tuple[str, ...] = ("first_name", "last_name", "email", "phone")

#: ``{{ token }}`` with optional inner whitespace.
#:
#: The token alphabet is letters, digits, underscore, hyphen, dot and space —
#: enough for a custom field called "Order Number" or "utm.source", and nothing
#: else. No brackets, no parentheses, no pipes, no quotes: every character a
#: template language would need to express a call, an index or a filter is
#: absent from the grammar, so those expressions are not tokens and are never
#: substituted. The length cap keeps a hostile 4 KiB "token" from being scanned
#: as one.
PLACEHOLDER_PATTERN = r"\{\{\s*([A-Za-z0-9_.\- ]{1,120}?)\s*\}\}"

_PLACEHOLDER_RE = re.compile(PLACEHOLDER_PATTERN)

#: A rendered block is going to a messaging API with its own limits, and a
#: hostile field value should not be able to turn a 20-character template into a
#: megabyte of outbound body. Applied after substitution, to the whole string.
MAX_RENDERED_CHARS = 20_000

#: Per-value cap, applied before the value is spliced in. Together with the
#: template's own schema limit this bounds the output without depending on how
#: many placeholders the author used.
MAX_VALUE_CHARS = 4_096


@dataclass(frozen=True)
class RenderContext:
    """The three namespaces :func:`render` resolves against, already flattened.

    Built once per node execution and reused for every string that node renders
    — a ``send_message`` with ten blocks should not run ten custom-field
    queries. :func:`context_for` is the constructor; this class stays a dumb
    holder so a test can build one from literals without touching a database.

    Keys in ``custom_fields`` and ``variables`` are stored **case-folded** so
    lookup is case-insensitive without lowercasing at every call site.
    """

    system: dict[str, Any] = field(default_factory=dict)
    custom_fields: dict[str, Any] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)

    def lookup(self, token: str) -> Any | None:
        """Resolve one token, or ``None`` when no namespace claims it.

        ``None`` is "no such token", which is why a *stored* ``None`` is
        normalised to the empty string on the way in (:func:`context_for`):
        otherwise a custom field that exists but is unset would be logged as a
        typo on every render.
        """
        key = token.strip().casefold()
        if not key:
            return None
        if key in self.system:
            return self.system[key]
        if key in self.custom_fields:
            return self.custom_fields[key]
        return self.variables.get(key)


def context_for(
    contact: Any = None,
    variables: dict[str, Any] | None = None,
    *,
    custom_fields: dict[str, Any] | None = None,
) -> RenderContext:
    """Assemble a :class:`RenderContext` for one contact and one variable bag.

    ``custom_fields`` may be supplied by a caller that has already loaded them;
    otherwise they are read here, in one query, through
    :func:`apps.contacts.services.field_values_for` — the same typed-value
    accessor the CRM uses, so a date renders the way the rest of the product
    renders dates rather than however this module would have guessed.

    A ``None`` contact is legitimate: a broadcast preview or a unit test renders
    with variables alone.
    """
    system = {name: _coerce(getattr(contact, name, "")) for name in SYSTEM_FIELDS}

    fields: dict[str, Any] = {}
    if custom_fields is not None:
        fields = {str(name).casefold(): _coerce(value) for name, value in custom_fields.items()}
    elif contact is not None and getattr(contact, "pk", None) is not None:
        fields = _custom_fields_for(contact)

    bag = {str(key).casefold(): _coerce(value) for key, value in (variables or {}).items()}
    return RenderContext(system=system, custom_fields=fields, variables=bag)


def _custom_fields_for(contact: Any) -> dict[str, Any]:
    """``{casefolded field name: rendered value}`` for one contact.

    Imported inside the function because this module is imported from node
    classes that are themselves imported from ``AppConfig.ready()``, and a
    module-level import of another app's services would run during app
    population.
    """
    from apps.contacts.models import CustomField
    from apps.contacts.services import field_values_for

    values = field_values_for(contact)
    if not values:
        return {}
    names = CustomField.objects.for_workspace(contact.workspace_id).filter(pk__in=values).values_list("id", "name")
    return {str(name).casefold(): _coerce(values[field_id]) for field_id, name in names}


def _coerce(value: Any) -> str:
    """Render one value as plain text, deterministically and boundedly.

    Every branch here answers "what would a person expect to see in a sentence",
    which is not what ``str()`` gives for three of these types: ``True`` reads
    as ``True`` in Python and as *yes* in a message, a ``datetime`` carries a
    microsecond tail nobody wants, and a ``Decimal`` prints as
    ``Decimal('2.50')`` when it is passed through ``repr`` anywhere downstream.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        text = value.isoformat(timespec="minutes")
    elif isinstance(value, date):
        text = value.isoformat()
    elif isinstance(value, Decimal):
        # Normalise so 2.50 and 2.5 render alike, but keep integers integral
        # rather than turning 10 into 1E+1, which is what normalize() does.
        normalised = value.normalize()
        text = format(normalised, "f")
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    return text[:MAX_VALUE_CHARS]


def render(template: Any, context: RenderContext, *, mode: str = "text") -> str:
    """Substitute ``{{tokens}}`` in ``template`` from ``context``.

    ``mode="html"`` escapes each substituted value; the template itself is left
    as authored. See the module docstring for why that asymmetry is the correct
    one and not an oversight.

    A non-string template renders as the empty string rather than raising: node
    config is user-authored JSON that has been through schema validation, but
    this function is also called on values a later layer may add, and a flow
    should not die because one block carried a number where a string was
    expected.
    """
    if not isinstance(template, str) or not template:
        return ""
    if mode not in ("text", "html"):
        raise ValueError(f"render() mode must be 'text' or 'html', not {mode!r}.")

    def _replace(match: "re.Match[str]") -> str:
        token = match.group(1)
        value = context.lookup(token)
        if value is None:
            # Debug, not warning: an author leaving a placeholder for a field
            # they have not created yet is ordinary, and this runs once per
            # placeholder per send.
            logger.debug("Placeholder %r resolved to nothing; rendering it as empty.", token)
            return ""
        text = _coerce(value)
        # The escape happens *here*, on the value alone, and the result is not
        # rescanned — re.sub never re-examines what a replacement callable
        # returns. That single fact is what makes nested placeholders and
        # template syntax in contact data inert.
        return escape(text) if mode == "html" else text

    rendered = _PLACEHOLDER_RE.sub(_replace, template)
    if len(rendered) > MAX_RENDERED_CHARS:
        logger.warning("Rendered text exceeded %s characters and was truncated.", MAX_RENDERED_CHARS)
        rendered = rendered[:MAX_RENDERED_CHARS]
    return rendered


def render_json(value: Any, context: RenderContext, *, mode: str = "text", _depth: int = 0) -> Any:
    """Render every string inside a JSON-shaped document, structure preserved.

    For node config that is a JSON body rather than a sentence — SPEC §11.7's
    External Request template (L4-E) is the case this exists for. Keys are
    rendered as well as values, because a header name may legitimately be
    parameterised; both go through the same substitution, so neither can smuggle
    a template engine in.

    The depth cap mirrors the graph's own (``MAX_GRAPH_DEPTH``): config is
    user-authored and recursion on user-authored nesting is a stack overflow
    waiting for the right input.
    """
    if _depth > 20:
        return None
    if isinstance(value, str):
        return render(value, context, mode=mode)
    if isinstance(value, dict):
        return {
            render(key, context, mode=mode) if isinstance(key, str) else key: render_json(
                item, context, mode=mode, _depth=_depth + 1
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [render_json(item, context, mode=mode, _depth=_depth + 1) for item in value]
    return value
