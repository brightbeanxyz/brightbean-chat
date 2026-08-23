"""The SSRF guard — SECURITY-BASELINE §6, SPEC §11.7 and §19.

    Any server-initiated request to a user-supplied or contact-supplied URL goes
    through the SSRF guard (``guarded_request``, issue #15) — External Request
    node, outbound webhook deliveries, media fetch-by-URL, provider callbacks.
    **No exceptions**; new call sites add a test proving the guard is in the path.

This module is that path, and it is the *only* one. Nothing else in this
repository may open an HTTP connection to a URL a user, a flow author or a
contact supplied. :func:`apps.channels.providers.base.request_json` is the
sibling for the other case — URLs an adapter builds from constants and stored
ids — and its docstring says so; a call site that cannot tell which of the two
it is wants this one.

Not to be confused with :mod:`apps.common.net`, which resolves the *client's*
address behind a reverse proxy and has nothing to do with outbound traffic.

**The attack this defends against.** A flow author writes
``https://api.partner.test/{{order_id}}`` and the server fetches it. From inside
the deployment's network that server can reach things nobody outside can:
``127.0.0.1:8000`` (this very application, including ``/internal/tick``),
``169.254.169.254`` (the cloud instance-metadata service and its credentials),
``10.0.0.5`` (the database). A URL is a request the *author* writes and the
*server* makes, which is the whole shape of server-side request forgery.

**Five checks, in order, on every hop.**

1. **Scheme.** ``http`` and ``https`` only. ``file:``, ``gopher:``, ``ftp:`` and
   ``data:`` are refused before anything else looks at the URL. Userinfo
   (``https://real.test@evil.test/``) is refused too: it is a display-confusion
   trick with no legitimate use here, and ``headers`` already exists for auth.
2. **Resolve**, through :func:`resolve_host` — one function, so a test has one
   thing to patch and there is one place DNS enters this module.
3. **Address rules**, applied to *every* address the name resolved to, not just
   the first. A name that returns one public and one private address is a
   rebinding attempt with the timing removed.
4. **The deployment's own host**, by name and by address. See
   :func:`_deployment_identity`.
5. **Pin.** The connection is made to the literal IP that was just checked, with
   ``Host`` and TLS SNI carrying the original hostname. Nothing re-resolves, so
   there is no window between the check and the connect for DNS to change its
   answer.

**Why pinning is not optional.** Steps 2–4 answer "where does this name point?"
and the connection asks the same question again a few milliseconds later. An
attacker controlling the authoritative nameserver answers ``93.184.216.34`` the
first time and ``127.0.0.1`` the second, with a one-second TTL, and every check
above passes while the request goes to loopback. Handing the socket a literal
address closes that window by never asking twice.

**Redirects re-run all five checks.** A public URL answering ``302 Location:
http://169.254.169.254/`` is the same attack wearing a hat, so redirects are not
followed by ``httpx`` (which would resolve the target itself) but by the loop in
:func:`guarded_request`, capped at :data:`MAX_REDIRECTS`.

**Proxies are off** (``trust_env=False``). An ``HTTPS_PROXY`` in the environment
would hand the pinned address back to a resolver in the proxy, which is the one
configuration where every guarantee above quietly stops holding.

**The body is read uncompressed**, because a size cap on a compressed body is
not a size cap. ``httpx`` decompresses before the caller counts anything, so a
65 KB gzip response — well under any ``Content-Length`` check — expands to 64 MB
before a limit on the decoded stream can fire. The guard asks for
``Accept-Encoding: identity`` and declines to expand a body that arrives
compressed anyway (:func:`_refuses_body`), so the bytes counted and the bytes
allocated are the same bytes.

**``timeout`` is a wall clock, not a read timeout.** httpx's is per-read, so a
server dripping one byte just inside it never trips it; the deadline is checked
between hops *and on every chunk of the body*, which is what stops a nominally
ten-second request from holding a contact's advisory lock for as long as the far
end cares to keep talking. The one thing it does not cover is name resolution:
``socket.getaddrinfo`` takes no timeout and cannot be interrupted, so that is
bounded by the OS resolver's configuration. The deadline is re-checked after
every resolution and the deployment's own host is resolved from cache, but a
caller needing a bound on *that* needs one around :func:`guarded_request`.

**Proving a call site uses this.** ``tests/ssrf.py`` exposes ``guard_required()``,
which fails any HTTP request made inside its block that did not come from here.
Baseline §6's "new call sites add a test proving the guard is in the path" means
that helper, not a comment.
"""

