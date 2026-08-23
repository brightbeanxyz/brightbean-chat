"""Proving an email provider's notification really came from that provider.

Two schemes, because the two providers chose differently, and neither is the
``sha256=<hex>`` HMAC ``apps.channels.security.verify_signature_header`` covers.

--------------------------------------------------------------------------
Resend — Svix
--------------------------------------------------------------------------

An HMAC-SHA256 over ``{id}.{timestamp}.{raw body}`` keyed on a base64 secret the
operator pastes in, presented as ``svix-signature: v1,<base64> v1,<base64>``
(several, because Svix rotates by signing with the old and new secrets at once).
Raw body, before parsing — SPEC §19: "raw-body HMAC before parsing".

The timestamp is checked against a tolerance, which is what stops a captured
delivery being replayed forever. It is not the *only* replay defence — the event
log's unique constraint is — but that one's window is the log's retention, so
this narrows it to minutes for the provider that offers a timestamp.

--------------------------------------------------------------------------
SES — SNS, and the one place this file fetches a URL
--------------------------------------------------------------------------

SNS signs with RSA over a canonical string and points at the signing certificate
with ``SigningCertURL`` **in the payload** — that is, at a URL an attacker
supplies. Fetching it naively is a textbook SSRF, so two things guard it:

1. the URL must match :data:`CERT_URL_RE` exactly — an ``https`` URL on
   ``sns.<region>.amazonaws.com`` whose path is a
   ``SimpleNotificationService-*.pem`` and nothing else;
2. the fetch goes through ``apps.common.outbound.guarded_request``
   (SECURITY-BASELINE §6), which resolves DNS once, validates every resolved
   address before pinning the connection to it, re-validates redirects and caps
   the body — so even a matching hostname that resolves to link-local is refused.

Certificates are cached in-process by URL, keyed after the pattern check, so a
busy topic does not fetch one per notification.

``SubscriptionConfirmation`` is **not** confirmed by visiting ``SubscribeURL``,
which would be a second attacker-supplied URL for no gain: the same operation is
available as ``sns:ConfirmSubscription`` on the AWS API, authenticated with the
credentials the connection already stores. See ``providers/email.py``.
"""

import base64
import hashlib
import hmac
import logging
import re
import time
from typing import Any

from apps.channels import security
from apps.common.outbound import BlockedURLError, OutboundTransportError, guarded_request

logger = logging.getLogger(__name__)

__all__ = [
    "CERT_URL_RE",
    "SVIX_TOLERANCE_SECONDS",
    "clear_certificate_cache",
    "verify_resend",
    "verify_sns",
]

# ---------------------------------------------------------------------------
# Resend / Svix
# ---------------------------------------------------------------------------

#: How far out of step a delivery's timestamp may be. Svix's own recommendation
#: is five minutes, and it has to absorb clock skew in both directions.
SVIX_TOLERANCE_SECONDS = 5 * 60

#: The prefix Svix puts on a secret when it shows it to an operator. Stripped
#: before base64-decoding, because people paste what they are shown.
_SVIX_SECRET_PREFIX = "whsec_"  # noqa: S105 - the prefix of a secret, not a secret

#: Cap on the header, so a hostile delivery cannot make us split a megabyte.
_MAX_SIGNATURE_HEADER = 4096


def verify_resend(request: Any, raw_body: bytes, secret: str) -> bool:
    """True when this delivery carries a valid Svix signature.

    Every failure is one ``False``. The caller turns that into the same 403 an
    unknown connection gets, so nothing here distinguishes "no secret
    configured" from "wrong signature".
    """
    if not secret:
        return False
    message_id = request.headers.get("svix-id", "")
    timestamp = request.headers.get("svix-timestamp", "")
    presented = request.headers.get("svix-signature", "")[:_MAX_SIGNATURE_HEADER]
    if not message_id or not timestamp or not presented:
        return False
    if not _timestamp_fresh(timestamp):
        return False

    try:
        key = base64.b64decode(secret.removeprefix(_SVIX_SECRET_PREFIX), validate=True)
    except (ValueError, TypeError):
        return False
    if not key:
        return False

    signed = b".".join((message_id.encode("utf-8"), timestamp.encode("utf-8"), raw_body))
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")

    # Every candidate is compared, and the comparison is constant-time — no
    # early return on the first match, so the header's length is all a caller
    # learns from the timing.
    matched = False
    for candidate in presented.split(" "):
        version, _, value = candidate.partition(",")
        if version != "v1":
            continue
        matched |= security.constant_time_equal(value, expected)
    return matched


def _timestamp_fresh(raw: str) -> bool:
    try:
        sent_at = int(raw)
    except (TypeError, ValueError):
        return False
    return abs(time.time() - sent_at) <= SVIX_TOLERANCE_SECONDS


# ---------------------------------------------------------------------------
# SES / SNS
# ---------------------------------------------------------------------------

#: The only URLs a signing certificate may be fetched from. Anchored at both
#: ends, no dots allowed in the region label, and the filename fixed — so
#: ``https://sns.eu-west-1.amazonaws.com.evil.test/…`` and
#: ``https://sns.eu-west-1.amazonaws.com/../…`` both fail to match.
CERT_URL_RE = re.compile(r"^https://sns\.[a-z0-9-]+\.amazonaws\.com/SimpleNotificationService-[A-Za-z0-9]+\.pem$")

