"""Small HTMX response helpers shared across apps.

Ported from BrightBean Studio's ``apps/common/htmx.py``, with type annotations
added (this project type-checks ``apps/`` with mypy + django-stubs).

The client half lives in ``templates/base.html``: a single toast host listens
for the ``showToast`` event on every page, so ``toast_response(...)`` works
from any view with no per-page include. Studio required one — its toast host
was a partial that each template had to pull in by hand, outside any
htmx-swapped region.
"""

import json
from typing import Any, Literal

from django.http import HttpResponse

# The tones the toast host in base.html knows how to render. Anything else
# falls back to "info" client-side rather than rendering an unstyled toast.
Tone = Literal["success", "info", "warn", "error"]


def trigger_response(triggers: dict[str, Any], status: int = 204) -> HttpResponse:
    """Return an empty response that fires the given ``HX-Trigger`` events.

    ``triggers`` maps event name to detail, e.g.
    ``{"showToast": {...}, "contactSaved": True}``. htmx dispatches each one on
    the element that made the request, so listeners bound with
    ``hx-trigger="contactSaved from:body"`` or ``@contactSaved.window`` pick
    them up as they bubble.

    The default 204 means htmx performs no swap: the response is the events.
    """
    return HttpResponse(status=status, headers={"HX-Trigger": json.dumps(triggers)})


def toast_response(
    *,
    tone: Tone,
    title: str,
    body: str = "",
    events: dict[str, Any] | None = None,
) -> HttpResponse:
    """204 that shows a client toast, plus any extra ``HX-Trigger`` events.

    The usual pattern is a toast alongside a refresh event, so the surface that
    triggered the action re-fetches its list:

        return toast_response(
            tone="success", title="Contact deleted", events={"contactsChanged": True}
        )

    ``title`` and ``body`` are rendered with ``textContent`` on the client, so
    they may safely carry user- or platform-supplied text
    (``docs/SECURITY-BASELINE.md`` §2).
    """
    triggers: dict[str, Any] = {"showToast": {"tone": tone, "title": title, "body": body}}
    if events:
        triggers.update(events)
    return trigger_response(triggers)
