"""Shared template tags and filters.

Ported from BrightBean Studio's ``apps/common/templatetags/common_extras.py``.
Behaviour is preserved exactly — several details here look like accidents and
are not; each is commented where it would otherwise be "cleaned up" into a bug.
"""

import json
from collections.abc import Iterable
from typing import Any

from django import template
from django.template.base import FilterExpression, NodeList, Parser, Token, token_kwargs
from django.template.context import Context
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

register = template.Library()

# The glyphs components/_filter_icon.html knows how to draw. Kept here so the
# trigger's `has-icon` padding and the partial's output cannot disagree: the
# template used to add 28px of left padding for ANY truthy icon name, so an
# unrecognised one indented the label past empty space.
FILTER_ICONS = frozenset({"status", "channel", "tag", "clock"})


# The platforms partials/_platform_icon.html draws a real glyph for. Anything
# else gets that partial's fallback glyph, and must get the matching neutral
# chip — see :func:`platform_class`.
#
# Issue #4 (L2-B) owns the canonical platform list on the channel model; this
# is only the render-time question of which glyph and which colour, so the two
# are allowed to differ (a platform can exist before it has artwork). Keep this
# in step with the branches in partials/_platform_icon.html.
PLATFORMS = frozenset({"telegram", "instagram", "messenger", "whatsapp", "sms", "email"})


@register.filter
def platform_class(platform: Any) -> str:
    """The ``.pi-*`` modifier for a platform key, falling back to ``pi-unknown``.

    Call sites used to interpolate ``pi-{{ key }}`` directly, which produced a
    class no stylesheet defines for any key outside :data:`PLATFORMS`. Paired
    with ``.pi-chip`` — which paints the glyph white for contrast against a
    coloured chip — that meant an unrecognised platform rendered a white glyph
    on a chip with no background: invisible on the white dropdown panel,
    exactly where the fallback is supposed to be doing its job.

    Unknown keys are the normal case here, not the exotic one: a platform
    reaches the UI as soon as issue #4 adds it to the model, whereas its
    artwork lands with the adapter, several layers later.
    """
    return f"pi-{platform}" if platform in PLATFORMS else "pi-unknown"


@register.filter
def platform_ink_class(platform: Any) -> str:
    """Like :func:`platform_class` but tints the glyph instead of a chip.

    Unknown keys get no tint at all rather than a made-up one: the fallback
    glyph inherits the surrounding text colour, which is always legible.
    """
    return f"pi-ink-{platform}" if platform in PLATFORMS else ""


@register.filter
def lines(value: Any) -> str:
    """Join a list onto one line each, for a ``<textarea>``.

    ``{{ items|join:"<newline>" }}`` cannot be written in a Django template, and
    the way it fails is silent and expensive. Django's lexer is
    ``re.compile(r"({%.*?%}|{{.*?}}|{#.*?#})")`` with **no** ``re.DOTALL``, so a
    tag broken across two lines to hold a literal newline is not recognised as a
    tag at all: it is printed as text. The comment-trigger form did exactly that
    for four fields, so opening it pre-filled the boxes with the string
    ``{{ config.post_ids|join:"``, and saving stored that as a post id — leaving
    a trigger scoped to posts that do not exist, which matches nothing and says
    nothing about why.

    Non-strings are rendered with ``str`` so a list of ids reads the same as a
    list of words. A non-list returns empty rather than its repr: this fills a
    form field, and a stray ``None`` there would be saved back as content.
    """
    if isinstance(value, str) or not isinstance(value, Iterable):
        return ""
    return "\n".join(str(item) for item in value)


@register.filter(is_safe=True)
def json_attr(value: Any) -> SafeString:
    """Serialize a value as a JSON literal safe to embed in an HTML attribute.

    Used for Alpine's ``x-data``. ``json.dumps`` produces the JSON, then
    HTML-escaping covers ``& < > " '`` — crucially the ``"`` that JSON uses for
    every key and string, which would otherwise terminate the attribute. The
    browser HTML-decodes the attribute value before the JS engine parses it, so
    Alpine still sees valid JSON.

    The order matters and is easy to invert: escape the *output* of
    ``json.dumps``, then ``mark_safe``. Marking safe first, or reaching for
    ``|safe`` on a pre-serialized string, reopens attribute injection.

    Pass Python values (list/dict/None), NOT pre-serialized JSON strings — a
    JSON string input gets double-encoded into a quoted string literal.

    ``None`` and ``""`` both become ``[]``. The empty string is Django's
    ``string_if_invalid`` fallback for a template variable that does not
    resolve, so an unresolvable name yields an empty Alpine array rather than a
    JS syntax error that breaks the whole component.
    """
    if value is None or value == "":
        return mark_safe(escape("[]"))  # noqa: S308 - escaped on the line above; see docstring
    return mark_safe(  # noqa: S308 - escape() runs on the json.dumps output first
        escape(json.dumps(value, ensure_ascii=False, default=str))
    )