import ipaddress
import logging
import socket
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx
from django.conf import settings

from apps.common.jsonlimits import max_json_depth

__all__ = [
    "CONNECT_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "DEPLOYMENT_ADDRESS_TTL",
    "GUARD_EXTENSION",
    "MAX_JSON_DEPTH",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "BlockedURLError",
    "GuardedResponse",
    "OutboundError",
    "OutboundTransportError",
    "allow_private",
    "guarded_request",
    "max_response_bytes",
    "reset_deployment_cache",
    "resolve_host",
]

logger = logging.getLogger(__name__)

#: SPEC §11.7 caps the External Request node at 10 seconds, and that is the
#: longest anything here should ever wait: the flow engine makes this call with
#: the contact's advisory lock held (SPEC §9.6), so the timeout is also the
#: bound on how long one contact is blocked behind it.
DEFAULT_TIMEOUT = 10.0

#: Shorter than the read timeout, for the same reason
#: ``apps.channels.providers.base`` gives: a host that has not completed a TCP
#: handshake in five seconds is not about to answer in time either.
CONNECT_TIMEOUT = 5.0

#: Issue #15: "response size cap (1 MB) with streaming cutoff". A cap that is
#: only checked after the body is in memory is not a cap.
MAX_RESPONSE_BYTES = 1024 * 1024

#: Issue #15: "re-validate on redirects (cap 3)".
MAX_REDIRECTS = 3

#: Marker set on every request this module issues. ``tests/ssrf.py`` asserts on
#: its presence, which is what makes "did this code path use the guard?" a
#: question with a mechanical answer rather than a reviewer's opinion.
GUARD_EXTENSION = "brightbean_guarded"

#: RFC 9110 §15.4.4/15.4.8: a 303 turns *any* follow-up into a bodyless GET.
_SEE_OTHER = 303

#: 301 and 302 rewrite **POST** to GET, and only POST. That rewrite is a
#: historical quirk browsers baked in for form submissions; RFC 9110 §15.4.2
#: says it "is known to change the request method" for POST, and every other
#: method keeps its semantics. Rewriting a PUT, PATCH or DELETE here would mean
#: a permanently-moved endpoint silently receiving a *read* where the flow
#: author wrote a write — the request would appear to succeed and do nothing.
_REWRITE_POST_TO_GET = frozenset({301, 302})

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Depth cap for a response body parsed as JSON (SECURITY-BASELINE §7). Applied
#: to the bytes, before ``json.loads`` recurses through them — see
#: :func:`apps.common.jsonlimits.max_json_depth` for why after is too late. Well
#: above any real API's nesting and far below what exhausts a parser.
MAX_JSON_DEPTH = 50

#: Headers a caller may not set, because the guard owns them. ``Host`` is the
#: dangerous one: overriding it would let a caller point the pinned connection's
#: virtual host somewhere else, which is most of the way back to the attack the
#: pinning prevents. ``Accept-Encoding`` is here for the size cap: the guard only
#: advertises codecs it can inflate under a bound (see :func:`_inflate`).
_RESERVED_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "accept-encoding"})

#: Headers dropped when a redirect crosses to a different origin. ``httpx`` does
#: this when it follows redirects itself; this module follows them, so it has to.
_ORIGIN_SCOPED_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})

#: The guard asks for an uncompressed body, and that is what makes the size cap
#: real. ``httpx`` decompresses before a caller can count anything, so a cap
#: applied to its output is applied *after* the allocation it exists to prevent:
#: measured, a 65 KB gzip body — comfortably under a 1 MB ``Content-Length``
#: check — expanded to 64 MB in one chunk. Asking for ``identity`` means the
#: bytes on the wire and the bytes in memory are the same bytes, so counting one
#: bounds the other.
_ACCEPT_ENCODING = "identity"

#: How long a resolution of the deployment's own host is reused. Cached because
#: it sits in front of *every* outbound request and changes about as often as
#: the deployment is redeployed; bucketed rather than held forever so a moved
#: deployment starts being recognised again without a restart.
DEPLOYMENT_ADDRESS_TTL = 300


class OutboundError(Exception):
    """Base for every refusal and failure in this module."""


class BlockedURLError(OutboundError):
    """The URL, or something it resolved or redirected to, is not allowed.

    Carries no address or full URL in its message — this text reaches flow
    ``last_error`` columns, the inbox and logs (SECURITY-BASELINE §5), and the
    path or query of a user-authored URL routinely carries an API key.
    """


class OutboundTransportError(OutboundError):
    """The request was allowed and did not complete: timeout, DNS, connection."""


