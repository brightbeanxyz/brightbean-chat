"""Shared validation utilities.

Ported from BrightBean Studio's ``apps/common/validators.py``. Studio's
``is_safe_url`` / ``resolve_public_ip`` SSRF helpers are deliberately **not**
here: SECURITY-BASELINE §6 puts the one shared SSRF guard in issue #15 and
forbids any server-side fetch of a user-supplied URL until it lands. Shipping a
weaker lookalike now would create exactly the tempting, TOCTOU-prone call site
the baseline is written to prevent. Tag and XML helpers follow their consumers
(#3 contacts, #4 webhooks).
"""

import re

from django.core.exceptions import ValidationError

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def validate_hex_color(value: str) -> None:
    """Reject any string that isn't a 6-digit hex color (#RRGGBB) or empty.

    Used as a model-field validator on every user-editable color column.
    Empty strings pass so that "no override" still works.
    """
    if value in ("", None):
        return
    if not isinstance(value, str) or not _HEX_COLOR_RE.match(value):
        raise ValidationError("Color must be a 6-digit hex value like #3B82F6.")


def is_valid_hex_color(value: str) -> bool:
    """Boolean form of ``validate_hex_color`` for view-layer rejection paths."""
    if value in ("", None):
        return True
    return isinstance(value, str) and bool(_HEX_COLOR_RE.match(value))