@register.inclusion_tag("components/ui_select.html")
def ui_select(
    *,
    model: str,
    options: Iterable[Any],
    multiple: bool = False,
    onchange: str = "",
    placeholder: str = "Select",
    value_field: str = "id",
    label_field: str = "",
    icon_field: str = "",
    icon: str = "",
) -> dict[str, Any]:
    """A styled single/multi select dropdown (Alpine + checkbox/click list).

    A drop-in upgrade for a plain ``<select>`` in an Alpine/HTMX toolbar. The
    panel is ``position: fixed`` and anchored on open, so an ``overflow`` filter
    row cannot clip it. Bind it to a property in the enclosing ``x-data`` scope:
    an **array** when ``multiple`` (empty = "all"), otherwise a **string**.

    Params:
      model        Alpine expression holding the selection, e.g. "filters.status".
      options      iterable of model instances, ``(value, label)`` pairs, plain
                   strings, or ``{"value","label","icon"}`` dicts.
      multiple     checkbox multi-select (True) vs single-select (False).
      onchange     Alpine expression run after a change, e.g. "reload()".
      placeholder  trigger label shown when nothing is selected.
      value_field / label_field / icon_field
                   attribute names read off model instances (ignored for dicts).
                   ``icon_field`` is read as a platform key and rendered as a
                   per-option badge.
      icon         leading glyph for the trigger itself — one of
                   status / channel / tag / clock (see components/_filter_icon.html).
                   Omit for no icon. An unknown name raises rather than
                   rendering a padded gap where the glyph should be.

    Every parameter is keyword-only. With nine of them, a positional call would
    be unreadable, and this guarantees call sites document themselves.
    """
    norm: list[dict[str, Any]] = []
    for o in options:
        opt_icon: Any
        if isinstance(o, dict):
            # Checked before tuple/list: a dict is neither, but it must not
            # fall through to the getattr branch below.
            value, label, opt_icon = o.get("value"), o.get("label"), o.get("icon")
        elif isinstance(o, tuple | list) and len(o) >= 2:
            # (value, label) pairs, e.g. Django `choices`. >= 2 rather than
            # == 2 so a longer tuple degrades instead of raising.
            value, label, opt_icon = o[0], o[1], None
        elif isinstance(o, str):
            value = label = o
            opt_icon = None
        else:
            # The getattr asymmetry is deliberate. `label_field` uses the
            # two-argument form, so a typo raises AttributeError loudly at the
            # call site that made it; `value_field` and `icon_field` use the
            # three-argument form, because a heterogeneous option list legitimately
            # contains objects without an icon.
            value = getattr(o, value_field, None)
            label = getattr(o, label_field) if label_field else str(o)
            opt_icon = getattr(o, icon_field, None) if icon_field else None
        # `value` is always coerced to str, so a UUID pk compares equal to the
        # string Alpine holds. None collapses to "" — the same sentinel the
        # template's "all" reset writes, which is why the client-side
        # comparison is String(model) === '<value>'.
        norm.append({"value": str(value) if value is not None else "", "label": label, "icon": opt_icon})

    if icon and icon not in FILTER_ICONS:
        raise ValueError(f"ui_select: unknown icon {icon!r}; expected one of {sorted(FILTER_ICONS)}")

    return {
        "model": model,
        "options": norm,
        # value+label only, for the Alpine trigger-label lookup in single mode.
        # str() here and not in `norm`: a lazy gettext proxy serializes fine
        # through default=str but would be pointless to stringify for rendering.
        "options_js": [{"value": o["value"], "label": str(o["label"])} for o in norm],
        "multiple": bool(multiple),
        "onchange": onchange,
        "placeholder": placeholder,
        "icon": icon,
    }