@dataclass(frozen=True)
class GuardedResponse:
    """What a guarded call returns.

    Deliberately *not* an ``httpx.Response``. A response object would tempt a
    caller into ``response.read()``, which is precisely the unbounded read the
    streaming cap exists to prevent, and it would carry the pinned IP in its
    ``url`` where every log line and error message would repeat it. This shape
    forces the caller to see :attr:`truncated`.
    """

    status_code: int
    headers: httpx.Headers
    content: bytes
    #: True when the body hit :data:`MAX_RESPONSE_BYTES` and was cut off. A
    #: truncated body is almost never parseable, and a caller that silently
    #: parsed one would report "not JSON" for what is really "too big".
    truncated: bool
    elapsed_ms: int
    #: The **logical** URL the response came from — hostname, not pinned IP, and
    #: after any redirects.
    final_url: str
    #: That URL's host on its own, already lowercased. Carried rather than left
    #: for the caller to re-parse: every log line and error message wants the
    #: host and nothing else (SECURITY-BASELINE §5), and the guard has already
    #: computed it.
    final_host: str = ""

    @property
    def ok(self) -> bool:
        """SPEC §11.7's "2xx"."""
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        """The body decoded as text. **Never raises** — see :attr:`charset`."""
        return self._decode(self.content)

    def text_prefix(self, max_chars: int) -> str:
        """The first ``max_chars`` characters of :attr:`text`, cheaply.

        Slices the *bytes* first, so keeping a two-kilobyte excerpt of a
        megabyte body does not decode the megabyte. Four bytes per character is
        UTF-8's maximum, so the byte slice can only over-read, never under-read;
        a multi-byte sequence cut in half is what ``errors="replace"`` is for.
        """
        return self._decode(self.content[: max(max_chars, 0) * 4])[:max_chars]

    def _decode(self, raw: bytes) -> str:
        """Decode ``raw`` under this response's charset, falling back to UTF-8.

        The fallback is not belt-and-braces. ``charset`` comes from a stranger's
        ``Content-Type`` header, and a codec that survives the probe there can
        still fail on real bytes, so the operation is attempted rather than
        trusted. Anything that goes wrong is a body we describe as best we can,
        never an exception: this is called from a flow node, and the runner does
        not catch — a raise here would roll the step back and hand a remote
        server the power to choose when this deployment retries.
        """
        try:
            return raw.decode(self.charset, errors="replace")
        except (UnicodeError, LookupError, TypeError, ValueError):  # pragma: no cover - the probe catches these
            return raw.decode("utf-8", errors="replace")

    @property
    def charset(self) -> str:
        """The declared charset, if this process can actually decode with it.

        Validated by **decoding a probe**, which is the whole point. An earlier
        version proved the codec could *encode* an empty string, and three real
        codec names pass that test and then raise on decode: ``undefined``
        raises unconditionally, ``idna`` rejects ``errors="replace"``, and
        ``punycode`` raises ``UnicodeDecodeError`` on any non-ASCII byte. The
        header is attacker-controlled, so the check has to be the same operation
        the caller will perform, not one that merely resembles it.
        """
        declared = self.headers.get("content-type", "")
        _, _, parameters = declared.partition("charset=")
        candidate = parameters.split(";")[0].strip().strip("\"'") or "utf-8"
        try:
            b"\xc3\xa9".decode(candidate, errors="replace")
        except (UnicodeError, LookupError, TypeError, ValueError):
            return "utf-8"
        return candidate

    def json(self) -> Any:
        """Parse the body as JSON. Raises ``ValueError`` like ``json.loads``.

        Depth-capped first, on the **bytes**. ``json.loads`` recurses, so a
        document nested ten thousand deep — a few kilobytes, nowhere near the
        size cap — raises ``RecursionError``, which is not a ``ValueError`` and
        so is not what any caller of this method is catching. It would escape
        the External Request node's no-raise contract, roll the flow step back
        and have the queue call the same endpoint again. Whether it raises at
        all depends on how much stack the caller had left, which is why the cap
        is on the input rather than on the outcome.
        """
        import json as _json

        depth = max_json_depth(self.content)
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"JSON nested {depth} deep; this guard parses at most {MAX_JSON_DEPTH}.")
        return _json.loads(self.text)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def allow_private() -> bool:
    """Whether RFC1918 and unique-local addresses are permitted.

    SPEC §11.7's ``EXTERNAL_REQUEST_ALLOW_PRIVATE=false``. It exists for on-prem
    deployments whose partner services genuinely live on ``10.0.0.0/8``, and it
    flips **only** the not-globally-routable rule: loopback, link-local (the
    metadata service), multicast, reserved, site-local and the deployment's own
    host stay denied however it is set, because none of those is ever a
    legitimate integration target and every one of them is a documented SSRF
    payload.
    """
    return bool(getattr(settings, "EXTERNAL_REQUEST_ALLOW_PRIVATE", False))


