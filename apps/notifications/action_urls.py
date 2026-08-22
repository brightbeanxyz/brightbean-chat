"""Where a notification is allowed to send someone.

``notify()`` takes an arbitrary ``context`` dict, and ``context["action_url"]``
becomes the link on the bell row and the button in the email. That value comes
from whichever layer called ``notify()`` — and those callers are inbound webhook
handlers, flow nodes and queue workers, all of which handle data this deployment
did not author. A contact's display name, a webhook field or a flow variable
reaching this key is an ordinary-looking mistake for a Layer-3 author to make.

So the policy is enforced **once, at write time**, in
:func:`~apps.notifications.engine.notify`: whatever a caller passes is reduced
to a root-relative path on this deployment, or dropped. Every render site then
inherits it — the bell, the history page, the email button, and whatever Layer 6
adds — without each having to remember.

**Only same-origin destinations survive.** All eight registered event types
point inside the product (a flow, a channel, a conversation), so nothing is lost
by refusing off-site links, and refusing them denies an attacker who reaches
this key the most valuable thing it offers: a phishing link rendered inside the
product's own chrome, under the product's own domain, in a notification the
recipient has every reason to trust. A later event that genuinely needs an
outbound link should be a reviewed change here rather than a default.
"""

import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

__all__ = ["absolute", "safe_path"]

# Browsers strip C0 control characters (notably TAB, LF and CR) from a URL
# *before* parsing its origin, so "/\tevil.com" and "/\t/evil.com" are read as
# "//evil.com" and navigate off-site. Any check that runs on the raw string
# without removing these first can be walked straight past.
_CONTROL_CHARS = frozenset(chr(code) for code in range(0x20)) | {"\x7f"}


def app_url() -> str:
    """The deployment's base URL, without a trailing slash."""
    return str(getattr(settings, "APP_URL", "http://localhost:8000")).rstrip("/")


def safe_path(raw: Any) -> str | None:
    """Reduce a caller's ``action_url`` to a root-relative path, or ``None``.

    ``None`` means "nowhere safe to send them" and the caller should fall back
    to a route it controls. Everything that survives starts with a single ``/``
    and cannot leave this origin.
    """
    if not isinstance(raw, str):
        return None

    candidate = raw.strip()
    if not candidate:
        return None

    if _CONTROL_CHARS.intersection(candidate):
        logger.warning("Dropped a notification action_url containing control characters.")
        return None

    # An absolute URL is accepted only when it is this deployment's own origin,
    # and is then reduced to a path so the stored value has exactly one shape.
    # A caller that builds "{APP_URL}/w/.../flows/1" is doing something
    # reasonable and should not silently lose its link.
    base = app_url()
    if base:
        if candidate == base:
            return "/"
        if candidate.startswith(f"{base}/"):
            # The trailing slash matters: without it, "https://chat.example"
            # would also prefix-match "https://chat.example.evil.com/x".
            candidate = candidate[len(base) :]

    if not candidate.startswith("/"):
        # Anything still carrying a scheme lands here — javascript:, data:,
        # mailto:, and every off-origin http(s) URL.
        logger.warning("Dropped a notification action_url that was not a path on this deployment.")
        return None

    # "//host" is protocol-relative and leaves the origin; "/\host" is the same
    # thing after the backslash normalisation browsers apply to the authority.
    if candidate.startswith(("//", "/\\")):
        logger.warning("Dropped a protocol-relative notification action_url.")
        return None

    return candidate


def absolute(path: str | None) -> str:
    """An absolute URL for an email, from a path :func:`safe_path` returned."""
    from django.urls import reverse

    if not path:
        path = reverse("notifications:list")
    return f"{app_url()}{path}"