def _filter_groups(groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise a page's filter groups for the popover and its chips.

    Each group is ``{"key", "label", "options"}``; ``key`` is the query
    parameter and the property of the Alpine model that holds the group's
    selection. Options take the same shapes ``ui_select`` does — ``(value,
    label)`` pairs, plain strings, or dicts — and a dict may also carry
    ``count`` (shown beside the label) and ``icon`` (a platform key). Values
    are coerced to ``str`` so a UUID compares equal to what Alpine holds.
    """
    normalised = []
    for group in groups:
        options = []
        for option in group["options"]:
            if isinstance(option, dict):
                value, label = option.get("value"), option.get("label")
                count, icon = option.get("count"), option.get("icon")
            elif isinstance(option, tuple | list) and len(option) >= 2:
                value, label, count, icon = option[0], option[1], None, None
            else:
                value = label = option
                count = icon = None
            options.append({"value": "" if value is None else str(value), "label": label, "count": count, "icon": icon})
        normalised.append({"key": group["key"], "label": group["label"], "options": options})
    return normalised


@register.inclusion_tag("components/filter_popover.html")
def filter_popover(
    *,
    groups: Iterable[dict[str, Any]],
    model: str = "filters",
    onchange: str = "",
    target: str = "",
    count: int | None = None,
    noun: str = "result",
    noun_plural: str = "",
) -> dict[str, Any]:
    """The list toolbar's Filter button and its popover (HANDOFF §2.2).

    Render it **inside** the toolbar's htmx form: it writes one hidden input
    per selected value, so the form serialises ``?status=a&status=b`` and the
    view reads it with :func:`apps.common.filters.multi`.

    Params:
      groups    the sections, see :func:`_filter_groups`.
      model     Alpine expression for an object in the enclosing ``x-data``
                holding one array per group key, e.g. ``filters`` with
                ``{status: [], folder: []}``. Every key must be present.
      onchange  Alpine expression run after each toggle, e.g. ``"reload()"``.
                Filters apply as they are picked; the footer only closes.
      target    CSS selector of the region the form swaps. The footer's count
                is read from the ``data-result-count`` on that region's first
                child after every swap.
      count     the result count at render time, for the footer before any
                swap has happened.
      noun      what is being counted, singular: "flow", "contact".
      noun_plural  its plural, when adding an "s" is wrong ("people").

    Keyword-only, like ``ui_select``, so call sites document themselves.
    """
    return {
        "groups": _filter_groups(groups),
        "model": model,
        "onchange": onchange,
        "target": target,
        "count": count,
        "noun": noun,
        "noun_plural": noun_plural or f"{noun}s",
    }


@register.inclusion_tag("components/filter_chips.html")
def filter_chips(*, groups: Iterable[dict[str, Any]], model: str = "filters", onchange: str = "") -> dict[str, Any]:
    """The removable chips under a list toolbar, one per active filter value.

    Same ``groups`` and ``model`` as the :func:`filter_popover` beside it. The
    labels travel as a JSON lookup so a chip says "Live" rather than "active"
    and a channel's name rather than its id.
    """
    normalised = _filter_groups(groups)
    return {
        "groups": normalised,
        "model": model,
        "onchange": onchange,
        "labels": {g["key"]: {o["value"]: str(o["label"]) for o in g["options"]} for g in normalised},
        "group_labels": {g["key"]: str(g["label"]) for g in normalised},
    }


# The widths components/modal.html knows. Anything else raises at render rather
# than falling back to the default, for the same reason ui_select's icon does: a
# typo should be loud at the call site, not a quietly narrow form.
MODAL_SIZES = frozenset({"", "lg"})
MODAL_ARGS = frozenset({"title", "close_on", "open", "size"})


class ModalNode(template.Node):
    def __init__(self, nodelist: NodeList, modal_id: FilterExpression, kwargs: dict[str, FilterExpression]) -> None:
        self.nodelist = nodelist
        self.modal_id = modal_id
        self.kwargs = kwargs

    def render(self, context: Context) -> str:
        values = {name: expression.resolve(context) for name, expression in self.kwargs.items()}
        size = values.get("size") or ""
        if size not in MODAL_SIZES:
            raise ValueError(f"modal: unknown size {size!r}; expected one of {sorted(MODAL_SIZES)}")
        assert context.template is not None  # noqa: S101 - always set while a template renders
        dialog = context.template.engine.get_template("components/modal.html")
        with context.push(
            id=self.modal_id.resolve(context),
            title=values.get("title", ""),
            close_on=values.get("close_on") or "",
            open=bool(values.get("open")),
            size=size,
            body=self.nodelist.render(context),
        ):
            return dialog.render(context)


@register.tag
def modal(parser: Parser, token: Token) -> ModalNode:
    """A dialog around whatever the block contains — usually one create form.

        {% modal "new-tag" title="New tag" close_on="tagsChanged" %}
          <form hx-post="...">...</form>
        {% endmodal %}

    Inside the form: a ``.bb-modal-body`` of fields, the first with
    ``autofocus``, then a ``.bb-modal-actions`` row — Cancel calling
    ``close()``, then the submit. Open it from anywhere with
    ``@click="$modal('new-tag')"``. templates/contacts/tag_list.html is the
    plainest example.

    Args:
      id        positional; the dialog's DOM id, and the argument to ``$modal``.
      title     the heading, which also labels the dialog.
      close_on  an HX-Trigger event name. The dialog closes after a response
                that sets it, and stays open on any other — including a 204
                refusal, so the typed value survives the error toast. Omit it
                for a form that navigates away on success.
      open      truthy opens the dialog on load: for a page that answers a
                refused plain POST by re-rendering itself with the error.
      size      "" (default) or "lg", for forms longer than a field or two.

    A block tag rather than an inclusion tag because the caller supplies the
    body. Everything else — the heading, the close button, the backdrop
    handling — is the component's, so no page can drift from the others.
    """
    bits = token.split_contents()
    if len(bits) < 2:
        raise template.TemplateSyntaxError("modal takes the dialog id as its first argument")
    modal_id = parser.compile_filter(bits[1])
    remaining = bits[2:]
    kwargs = token_kwargs(remaining, parser)
    if remaining:
        raise template.TemplateSyntaxError(f"modal: unexpected arguments {remaining}")
    unknown = set(kwargs) - MODAL_ARGS
    if unknown:
        raise template.TemplateSyntaxError(f"modal: unknown arguments {sorted(unknown)}")
    nodelist = parser.parse(("endmodal",))
    parser.delete_first_token()
    return ModalNode(nodelist, modal_id, kwargs)