#: Fields signed, in order, per notification type. SNS specifies both the set and
#: the order; a field that is absent from the payload is skipped, which is how
#: ``Subject`` (optional) is handled.
_SIGNED_FIELDS: dict[str, tuple[str, ...]] = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": (
        "Message",
        "MessageId",
        "SubscribeURL",
        "Timestamp",
        "Token",
        "TopicArn",
        "Type",
    ),
}
_SIGNED_FIELDS["UnsubscribeConfirmation"] = _SIGNED_FIELDS["SubscriptionConfirmation"]

#: SignatureVersion -> hash. AWS moved to 2 (SHA-256) and still emits 1 (SHA-1)
#: on older topics, so both are accepted; anything else is refused rather than
#: guessed at.
_SIGNATURE_HASHES = {"1": "SHA1", "2": "SHA256"}

#: url -> public key. Bounded because the URL pattern bounds what can be a key:
#: one per region, and AWS rotates rarely.
_CERTIFICATE_CACHE: dict[str, Any] = {}
_MAX_CACHED_CERTIFICATES = 32


def clear_certificate_cache() -> None:
    """Empty the in-process certificate cache. For tests and for rotation."""
    _CERTIFICATE_CACHE.clear()


def verify_sns(payload: dict[str, Any]) -> bool:
    """True when ``payload`` carries a valid SNS signature.

    Defensive throughout: the payload is attacker-controlled until this returns
    True, so every read is type-checked and every failure is one ``False``.
    """
    if not isinstance(payload, dict):
        return False
    message_type = payload.get("Type")
    fields = _SIGNED_FIELDS.get(message_type) if isinstance(message_type, str) else None
    if fields is None:
        return False

    version = str(payload.get("SignatureVersion") or "")
    algorithm = _SIGNATURE_HASHES.get(version)
    if algorithm is None:
        return False

    cert_url = payload.get("SigningCertURL")
    if not isinstance(cert_url, str) or not CERT_URL_RE.match(cert_url):
        logger.warning("SNS delivery named a signing certificate URL outside the allowlist; refusing it.")
        return False

    raw_signature = payload.get("Signature")
    if not isinstance(raw_signature, str) or not raw_signature:
        return False
    try:
        signature = base64.b64decode(raw_signature, validate=True)
    except (ValueError, TypeError):
        return False

    canonical = _canonical_string(payload, fields)
    if canonical is None:
        return False

    public_key = _public_key(cert_url)
    if public_key is None:
        return False

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    if not isinstance(public_key, rsa.RSAPublicKey):
        return False
    digest = hashes.SHA1() if algorithm == "SHA1" else hashes.SHA256()  # noqa: S303 - AWS signs v1 topics with SHA-1
    try:
        public_key.verify(signature, canonical, padding.PKCS1v15(), digest)
    except InvalidSignature:
        return False
    except Exception:  # noqa: BLE001 - a malformed key must not 500 an unauthenticated route
        logger.exception("SNS signature verification raised; treating the delivery as unsigned.")
        return False
    return True


def _canonical_string(payload: dict[str, Any], fields: tuple[str, ...]) -> bytes | None:
    """SNS's signing string: ``name\\nvalue\\n`` per present field, in order."""
    parts: list[str] = []
    for name in fields:
        value = payload.get(name)
        if value is None:
            # Optional fields (Subject) are omitted from the signature rather
            # than signed as empty. A required one being absent means this is
            # not an SNS envelope at all.
            if name in {"Subject"}:
                continue
            return None
        if not isinstance(value, str):
            return None
        parts.append(f"{name}\n{value}\n")
    return "".join(parts).encode("utf-8")


def _public_key(cert_url: str) -> Any:
    """The certificate's public key, fetched once and cached. ``None`` on any failure."""
    cached = _CERTIFICATE_CACHE.get(cert_url)
    if cached is not None:
        return cached

    try:
        # The URL came out of an attacker-controlled payload, so even having
        # matched the allowlist above it goes through the SSRF guard: a
        # hostname AWS controls today could resolve anywhere tomorrow, and the
        # guard is what pins the resolved address and re-checks each redirect.
        response = guarded_request("GET", cert_url, timeout=5.0)
    except (BlockedURLError, OutboundTransportError):
        logger.warning("Could not fetch the SNS signing certificate; refusing the delivery.")
        return None
    if not response.ok:
        return None

    from cryptography import x509

    try:
        certificate = x509.load_pem_x509_certificate(response.content)
    except Exception:  # noqa: BLE001 - anything unparseable is simply not a certificate
        logger.warning("The SNS signing certificate did not parse; refusing the delivery.")
        return None

    key = certificate.public_key()
    if len(_CERTIFICATE_CACHE) >= _MAX_CACHED_CERTIFICATES:
        # The allowlist bounds this to one per region, so reaching the cap means
        # something unexpected. Clearing beats evicting arbitrarily.
        _CERTIFICATE_CACHE.clear()
    _CERTIFICATE_CACHE[cert_url] = key
    return key