def max_response_bytes() -> int:
    """The response cap, from settings.

    A value that will not parse falls back to the default rather than raising.
    ``config/settings/base.py`` uses ``env.int``, so a bad *env var* is caught at
    boot — but this reads whatever is on the settings object, and a ``ValueError``
    escaping here is not an :class:`OutboundError`, so it would sail past every
    caller's handler and crash a flow step instead of failing it.
    """
    value = getattr(settings, "EXTERNAL_REQUEST_MAX_RESPONSE_BYTES", MAX_RESPONSE_BYTES)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        logger.warning("EXTERNAL_REQUEST_MAX_RESPONSE_BYTES is not a number; using %s.", MAX_RESPONSE_BYTES)
        return MAX_RESPONSE_BYTES
    return parsed if parsed > 0 else MAX_RESPONSE_BYTES


def resolve_host(host: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    """Every address ``host`` resolves to, or ``()``.

    **The one place DNS enters this module**, which is what makes the rebinding
    tests possible: they patch this and count the calls, and a guard that
    resolved anywhere else would pass them while being wrong.

    A literal address short-circuits without a lookup — ``getaddrinfo`` would
    hand it straight back, and skipping it keeps the common case (and every
    test) free of a syscall that can block.
    """
    literal = host.strip().strip("[]")
    try:
        return (ipaddress.ip_address(literal),)
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return ()

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        # ``info[4]`` is the sockaddr: (host, port) for v4, four elements for
        # v6. The host may carry a ``%en0`` scope suffix, which is not part of
        # the address (and every scoped address is link-local, so denied).
        candidate = str(info[4][0]).split("%")[0]
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:  # pragma: no cover - getaddrinfo returns parseable addresses
            continue
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def _unwrap(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Peel a v4 address out of the v6 form that is carrying it.

    ``::ffff:127.0.0.1`` is loopback, but ``IPv6Address.is_loopback`` is False
    for it — the flag describes ``::1``. The same is true of 6to4
    (``2002:7f00:1::``) and Teredo, both of which embed a v4 address that the v6
    predicates never look at. Every one of these is a published SSRF filter
    bypass, so the embedded address is extracted and checked as itself.
    """
    if not isinstance(address, ipaddress.IPv6Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        return address.teredo[1]
    return address


def _refusal(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """Why this address is not allowed, or ``""``.

    **Order matters.** ``127.0.0.1`` and ``169.254.169.254`` are both *also*
    ``is_private``, so the categories that stay denied whatever
    ``EXTERNAL_REQUEST_ALLOW_PRIVATE`` says are tested first. Getting that
    backwards would make the on-prem flag open loopback and the cloud metadata
    service, which is the bypass this whole module exists to prevent.

    **The last rule is ``is_global``, not ``is_private``**, and the difference
    is not academic. ``100.64.0.0/10`` — carrier-grade NAT, "shared address
    space" — answers ``is_private = False`` and ``is_reserved = False`` in
    Python's ``ipaddress``, so a check written as "deny private" lets it
    straight through while it is exactly the sort of network an SSRF is looking
    for. Anything the registry does not call globally routable is treated as
    somebody's internal network, which is the direction to be wrong in; the
    on-prem flag is what opens that whole bucket for a deployment that means to.
    """
    ip = _unwrap(address)
    if ip.is_unspecified:
        return "an unspecified address"
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_reserved:
        return "a reserved address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.is_site_local:
        return "a site-local address"
    if not ip.is_global and not allow_private():
        return "a private address"
    return ""


def _deployment_identity() -> tuple[frozenset[str], frozenset[str]]:
    """``(hostnames, addresses)`` that mean "this deployment".

    The names are ``APP_URL``'s host plus every literal ``ALLOWED_HOSTS`` entry.
    Wildcards are skipped: development sets ``ALLOWED_HOSTS = ["*"]``, and
    reading that as a hostname would deny every URL in the product. Django's
    ``.example.com`` form (apex plus every subdomain) is read as the apex alone —
    treating it as a suffix would deny an integration at ``api.example.com`` that
    has nothing to do with this deployment, and the address check still catches a
    subdomain that really does point here.

    Denied on **any port**, not only the configured one. SPEC §11.7 says "the
    deployment's own host"; a deployment behind a proxy answers on 80, 443 and
    whatever gunicorn bound, and the interesting target — ``/internal/tick``, an
    admin page, another tenant's webhook URL — is reachable on all of them.

    The addresses come from ``APP_URL`` alone. Resolving every ``ALLOWED_HOSTS``
    entry would put a fan of DNS lookups in front of each outbound request, and
    ``APP_URL`` is the one entry a deployment is guaranteed to have set correctly
    (it is what every absolute link in the product is built from). Resolution
    failures answer "no addresses" rather than raising: this is a second line
    behind the name match, and a deployment whose own name does not resolve from
    inside its network is ordinary rather than a reason to break every flow.

    Both halves are derived from one parse of ``APP_URL``, and the resolution is
    cached — see :func:`_resolved_deployment_addresses`.
    """
    hosts = {str(host).strip().lower() for host in (getattr(settings, "ALLOWED_HOSTS", None) or [])}
    hosts = {host.lstrip(".") for host in hosts if host and "*" not in host}
    app_host = (httpx.URL(str(getattr(settings, "APP_URL", "") or "")).host or "").lower()
    if not app_host:
        return frozenset(hosts), frozenset()
    hosts.add(app_host)
    bucket = int(time.monotonic() // DEPLOYMENT_ADDRESS_TTL)
    return frozenset(hosts), _resolved_deployment_addresses(app_host, bucket)


@lru_cache(maxsize=16)
def _resolved_deployment_addresses(app_host: str, _bucket: int) -> frozenset[str]:
    """``APP_URL``'s addresses, cached for :data:`DEPLOYMENT_ADDRESS_TTL`.

    Cached on the same reasoning as :func:`apps.common.net._trusted_networks`:
    this runs in front of every outbound request and its answer is a property of
    the deployment, not of the request. Uncached it was a synchronous DNS round
    trip per call, taken inside the flow engine's transaction with the contact's
    advisory lock held, for a value that had not changed since the last one.

    ``_bucket`` is a coarse clock, so the entry expires instead of being pinned
    for the process's life — ``lru_cache`` has no TTL of its own, and a
    deployment that moves should start recognising itself again without a
    restart. Tests that swap :func:`resolve_host` must call
    :func:`reset_deployment_cache`.
    """
    # Stored **unwrapped**, because that is how they are compared. Without it a
    # deployment on 10.0.0.5 is reachable as ``::ffff:10.0.0.5``: the category
    # rules unwrap before testing, so with EXTERNAL_REQUEST_ALLOW_PRIVATE on the
    # address passes them, and ``str(::ffff:10.0.0.5)`` is ``'::ffff:a00:5'``,
    # which matches no entry written as ``'10.0.0.5'``.
    return frozenset(str(_unwrap(address)) for address in resolve_host(app_host))


def reset_deployment_cache() -> None:
    """Forget the cached resolution of the deployment's own host.

    For tests that patch :func:`resolve_host`, and for an operator who has moved
    the deployment and does not want to wait out the TTL.
    """
    _resolved_deployment_addresses.cache_clear()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    """One validated hop: where to connect, and what to claim to be."""

    #: The URL as written — hostname intact. What logs and ``final_url`` show.
    logical: httpx.URL
    #: The same URL with the host replaced by the pinned literal address.
    pinned: httpx.URL
    #: The URL's authority in **wire** form — ASCII, IPv6 in brackets, port
    #: included. What the ``Host`` header carries.
    authority: str
    #: The ASCII (punycode) host on its own, no brackets and no port. What TLS
    #: SNI carries, what logs name, and what ``origin`` compares.
    host: str
    origin: tuple[str, str, int]


def _validate(url: httpx.URL, *, own_hosts: frozenset[str], own_addresses: frozenset[str]) -> _Target:
    """Run every check on one URL and return what to connect to.

    Raises :class:`BlockedURLError` for anything refused. Called once per hop,
    redirects included — a check that ran only on the first URL would be a
    check an attacker skips by answering ``302``.

    The deployment's own identity is passed in rather than read here so that a
    four-hop redirect chain costs one lookup of it instead of four.
    """
    scheme = url.scheme.lower()
    if scheme not in ("http", "https"):
        raise BlockedURLError(f"{scheme or 'that'} is not a scheme this server will request; use http or https.")
    if url.userinfo:
        raise BlockedURLError("A URL carrying a username or password is not allowed; use a header instead.")

    display = (url.host or "").strip().lower()
    if not display:
        raise BlockedURLError("That URL has no host.")

    # The **wire** forms, not the display ones. ``URL.host`` decodes an
    # internationalized name back to Unicode for humans to read, and putting
    # that in a ``Host`` header or a TLS SNI extension raises
    # ``UnicodeEncodeError`` from inside httpx — which is not an OutboundError
    # and so escapes every caller's handler. ``raw_host`` and ``netloc`` are
    # what actually goes on the wire: punycode, lowercased, IPv6 bracketed and
    # the port kept.
    try:
        lowered = url.raw_host.decode("ascii").lower()
        authority = url.netloc.decode("ascii")
    except (UnicodeDecodeError, AttributeError) as exc:  # pragma: no cover - httpx normalises to ASCII
        raise BlockedURLError("That URL's host cannot be encoded for a request.") from exc
    if not lowered:
        raise BlockedURLError("That URL has no host.")

    # Both spellings, so a deployment listed under either its Unicode or its
    # punycode name is recognised under the other.
    if lowered in own_hosts or display in own_hosts:
        raise BlockedURLError("That URL points back at this deployment.")

    addresses = resolve_host(lowered)
    if not addresses:
        raise BlockedURLError(f"{lowered} does not resolve.")

    for address in addresses:
        refusal = _refusal(address)
        if refusal:
            raise BlockedURLError(f"{lowered} resolves to {refusal}.")
        if str(_unwrap(address)) in own_addresses:
            raise BlockedURLError("That URL points back at this deployment.")

    port = url.port
    default_port = 443 if scheme == "https" else 80
    try:
        pinned = url.copy_with(host=str(addresses[0]))
    except (httpx.InvalidURL, ValueError, TypeError) as exc:
        # Every refusal in this module has to be a BlockedURLError, because that
        # is what callers route on. A URL httpx will parse but not rebuild is
        # rare and is still an input, not a bug.
        raise BlockedURLError("That URL cannot be requested against a fixed address.") from exc
    return _Target(
        logical=url,
        pinned=pinned,
        authority=authority,
        host=lowered,
        origin=(scheme, lowered, port if port is not None else default_port),
    )


def _parsed(url: str, *, base: httpx.URL | None = None) -> httpx.URL:
    """``url`` as an ``httpx.URL``, resolved against ``base`` when it is relative.

    A parse failure is a :class:`BlockedURLError` rather than whatever ``httpx``
    raises, and that matters at both call sites. The caller's URL is
    user-authored, and the redirect's ``Location`` is written by the far end —
    a stranger's server — so "unparseable" must be a refusal the caller routes
    on, not the one input that crashes a flow step.
    """
    try:
        return base.join(url) if base is not None else httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError) as exc:
        raise BlockedURLError("That is not a URL this server can request.") from exc


def _redirect_location(status: int, headers: httpx.Headers) -> str:
    """The ``Location`` this response redirects to, or ``""``.

    One function because two places ask the question — :func:`_send`, deciding
    whether to spend the cap on a body it is about to discard, and
    :func:`guarded_request`, deciding whether to follow. They disagreed once: one
    tested the raw header and the other its stripped form, so a
    ``Location:`` holding only spaces made ``_send`` drop a body that
    ``guarded_request`` then returned as the final response, empty.
    """
    if status not in _REDIRECT_STATUSES:
        return ""
    return headers.get("location", "").strip()


def _refuses_body(encoding: str) -> bool:
    """True when this ``Content-Encoding`` is one the guard will not expand.

    The guard sent ``Accept-Encoding: identity``, so a compressed body is a
    server ignoring the request — and its decompressed size is a number this
    process learns only by allocating it. Declining is the one answer that keeps
    :data:`MAX_RESPONSE_BYTES` a real bound rather than an aspiration: the check
    is on the *header*, before a byte of the body is read, so a compression bomb
    never becomes memory at all.

    The cost is honest and logged: an integration behind a server that
    compresses unasked gets an empty body and a ``truncated`` flag, which its
    caller reports as a skipped mapping. That is a visible, diagnosable
    failure, and the alternative on offer was an unbounded allocation in a
    worker holding a contact's advisory lock.
    """
    return bool(encoding) and encoding != "identity"


def _clean_headers(headers: Mapping[str, Any] | Iterable[tuple[str, Any]] | None) -> dict[str, str]:
    """Drop headers that are not safe to forward, with a reason in the log.

    Skipped rather than raised on: a flow author typing a stray newline into a
    header value should not fail the run, and the header is the part that must
    not survive. Nothing here logs a header **value** — they carry API keys, and
    SECURITY-BASELINE §5 puts them out of logs entirely.
    """
    items: Iterable[tuple[str, Any]]
    if headers is None:
        return {}
    items = headers.items() if isinstance(headers, Mapping) else headers

    cleaned: dict[str, str] = {}
    for raw_name, raw_value in items:
        name = str(raw_name).strip()
        value = str(raw_value)
        if not name:
            continue
        if name.lower() in _RESERVED_HEADERS:
            logger.warning("Outbound request: header %r is set by the guard and cannot be overridden.", name)
            continue
        if any(character in name or character in value for character in ("\r", "\n", "\x00")):
            # Header splitting: a value ending "\r\nX-Admin: 1" is two headers
            # by the time it reaches the origin server.
            logger.warning("Outbound request: header %r contains a control character and was dropped.", name)
            continue
        try:
            name.encode("ascii")
            value.encode("ascii")
        except UnicodeEncodeError:
            # The *value* matters as much as the name and used to go
            # unchecked: httpx ASCII-encodes it when building the request and
            # raises ``UnicodeEncodeError``, which is not an OutboundError, so
            # a header rendered from a contact called "Jörg" crashed the flow
            # step instead of following its error path. RFC 9110 §5.5 makes
            # non-ASCII field values obsolete, and there is no encoding to
            # guess on the author's behalf — so it is dropped, loudly, like
            # every other header this function refuses.
            logger.warning("Outbound request: header %r is not ASCII and was dropped.", name)
            continue
        cleaned[name] = value
    return cleaned


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


def guarded_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
    json: Any = None,
    content: bytes | str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int | None = None,
    client: httpx.Client | None = None,
) -> GuardedResponse:
    """Make one HTTP request to a URL somebody else chose.

    Every rule in this module's docstring applies. Raises
    :class:`BlockedURLError` when the URL (or a redirect from it) is refused and
    :class:`OutboundTransportError` when an allowed request does not complete; a
    non-2xx status is **not** an error here — it is a
    :class:`GuardedResponse` with that status, because the caller decides what
    a 404 means.

    ``timeout`` is the wall-clock budget for the **HTTP** phase of the whole
    call — every redirect hop and every chunk of the body, not a per-read
    inactivity timeout. It does **not** bound name resolution:
    ``socket.getaddrinfo`` takes no timeout and cannot be interrupted, so a
    blackholed nameserver is bounded by the OS resolver's own configuration
    (``options timeout``/``attempts`` in ``resolv.conf``). The deadline is
    re-checked after every resolution, so a slow lookup fails the call rather
    than also spending the HTTP budget, and the deployment's own host is
    resolved from cache — but a caller needing a bound on resolution itself
    needs one around ``guarded_request``.

    ``client`` is the test seam, the same one
    :func:`apps.channels.providers.base.request_json` offers — pass an
    ``httpx.Client`` on a ``MockTransport`` and no socket is opened.
    """
    cap = max_response_bytes() if max_bytes is None else int(max_bytes)
    budget = max(float(timeout), 0.1)
    started = time.monotonic()
    deadline = started + budget

    # Resolved once, not per hop: on a redirect chain these answers cannot
    # change, and re-deriving them would put a DNS lookup on every hop.
    own_hosts, own_addresses = _deployment_identity()

    send_headers = _clean_headers(headers)
    current_method = str(method or "GET").upper()
    body_json, body_content = json, content

    owned = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(budget, connect=min(CONNECT_TIMEOUT, budget)), trust_env=False)
    try:
        target = _validate(_parsed(url), own_hosts=own_hosts, own_addresses=own_addresses)
        for hop in range(MAX_REDIRECTS + 1):
            status, response_headers, body, truncated = _send(
                http,
                target,
                current_method,
                send_headers,
                body_json,
                body_content,
                deadline=deadline,
                cap=cap,
            )

            location = _redirect_location(status, response_headers)
            if not location:
                return GuardedResponse(
                    status_code=status,
                    headers=response_headers,
                    content=body,
                    truncated=truncated,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    final_url=str(target.logical),
                    final_host=target.host,
                )
            if hop == MAX_REDIRECTS:
                raise BlockedURLError(f"{target.host} redirected more than {MAX_REDIRECTS} times.")

            following = _validate(
                _parsed(location, base=target.logical), own_hosts=own_hosts, own_addresses=own_addresses
            )
            if following.origin != target.origin:
                send_headers = {
                    name: value for name, value in send_headers.items() if name.lower() not in _ORIGIN_SCOPED_HEADERS
                }
            if status == _SEE_OTHER or (status in _REWRITE_POST_TO_GET and current_method == "POST"):
                current_method = "GET"
                body_json, body_content = None, None
            target = following
        raise AssertionError("unreachable")  # pragma: no cover - the loop returns or raises on every path
    finally:
        if owned:
            http.close()


def _send(
    http: httpx.Client,
    target: _Target,
    method: str,
    headers: dict[str, str],
    body_json: Any,
    body_content: bytes | str | None,
    *,
    deadline: float,
    cap: int,
) -> tuple[int, httpx.Headers, bytes, bool]:
    """One hop against the pinned address, with the body read under the cap.

    The three extensions are the whole pinning mechanism:

    * the URL carries the literal address, so nothing resolves the name again;
    * ``Host`` carries the real authority, so name-based virtual hosting and
      redirects on the far side still work;
    * ``sni_hostname`` carries it too, so the TLS handshake presents the real
      name and the certificate is verified against it rather than against an IP
      no certificate names.

    A redirect carrying a ``Location`` has its body dropped unread: the caller
    never sees it, and reading it would spend the budget and the cap on a hop
    that is about to be replaced.

    ``deadline`` rather than a duration, because the body loop has to check it.
    httpx's read timeout is **per read**, not total: a server dripping one byte
    every nine seconds never trips a ten-second read timeout, and a loop that
    only checked the clock between hops would sit there until the size cap was
    reached — a nominally ten-second request holding a contact's advisory lock
    for as long as the far end cares to keep talking.
    """
    request_headers = dict(headers)
    request_headers["Host"] = target.authority
    request_headers["Accept-Encoding"] = _ACCEPT_ENCODING

    def remaining() -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise OutboundTransportError(f"{target.host} took longer than the request budget allows")
        return left

    try:
        with http.stream(
            method,
            target.pinned,
            headers=request_headers,
            json=body_json,
            content=body_content,
            timeout=httpx.Timeout(remaining(), connect=min(CONNECT_TIMEOUT, remaining())),
            follow_redirects=False,
            extensions={"sni_hostname": target.host, GUARD_EXTENSION: True},
        ) as response:
            status = response.status_code
            response_headers = response.headers
            if _redirect_location(status, response_headers):
                response.close()
                return status, response_headers, b"", False

            declared = response_headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > cap:
                # Refused before a single byte of it is read: the point of a cap
                # is that an oversized body never becomes memory pressure. Only
                # a lower bound when the body is compressed, which is why the
                # streaming cap below counts wire bytes rather than trusting it.
                response.close()
                logger.info("Outbound response from %s declared %s bytes; cut off at %s.", target.host, declared, cap)
                return status, response_headers, b"", True

            encoding = response_headers.get("content-encoding", "").strip().lower()
            if _refuses_body(encoding):
                response.close()
                logger.info(
                    "Outbound response from %s is %s-encoded though identity was requested; body declined.",
                    target.host,
                    encoding,
                )
                return status, response_headers, b"", True

            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in response.iter_bytes():
                remaining()  # the wall clock, not httpx's per-read inactivity timeout
                chunks.append(chunk)
                size += len(chunk)
                # ``>``, not ``>=``: a body that is exactly ``cap`` bytes long
                # arrived whole, and calling it truncated makes callers throw
                # away a complete response — the node skips every one of its
                # response mappings on a truncated body.
                if size > cap:
                    truncated = True
                    break
            return status, response_headers, b"".join(chunks)[:cap], truncated
    except httpx.InvalidURL as exc:
        # httpx parses ``Location`` to populate ``response.next_request`` even
        # with ``follow_redirects=False``, so a far end answering
        # ``Location: javascript:alert(1)`` raises from inside ``stream()``.
        # ``InvalidURL`` is not an ``HTTPError`` — it does not inherit from it —
        # so without this clause it would escape as an unhandled exception, roll
        # the flow engine's step back and put the request on the queue's retry
        # ladder. A stranger's server would be choosing when this deployment
        # retries, which is the storm SPEC §11.7 exists to avoid.
        raise BlockedURLError(f"{target.host} answered with a location that is not a URL.") from exc
    except httpx.TimeoutException as exc:
        raise OutboundTransportError(f"{target.host} timed out") from exc
    except httpx.HTTPError as exc:
        # ``type(exc).__name__`` rather than ``str(exc)``: httpx puts the full
        # URL — query string and any credential in it — into transport errors.
        raise OutboundTransportError(f"{target.host} could not be reached: {type(exc).__name__}") from exc
