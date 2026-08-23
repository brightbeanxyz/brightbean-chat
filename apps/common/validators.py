"""Shared validation utilities.

Ported from BrightBean Studio's ``apps/common/validators.py``. Studio's
``is_safe_url`` / ``resolve_public_ip`` SSRF helpers are deliberately **not**
here: SECURITY-BASELINE §6 puts the one shared SSRF guard in issue #15 and
forbids any server-side fetch of a user-supplied URL until it lands. Shipping a
weaker lookalike now would create exactly the tempting, TOCTOU-prone call site
the baseline is written to prevent. Tag and XML helpers follow their consumers
(#3 contacts, #4 webhooks).

:func:`is_renderable_url` below is the other half of that sentence and is not an
exception to it — see its docstring.
"""

import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

#: The only two schemes a platform-supplied URL may reach the DOM under.
_RENDERABLE_SCHEMES = frozenset({"http", "https"})


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


def is_renderable_url(value: object) -> bool:
    """May this string become an ``href`` or a ``src``? (SECURITY-BASELINE §2)

    **This is not an SSRF guard and must never be used as one.** It answers a
    rendering question — "is putting this in the DOM safe for the reader" — not
    a fetching question. The one mandatory path for a *server-side* request to a
    user-supplied URL is issue #15's shared guard, and the module docstring above
    says why a lookalike must not appear here. Nothing in this function resolves
    a hostname, and a URL it accepts may still point at localhost.

    What it rejects is the set of schemes that execute or impersonate when a
    browser follows them: ``javascript:`` (script in a link), ``data:`` (an
    attacker-authored document on our origin's coat-tails), ``vbscript:``,
    ``file:``, and everything else unknown. Media URLs on a message body arrive
    from strangers and are stored verbatim —
    ``apps.messaging.ingest`` caps their length and strips NULs but deliberately
    does not check the scheme — so ``javascript:alert(1)`` is a value that can
    legitimately be sitting in ``Message.body`` right now.

    Also rejected: a scheme-relative ``//host/path``, which inherits whatever
    scheme the page was served over and reads as a path to anyone skimming the
    stored value, and anything ``urlsplit`` cannot parse.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    # Control characters are checked on the RAW value, before stripping, and
    # tab and newline are the reason. Browsers (and urlsplit, since the fix for
    # CVE-2022-0391) silently drop them from a URL before parsing it, so
    # "java\tscript:alert(1)" parses as a javascript: URL while reading as
    # something else in the database. A stored URL whose parsed form disagrees
    # with its written form has nothing to recommend it either way.
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return False
    try:
        parts = urlsplit(value.strip())
        # Read, not just parsed: urlsplit defers validating the port until the
        # attribute is touched, so without this a non-numeric port sails past
        # the except clause below.
        _ = parts.port
    except ValueError:
        # A malformed IPv6 literal or a non-numeric port.
        return False
    if parts.scheme.lower() not in _RENDERABLE_SCHEMES:
        return False
    return bool(parts.netloc)
