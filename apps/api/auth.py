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

Order of checks in :meth:`ApiKeyAuth.authenticate`, and why. The ordering
principle is that **an unauthenticated caller may not make this process do
expensive work**, so the cost of each step rises as confidence in the caller
does:

1. **Declared body size**, free — it reads ``Content-Length`` and refuses on the
   header alone, buffering nothing (SECURITY-BASELINE §7).
2. **HTTPS**, free. A bearer token over plaintext is a bearer token in every
   proxy log between here and the caller.
3. **Failed-auth throttle**, one indexed read — *before* the digest, so a script
   walking the key space does not get to pay only the hash cost per attempt once
   it is over the line.
4. **Key resolution**: indexed prefix lookup, then a constant-time compare of
   digests, then the revocation and workspace checks.
5. **Actual body limits**, once the key is known good. Buffering a body and
   scanning it for nesting depth is the one genuinely expensive step here, and
   putting it behind authentication is what stops an anonymous caller from
   spending it for free.
6. **Rate limit**, last, so a request that was never going to authenticate does
   not consume an authenticated key's budget.

Every failure in 2–4 returns ``None``, which Ninja turns into the one uniform
401 in ``apps.api.errors``. No branch tells the caller which check refused it —
and only step 4's *unrecognised* case is held against the caller's address, so
one revoked key cannot throttle its owner's working ones.
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
from apps.api.models import ApiScope
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
    "Resolution",
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
#: ``erase`` is the third scope, and it exists rather than being folded into
#: ``write`` because of :func:`apps.api.services._validated_scopes`: a scope is
#: grantable only by an issuer who holds every permission in it, so a scope
#: carrying the admin-only ``erase_contacts`` key can only ever be granted by an
#: admin. That is the property SPEC §19's irreversible erasure needs, and it
#: comes free from machinery that already exists.
#:
#: Issue #29 specifies ``DELETE /api/v1/contacts/<id>`` as "scope write". This
#: is a deliberate deviation, argued in that PR: putting ``erase_contacts`` in
#: ``write`` would have granted irreversible erasure to every key **already
#: issued**, at upgrade, with nothing on the keys page changing and no
#: re-consent from the operator who minted it. SPEC §17 does not enumerate the
#: scopes and does not list this endpoint, so a third coarse scope contradicts
#: no contract — and "coarse" is still true at three.
#:
#: ``write`` is a superset of ``read`` rather than a sibling, so a key holding
#: both is the same as a key holding ``write``.
#:
#: Keyed by :class:`~apps.api.models.ApiScope` members rather than by bare
#: strings, so the enum that labels a scope in the issuance form and the table
#: that decides what it grants are one thing. ``apps/members/roles.py``'s
#: docstring makes the case: "A permission system whose authority ordering can
#: disagree with itself is a permission system that will, eventually, disagree
#: with itself." ``apps/api/tests/test_auth.py`` pins the two key sets equal, so
#: a scope added to one and not the other fails rather than silently becoming
#: un-grantable.
SCOPE_PERMISSIONS: dict[str, frozenset[str]] = {
    ApiScope.READ.value: frozenset({"use_inbox", "view_analytics"}),
    ApiScope.WRITE.value: frozenset(
        {
            "use_inbox",
            "view_analytics",
            "edit_contact_fields",
            "manage_crm",
            "reply_in_inbox",
        }
    ),
    # Additive, not a superset of ``write``: a key may hold both, and one that
    # holds only this can erase and do nothing else — which is what a
    # deletion-sync integration actually wants.
    ApiScope.ERASE.value: frozenset({"erase_contacts"}),
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


@dataclass(frozen=True)
class Resolution:
    """What looking a presented secret up produced.

    ``recognised`` is the half that matters beyond this module: a secret that
    matched a row we issued is not a guess, whatever state that row is in.
    """

    api_key: Any
    recognised: bool


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

        self._refuse_declared_oversize(request)

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

        resolution = self._resolve(ApiKey, secret, lookup_prefix)
        if resolution.api_key is None:
            if not resolution.recognised:
                # Only an unrecognised secret counts as guessing. A key we
                # issued and later revoked proves the caller is *not* walking
                # the key space, and charging it would let one dead credential
                # lock its owner's working keys out — see _resolve.
                record_auth_failure(request)
            return None
        api_key = resolution.api_key

        # Only now, with a real credential in hand, is it worth buffering and
        # scanning the body.
        self._enforce_body_limits(request)

        if over_key_limit(api_key):
            raise RateLimitedError(RATE_WINDOW_SECONDS)

        bound = cast(ApiRequest, request)
        bound.api_key = api_key
        bound.workspace = api_key.workspace
        bound.workspace_membership = VirtualMembership(permissions_for_scopes(api_key.scopes))
        touch_last_used(api_key)
        return api_key

    @staticmethod
    def _refuse_declared_oversize(request: HttpRequest) -> None:
        """Reject an over-sized body from its ``Content-Length`` alone.

        Free — it reads a header, not a body — so it runs before everything,
        including the throttle. A caller that announces two megabytes is refused
        without anything being buffered.
        """
        limit = settings.API_MAX_BODY_BYTES
        declared = request.META.get("CONTENT_LENGTH") or ""
        try:
            declared_bytes = int(declared)
        except (TypeError, ValueError):
            # No Content-Length (a chunked request). The real read below is
            # bounded by Django's DATA_UPLOAD_MAX_MEMORY_SIZE either way.
            return
        if declared_bytes > limit:
            raise PayloadTooLargeError(f"Request bodies are limited to {limit} bytes.")

    @staticmethod
    def _enforce_body_limits(request: HttpRequest) -> None:
        """Refuse an over-sized or over-nested body before anything parses it.

        Still inside the auth callback, because Ninja's ``_run_checks`` is the
        last hook before ``_get_values`` hands the body to Pydantic and
        SECURITY-BASELINE §7 wants the cap applied before the parser and before
        any database write. But **after** the key has been verified: buffering
        and scanning a body is the one genuinely expensive thing on this path,
        and an unauthenticated caller must not be able to make us do it. The
        free half of the check (:meth:`_refuse_declared_oversize`) still runs
        first, so an honest over-sized request never gets this far.

        The depth check has to look at the *bytes*: Python's JSON parser
        recurses, so a nesting bomb is a stack overflow rather than a catchable
        exception, and checking the parsed result is checking something that may
        never exist. ``apps.common.jsonlimits.max_json_depth`` scans without
        parsing.
        """
        if request.method in {"GET", "HEAD", "DELETE"}:
            return

        limit = settings.API_MAX_BODY_BYTES
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
    def _resolve(model: Any, secret: str, lookup_prefix: str) -> Resolution:
        """Find the usable key a presented secret belongs to.

        ``.unscoped()`` because this *is* how the workspace gets resolved: there
        is no session and no URL kwarg, and the key names its own tenant. What
        bounds the query is the credential itself — without a matching digest
        there is no row, and the digest is keyed on ``SECRET_KEY``, so a database
        dump does not let anyone recompute one.

        The prefix is not unique (8 hex characters; see ``apps.api.keys``), so
        this walks the candidates and compares each in constant time rather than
        assuming one row.

        **Revoked rows are fetched, not filtered out.** The caller has to be able
        to tell "this secret matches nothing we ever issued" from "this is one of
        ours and it is dead", because only the first is someone walking the key
        space. Filtering in SQL collapses the two, and the consequence is real:
        an integration retrying a revoked key at the documented 10 req/s crosses
        the per-address failed-auth limit in two seconds, and every *working* key
        from that address then gets the same opaque 401 for the rest of the
        window. Either way the answer to this request is a 401 — the distinction
        only decides whether it is also held against the address.
        """
        candidates = (
            # Cross-tenant by necessity: an API key identifies its own workspace.
            model.objects.unscoped().select_related("workspace").filter(lookup_prefix=lookup_prefix)
        )
        for candidate in candidates:
            if not candidate.matches(secret):
                continue
            if candidate.revoked_at is not None:
                return Resolution(None, recognised=True)
            if candidate.workspace is None or candidate.workspace.is_archived:
                return Resolution(None, recognised=True)
            return Resolution(candidate, recognised=True)
        return Resolution(None, recognised=False)
