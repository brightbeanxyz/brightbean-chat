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
   :func:`_deployment_hosts`.
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
from typing import Any

import httpx
from django.conf import settings

__all__ = [
    "CONNECT_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "GUARD_EXTENSION",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "BlockedURLError",
    "GuardedResponse",
    "OutboundError",
    "OutboundTransportError",
    "allow_private",
    "guarded_request",
    "max_response_bytes",
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

#: Redirects that turn the follow-up into a GET without a body, per RFC 9110.
#: 307 and 308 are deliberately absent — they preserve both.
_REWRITE_TO_GET = frozenset({301, 302, 303})

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Headers a caller may not set, because the guard owns them. ``Host`` is the
#: dangerous one: overriding it would let a caller point the pinned connection's
#: virtual host somewhere else, which is most of the way back to the attack the
#: pinning prevents.
_RESERVED_HEADERS = frozenset({"host", "content-length", "transfer-encoding"})

#: Headers dropped when a redirect crosses to a different origin. ``httpx`` does
#: this when it follows redirects itself; this module follows them, so it has to.
_ORIGIN_SCOPED_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


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

    @property
    def ok(self) -> bool:
        """SPEC §11.7's "2xx"."""
        return 200 <= self.status_code < 300

    @property
    def text(self) -> str:
        """The body decoded as text, never raising on bad bytes."""
        return self.content.decode(self.charset, errors="replace")

    @property
    def charset(self) -> str:
        encoding = self.headers.get("content-type", "")
        _, _, parameters = encoding.partition("charset=")
        candidate = parameters.split(";")[0].strip().strip('"') or "utf-8"
        try:
            "".encode(candidate)
        except LookupError:
            return "utf-8"
        return candidate

    def json(self) -> Any:
        """Parse the body as JSON. Raises ``ValueError`` like ``json.loads``."""
        import json as _json

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
    value = getattr(settings, "EXTERNAL_REQUEST_MAX_RESPONSE_BYTES", MAX_RESPONSE_BYTES)
    return int(value) if isinstance(value, int | str) else MAX_RESPONSE_BYTES


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


def _deployment_hosts() -> frozenset[str]:
    """Hostnames that mean "this deployment".

    ``APP_URL``'s host plus every literal ``ALLOWED_HOSTS`` entry. Wildcards are
    skipped: development sets ``ALLOWED_HOSTS = ["*"]``, and reading that as a
    hostname would deny every URL in the product.

    Denied on **any port**, not only the configured one. SPEC §11.7 says "the
    deployment's own host"; a deployment behind a proxy answers on 80, 443 and
    whatever gunicorn bound, and the interesting target — ``/internal/tick``, an
    admin page, another tenant's webhook URL — is reachable on all of them.

    Django's ``.example.com`` form (apex plus every subdomain) is read here as
    the apex alone. Treating it as a suffix would deny an integration at
    ``api.example.com`` that has nothing to do with this deployment, and the
    address check below still catches a subdomain that really does point here.
    """
    hosts = {str(host).strip().lower() for host in (getattr(settings, "ALLOWED_HOSTS", None) or [])}
    hosts = {host.lstrip(".") for host in hosts if host and "*" not in host}
    app_host = httpx.URL(str(getattr(settings, "APP_URL", "") or "")).host
    if app_host:
        hosts.add(app_host.lower())
    return frozenset(hosts)


def _deployment_addresses() -> frozenset[str]:
    """The addresses ``APP_URL``'s host resolves to, best effort.

    Only ``APP_URL``: resolving every ``ALLOWED_HOSTS`` entry would put a fan of
    DNS lookups in front of each outbound request, and ``APP_URL`` is the one
    entry a deployment is guaranteed to have set correctly (it is what every
    absolute link in the product is built from).

    Resolution failures answer "no addresses" rather than raising. This check is
    a second line behind the name match and the private-range rules; a
    deployment whose own name does not resolve from inside its network is
    ordinary, and turning that into a refused request would break every flow.
    """
    app_host = httpx.URL(str(getattr(settings, "APP_URL", "") or "")).host
    if not app_host:
        return frozenset()
    return frozenset(str(address) for address in resolve_host(app_host))


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
    #: ``host`` or ``host:port``, for the ``Host`` header and TLS SNI.
    authority: str
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

    host = (url.host or "").strip()
    if not host:
        raise BlockedURLError("That URL has no host.")
    lowered = host.lower()

    if lowered in own_hosts:
        raise BlockedURLError("That URL points back at this deployment.")

    addresses = resolve_host(host)
    if not addresses:
        raise BlockedURLError(f"{lowered} does not resolve.")

    for address in addresses:
        refusal = _refusal(address)
        if refusal:
            raise BlockedURLError(f"{lowered} resolves to {refusal}.")
        if str(address) in own_addresses:
            raise BlockedURLError("That URL points back at this deployment.")

    port = url.port
    authority = f"{lowered}:{port}" if port is not None else lowered
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
        except UnicodeEncodeError:
            logger.warning("Outbound request: header %r is not an ASCII name and was dropped.", name)
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

    ``timeout`` is the budget for the **whole** call, redirects included: three
    hops at ten seconds each would otherwise be a thirty-second request behind a
    contact's advisory lock.

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
    own_hosts = _deployment_hosts()
    own_addresses = _deployment_addresses()

    send_headers = _clean_headers(headers)
    current_method = str(method or "GET").upper()
    body_json, body_content = json, content

    owned = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(budget, connect=min(CONNECT_TIMEOUT, budget)), trust_env=False)
    try:
        target = _validate(_parsed(url), own_hosts=own_hosts, own_addresses=own_addresses)
        for hop in range(MAX_REDIRECTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OutboundTransportError(f"{target.host} timed out")

            status, response_headers, body, truncated = _send(
                http,
                target,
                current_method,
                send_headers,
                body_json,
                body_content,
                remaining=remaining,
                cap=cap,
            )

            location = response_headers.get("location", "").strip() if status in _REDIRECT_STATUSES else ""
            if not location:
                return GuardedResponse(
                    status_code=status,
                    headers=response_headers,
                    content=body,
                    truncated=truncated,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    final_url=str(target.logical),
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
            if status in _REWRITE_TO_GET and current_method not in ("GET", "HEAD"):
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
    remaining: float,
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
    """
    request_headers = dict(headers)
    request_headers["Host"] = target.authority

    try:
        with http.stream(
            method,
            target.pinned,
            headers=request_headers,
            json=body_json,
            content=body_content,
            timeout=httpx.Timeout(remaining, connect=min(CONNECT_TIMEOUT, remaining)),
            follow_redirects=False,
            extensions={"sni_hostname": target.host, GUARD_EXTENSION: True},
        ) as response:
            status = response.status_code
            response_headers = response.headers
            if status in _REDIRECT_STATUSES and response_headers.get("location"):
                response.close()
                return status, response_headers, b"", False

            declared = response_headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > cap:
                # Refused before a single byte of it is read: the point of a cap
                # is that an oversized body never becomes memory pressure.
                response.close()
                logger.info("Outbound response from %s declared %s bytes; cut off at %s.", target.host, declared, cap)
                return status, response_headers, b"", True

            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= cap:
                    truncated = True
                    break
            body = b"".join(chunks)[:cap]
            return status, response_headers, body, truncated
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
