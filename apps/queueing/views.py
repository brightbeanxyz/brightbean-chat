"""``/internal/tick`` — an HTTP drain for hosts with no long-lived worker.

SPEC §15: "``/internal/tick?token=`` HTTP wrapper (constant-time token compare,
env ``TICK_TOKEN``) for external pingers. Behavior identical to one worker
cycle; safe to run concurrently with a worker."

The concurrency safety is not a claim made here — it is the ``FOR UPDATE SKIP
LOCKED`` claim in ``apps.queueing.worker``. This view calls the same
``drain()`` every worker calls; nothing about being reached over HTTP changes
what a claim does.

**Why a bare token rather than the shared signer.** ``apps/common/signing.py``
lists this route among its consumers, and this is a deliberate, documented
divergence. A signed token buys expiry and purpose-scoping; the caller here is a
third-party pinger (cron-job.org, Uptime Robot, a Kubernetes CronJob) that
stores one URL in its configuration and calls it forever, so the token would
have to be minted with ``max_age=None`` — at which point it is a bare token with
extra steps, and rotating it means re-minting and re-pasting rather than
changing one environment variable. The properties that actually matter are kept:
constant-time comparison, and a bare 404 for every failure including a missing
configuration (SECURITY-BASELINE §4).
"""

import logging
import time

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt

from apps.queueing.housekeeping import ensure_housekeeping_scheduled
from apps.queueing.worker import DEFAULT_BATCH_SIZE, drain

logger = logging.getLogger(__name__)

#: Deliberately shorter than ``manage.py tick``'s 55 s.
#:
#: The Procfile and the Dockerfile run gunicorn at its default 30 s worker
#: timeout, so a request that worked for 55 s would be killed mid-batch — the
#: rows would sit in ``running`` until zombie recovery ten minutes later, and
#: the operator would see a 502 from a tick that was working perfectly well.
#: The budget is checked between batches, so the real ceiling is this plus one
#: batch.
MAX_SECONDS = 20


@csrf_exempt  # Token-authenticated, not session-authenticated: an external pinger has no CSRF cookie.
def internal_tick(request: HttpRequest) -> HttpResponse:
    """Drain the queue once. 404 unless the caller presents ``TICK_TOKEN``.

    The method check is inside the body, *after* the token check, rather than in
    a ``@require_http_methods`` decorator. A decorator runs first, so an
    unauthenticated ``HEAD`` or ``DELETE`` would answer ``405`` with an
    ``Allow`` header while every unmounted path answers ``404`` — which
    confirms this route exists, and with it that the deployment runs this
    queue, to a caller holding no token at all. That is the same reasoning
    CONTRIBUTING.md gives for stacking ``@require_POST`` innermost on the
    tenant views: the check that reveals nothing has to run before the one that
    reveals something.
    """
    expected = (getattr(settings, "TICK_TOKEN", "") or "").strip()
    if not expected:
        # Unset means the route does not exist for this deployment. 404 rather
        # than 403 or 503: an operator who has not configured it should not be
        # able to learn from the response that it is there to be configured.
        raise Http404

    provided = request.GET.get("token", "")
    if not constant_time_compare(provided, expected):
        # Bare 404, no detail, no distinguishable status (SECURITY-BASELINE §4).
        # Logged without the token — the scrubbing filter would redact it
        # anyway, but there is no reason to hand it to the filter at all.
        logger.warning("Rejected /internal/tick: bad or missing token")
        raise Http404

    # Only now, with the caller proven, is it safe to say something specific.
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])

    started = time.monotonic()
    ensure_housekeeping_scheduled()
    result = drain(batch_size=DEFAULT_BATCH_SIZE, max_seconds=MAX_SECONDS)
    duration_ms = int((time.monotonic() - started) * 1000)

    logger.info(
        "Tick drained claimed=%s done=%s failed=%s retried=%s duration_ms=%s",
        result.claimed,
        result.done,
        result.failed,
        result.retried,
        duration_ms,
    )
    return JsonResponse(
        {
            "claimed": result.claimed,
            "done": result.done,
            "failed": result.failed,
            "retried": result.retried,
            "duration_ms": duration_ms,
        }
    )
