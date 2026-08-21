"""Shared template tags and filters.

Ported from BrightBean Studio's ``apps/common/templatetags/common_extras.py``.
Behaviour is preserved exactly — several details here look like accidents and
are not; each is commented where it would otherwise be "cleaned up" into a bug.
"""

import json
from collections.abc import Iterable
from typing import Any

from django import template
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

register = template.Library()


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
                   Omit for no icon.

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
