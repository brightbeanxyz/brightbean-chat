"""Bearer authentication for ``/api/v1/`` (SPEC §17, §4.2).

This is the first surface in BrightBean Chat that authenticates without a
session, so everything the rest of the project leans on is absent:
``RBACMiddleware`` has no ``/w/<workspace_id>/`` kwarg to resolve, there is no
logged-in user, and there is no CSRF token because there is no cookie.

What replaces it is one narrow shim. SPEC §4.2 and
``apps.members.models.WorkspaceMembership.effective_permissions`` both say the
same thing, and ``apps.members.decorators.require_permission`` is written to
match: the *only* protocol a permission check consumes is a mapping of
permission key to bool. So :class:`VirtualMembership` exposes exactly that and
nothing else, ``@require_permission`` works on a Ninja operation unchanged, and
no synthetic ``WorkspaceMembership`` row is ever written.

Order of checks in :meth:`ApiKeyAuth.authenticate`, and why:

1. **Body size**, free and first (SECURITY-BASELINE §7) — before any database
   work at all.
2. **HTTPS**, free. A bearer token over plaintext is a bearer token in every
   proxy log between here and the caller.
3. **Failed-auth throttle**, one indexed read — *before* the digest, so a script
   walking the key space does not get to pay only the hash cost per attempt once
   it is over the line.
4. **Key resolution**: indexed prefix lookup, then a constant-time compare of
   digests, then the revocation and workspace checks.
5. **Rate limit**, last, so a request that was never going to authenticate does
   not consume an authenticated key's budget.

Every failure in 2–4 returns ``None``, which Ninja turns into the one uniform
401 in ``apps.api.errors``. No branch tells the caller which check refused it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import HttpRequest
from ninja.security import HttpBearer

from apps.api import keys as key_tokens
from apps.api.errors import ApiError, PayloadTooLargeError, RateLimitedError
from apps.api.ratelimit import (
    RATE_WINDOW_SECONDS,
    auth_failures_exhausted,
    over_key_limit,
    record_auth_failure,
)
from apps.api.requests import ApiRequest
from apps.common.jsonlimits import max_json_depth
from apps.members.roles import PERMISSION_KEYS

LOG = logging.getLogger(__name__)

__all__ = [
    "SCOPE_PERMISSIONS",
    "ApiKeyAuth",
    "VirtualMembership",
    "permissions_for_scopes",
]

#: What SPEC §17's coarse scopes mean in terms of SPEC §4.2's permission keys.
#:
#: Two deliberate absences. ``manage_api_keys`` is not grantable at any scope —
#: a key that can mint keys is a key that never really gets revoked. Neither are
#: ``manage_channels``, ``manage_members``, ``manage_workspace_settings``,
#: ``manage_media``, ``edit_flows`` or ``send_broadcasts``: SPEC §17's endpoints
#: read and write contacts, start flows and send messages, and a scope that
#: grants more than its endpoints need is a scope waiting for the endpoint that
#: uses it.
#:
#: ``write`` is a superset of ``read`` rather than a sibling, so a key holding
#: both is the same as a key holding ``write``.
SCOPE_PERMISSIONS: dict[str, frozenset[str]] = {
    "read": frozenset({"use_inbox", "view_analytics"}),
    "write": frozenset(
        {
            "use_inbox",
            "view_analytics",
            "edit_contact_fields",
            "manage_crm",
            "reply_in_inbox",
        }
    ),
}


def permissions_for_scopes(scopes: Any) -> dict[str, bool]:
    """Expand a key's scopes into the mapping ``require_permission`` reads.

    Returns an answer for **every** key in ``PERMISSION_KEYS``, not only the
    granted ones. ``effective_permissions.get(key, False)`` would cope either
    way, but a partial mapping is a mapping whose meaning depends on the reader,
    and ``apps/members/tests/test_roles.py`` holds the real memberships to the
    same completeness standard.

    An unrecognised scope string grants nothing rather than everything: a row
    holding a scope this code does not understand means the database is ahead of
    the deployment, and guessing in the permissive direction is how a stale row
    becomes a privilege escalation.
    """
    granted: set[str] = set()
    for scope in scopes or ():
        granted |= SCOPE_PERMISSIONS.get(str(scope), frozenset())
    return {key: key in granted for key in PERMISSION_KEYS}


@dataclass(eq=False)
class VirtualMembership:
    """Duck-typed stand-in for ``apps.members.models.WorkspaceMembership``.

    Deliberately narrow. ``require_permission`` reads ``effective_permissions``
    and nothing else, so this exposes nothing else — which means a membership
    model that grows custom roles, signals or a ``save()`` cannot quietly change
    what a bearer token is allowed to do. No row is created and no signal fires.
    """

    effective_permissions: dict[str, bool] = field(default_factory=dict)


class ApiKeyAuth(HttpBearer):
    """Resolve ``Authorization: Bearer bb_…`` to a workspace-scoped API key.

    Ninja's contract: return a truthy value to authenticate (it becomes
    ``request.auth``), or ``None`` for a 401. We return the ``ApiKey`` row and
    also attach the request state every route and decorator downstream expects —
    ``request.workspace``, ``request.workspace_membership`` and
    ``request.api_key``.
    """

    openapi_scheme = "bearer"

    def authenticate(self, request: HttpRequest, token: str) -> Any:
        from apps.api.models import ApiKey, touch_last_used

        self._enforce_body_limits(request)

        if not self._transport_is_acceptable(request):
            return None

        if auth_failures_exhausted(request):
            # DEBUG rather than WARNING: this fires on every request from an
            # already-throttled address, so a louder level would let the caller
            # choose our log volume.
            LOG.debug("Public API bearer refused: client address is over its failed-auth budget.")
            return None

        parsed = key_tokens.parse(token)
        if parsed is None:
            record_auth_failure(request)
            return None
        secret, lookup_prefix = parsed

        api_key = self._resolve(ApiKey, secret, lookup_prefix)
        if api_key is None:
            record_auth_failure(request)
            return None

        if over_key_limit(api_key):
            raise RateLimitedError(RATE_WINDOW_SECONDS)

        bound = cast(ApiRequest, request)
        bound.api_key = api_key
        bound.workspace = api_key.workspace
        bound.workspace_membership = VirtualMembership(permissions_for_scopes(api_key.scopes))
        touch_last_used(api_key)
        return api_key

    @staticmethod
    def _enforce_body_limits(request: HttpRequest) -> None:
        """Refuse an over-sized or over-nested body before anything parses it.

        This runs in the auth callback because Ninja's ``_run_checks`` is the
        last hook before ``_get_values`` hands the body to Pydantic, and
        SECURITY-BASELINE §7 wants the cap applied before the parser and before
        any database write.

        The depth check has to look at the *bytes*: Python's JSON parser
        recurses, so a nesting bomb is a stack overflow rather than a catchable
        exception, and checking the parsed result is checking something that may
        never exist. ``apps.common.jsonlimits.max_json_depth`` scans without
        parsing.
        """
        limit = settings.API_MAX_BODY_BYTES
        declared = request.META.get("CONTENT_LENGTH") or ""
        try:
            if int(declared) > limit:
                raise PayloadTooLargeError(f"Request bodies are limited to {limit} bytes.")
        except (TypeError, ValueError):
            # No Content-Length (a chunked request); fall through to the read,
            # which Django bounds with DATA_UPLOAD_MAX_MEMORY_SIZE.
            pass

        if request.method in {"GET", "HEAD", "DELETE"}:
            return

        try:
            raw = request.body
        except RequestDataTooBig as exc:
            raise PayloadTooLargeError(f"Request bodies are limited to {limit} bytes.") from exc
        if len(raw) > limit:
            raise PayloadTooLargeError(f"Request bodies are limited to {limit} bytes.")
        if raw and max_json_depth(raw) > settings.API_MAX_JSON_DEPTH:
            raise ApiError(
                "The request body is nested too deeply.",
                code="body_too_deep",
                status=422,
            )

    @staticmethod
    def _transport_is_acceptable(request: HttpRequest) -> bool:
        """Refuse a bearer sent in the clear outside development.

        ``DEBUG`` is the escape hatch for ``http://127.0.0.1:8000`` while
        developing. In production a plaintext bearer has already been logged by
        every hop that saw it, so accepting it would only confirm that the
        credential works.
        """
        if request.is_secure() or settings.DEBUG:
            return True
        LOG.warning("Public API bearer refused: the request arrived over plain HTTP.")
        return False

    @staticmethod
    def _resolve(model: Any, secret: str, lookup_prefix: str) -> Any:
        """Find the active key a presented secret belongs to, or ``None``.

        ``.unscoped()`` because this *is* how the workspace gets resolved: there
        is no session and no URL kwarg, and the key names its own tenant. What
        bounds the query is the credential itself — without a matching digest
        there is no row, and the digest is keyed on ``SECRET_KEY``, so a database
        dump does not let anyone recompute one.

        The prefix is not unique (8 hex characters; see ``apps.api.keys``), so
        this walks the candidates and compares each in constant time rather than
        assuming one row.
        """
        candidates = (
            # Cross-tenant by necessity: an API key identifies its own workspace.
            model.objects.unscoped()
            .select_related("workspace")
            .filter(lookup_prefix=lookup_prefix, revoked_at__isnull=True)
        )
        for candidate in candidates:
            if not candidate.matches(secret):
                continue
            if candidate.workspace is None or candidate.workspace.is_archived:
                return None
            return candidate
        return None
