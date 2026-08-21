"""Client-address resolution behind (or without) a reverse proxy.

BrightBean Studio's ``AuthRateLimitMiddleware`` reads the leftmost value of
``X-Forwarded-For`` unconditionally. That header is attacker-controlled: any
client can send ``X-Forwarded-For: 1.2.3.4`` and get a fresh rate-limit bucket
per request, which turns the limiter off. It is only trustworthy when a proxy
you control appends to it, and only for the entries that proxy appended.

So the header is ignored unless the peer (``REMOTE_ADDR``) is itself a trusted
proxy, and even then trusted hops are peeled off the **right**, where the proxy
chain appends — the leftmost entry is the one the client wrote.

``TRUSTED_PROXIES`` defaults to empty: trust nothing. It is unrelated to
``SECURE_PROXY_SSL_HEADER`` / ``USE_X_FORWARDED_HOST`` in
``config/settings/development.py``, which decide the request's *scheme and host*
for tunnelled webhook development. This setting decides the *client's identity*
for rate limiting, and getting that wrong disables a security control rather
than breaking a redirect.
"""

import ipaddress
from functools import lru_cache

from django.conf import settings
from django.http import HttpRequest

__all__ = ["get_client_ip", "is_trusted_proxy"]


@lru_cache(maxsize=8)
def _trusted_networks(entries: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse TRUSTED_PROXIES into networks, dropping anything unparseable.

    Cached on the parsed tuple rather than read from settings directly so
    ``override_settings`` in tests still produces a fresh parse.
    """
    networks = []
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            # A typo in TRUSTED_PROXIES must not silently widen trust; skipping
            # it fails closed (the header is ignored).
            continue
    return tuple(networks)


def is_trusted_proxy(address: str) -> bool:
    """True when ``address`` is covered by an entry in ``TRUSTED_PROXIES``."""
    networks = _trusted_networks(tuple(getattr(settings, "TRUSTED_PROXIES", []) or []))
    if not networks:
        return False
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError:
        return False
    return any(ip in network for network in networks)


def get_client_ip(request: HttpRequest) -> str:
    """Return the address to attribute this request to.

    ``REMOTE_ADDR`` unless the peer is a trusted proxy, in which case trusted
    hops are peeled off the right of ``X-Forwarded-For`` and the first untrusted
    address is returned. Never returns a client-supplied value from an untrusted
    peer.
    """
    remote_addr = (request.META.get("REMOTE_ADDR") or "").strip()
    if not is_trusted_proxy(remote_addr):
        return remote_addr

    forwarded = request.META.get("HTTP_X_FORWARDED_FOR") or ""
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    while hops and is_trusted_proxy(hops[-1]):
        hops.pop()
    return hops[-1] if hops else remote_addr
