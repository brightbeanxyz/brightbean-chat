"""The three ways an email leaves this deployment, behind one seam.

SPEC §6.7 is "BYO SMTP / Resend / SES", which is the one place in the product
where a single platform has three unrelated transports. The seam is drawn the
way ``apps/media_library/storage.py`` draws its boto3 seam, and for the same
reason: **module-level functions, with the third-party import deferred inside
the one function that needs it.**

So an SMTP-only deployment never loads boto3, a deployment with no SES
connection never pays for botocore's import, and the test suite exercises all
three with monkeypatches rather than a bucket, an API key or a mail server.

--------------------------------------------------------------------------
What each backend is handed
--------------------------------------------------------------------------

An :class:`Envelope` — already rendered, already sanitized, already carrying its
headers. Composition is the adapter's job (``providers/email.py``); this module
only knows how to put a finished message on a wire. That split is what keeps
"every email carries List-Unsubscribe" a property of one function rather than a
thing three transports each have to remember.

All three send the **same MIME document** where they can. SES takes raw MIME
outright; SMTP is Django's ``EmailMultiAlternatives``, which builds the same
thing; Resend takes a JSON body with an explicit ``headers`` map. The one thing
that must survive every path is the ``List-Unsubscribe`` pair, because a provider
that silently dropped it would make every send non-compliant with nothing
visible to say so — ``test_email_backends.py`` asserts it on all three.

--------------------------------------------------------------------------
Errors
--------------------------------------------------------------------------

Everything raises ``APIError`` / ``RateLimitError`` from
``providers/exceptions.py``, so ``apps.messaging.services._dispatch`` maps them
with the policy it already has: 4xx permanent, 5xx and timeouts retryable, 429
deferred with the provider's own ``retry_after``. Nothing here invents a retry,
a sleep or a status.

Messages never carry the provider's prose (SECURITY-BASELINE §5): an SMTP
rejection quotes the envelope, and an API error quotes the request that caused
it — including, on a bad-credential path, the credential.
"""

import logging
import smtplib
import socket
import ssl
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings

from apps.channels.providers.base import BACKGROUND_TIMEOUT, request_json
from apps.channels.providers.exceptions import APIError, RateLimitError
from apps.common.outbound import refusal_for, resolve_host

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROVIDER",
    "check_destination",
    "guard_boto_client",
    "resolved_destination",
    "Envelope",
    "PROVIDERS",
    "RESEND_API_ROOT",
    "credentials_of",
    "deliver",
    "new_message_id",
    "provider_for",
    "verify_credentials",
]

#: The provider values ``credentials["provider"]`` may hold. Mirrored by
#: ``apps.channels.views._email_provider``, which builds the webhook URL from
#: the same value, and by the ``<provider>`` segment of the webhook route.
PROVIDERS: tuple[str, ...] = ("smtp", "resend", "ses")

#: What a connection with no stored provider is. Matches
#: ``apps.channels.views.DEFAULT_EMAIL_PROVIDER``, which predates this module.
DEFAULT_PROVIDER = "smtp"

RESEND_API_ROOT = "https://api.resend.com"

#: SMTP is a conversation, not a request, and 2 seconds is not enough for one
#: over a TLS handshake to a third-party relay. The adapter runs in a worker.
SMTP_TIMEOUT = 30.0

#: Resend's API is a fixed host built from a constant here, so this is
#: ``request_json`` territory rather than the SSRF guard's (SECURITY-BASELINE
#: §6: "the sibling for URLs an adapter builds from constants and stored ids").
RESEND_TIMEOUT = BACKGROUND_TIMEOUT


@dataclass(frozen=True)
class Envelope:
    """One finished email, transport-agnostic.

    Every field is already rendered and already bounded by the adapter. A
    backend's only remaining job is to encode it.
    """

    to: str
    subject: str
    html: str
    text: str
    from_address: str
    from_name: str = ""
    #: ``List-Unsubscribe``, ``List-Unsubscribe-Post`` and anything else the
    #: adapter decided belongs on the wire. Header injection is impossible by
    #: construction — the adapter builds these, never a contact — but the values
    #: are still scrubbed of newlines by :func:`_header_safe` on the way out.
    headers: dict[str, str] = field(default_factory=dict)
    #: Our own ``Message-ID``. Minted before the send so it can be correlated
    #: with a bounce even on SMTP, which returns no id of its own.
    message_id: str = ""

    def sender(self) -> str:
        """The ``From`` header value, with a display name when there is one."""
        return formataddr((self.from_name, self.from_address)) if self.from_name else self.from_address


def credentials_of(connection: Any) -> dict[str, Any]:
    """The connection's decrypted credentials, or ``{}``.

    Decryption failure is reported and swallowed rather than raised, exactly as
    ``telegram.bot_token`` does: a connection whose ciphertext no longer opens
    is a connection that cannot send, and the caller's "no credentials" path is
    already the right one.
    """
    try:
        credentials: Any = connection.credentials or {}
    except ValueError:
        logger.error("Connection %s: credentials could not be decrypted.", connection.pk)
        return {}
    return credentials if isinstance(credentials, dict) else {}


def provider_for(connection: Any) -> str:
    """Which of :data:`PROVIDERS` this connection sends through.

    Falls back to :data:`DEFAULT_PROVIDER` for anything unrecognised, which is
    what makes the value safe to interpolate into the webhook URL — it can only
    ever be one of three literals from this module.
    """
    value = credentials_of(connection).get("provider")
    provider = str(value).strip().lower() if isinstance(value, str) else ""
    return provider if provider in PROVIDERS else DEFAULT_PROVIDER


def new_message_id(domain: str = "") -> str:
    """A fresh RFC 5322 ``Message-ID``, in the sending domain where we have one."""
    cleaned = _header_safe(domain).strip().lstrip("@")
    return make_msgid(domain=cleaned) if cleaned else make_msgid()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def deliver(connection: Any, envelope: Envelope) -> str:
    """Send ``envelope``. Returns the provider's message id, or ours.

    Raises ``APIError``/``RateLimitError``; never returns a failure. The message
    row's status is the send pipeline's to write, not this module's.
    """
    provider = provider_for(connection)
    if provider == "resend":
        return _deliver_resend(connection, envelope)
    if provider == "ses":
        return _deliver_ses(connection, envelope)
    return _deliver_smtp(connection, envelope)


def verify_credentials(connection: Any) -> None:
    """Prove the stored credentials work. Raises ``APIError`` when they do not.

    Called by the guided connect **before the connection row is written**, the
    same ordering ``views_telegram._connect`` uses so a bad credential leaves no
    trace, and again by the "send test email" action.

    Deliberately the cheapest call each provider offers rather than a send: an
    operator pasting the wrong key should not have to receive an email to find
    out, and SES in sandbox mode would refuse the send for an unrelated reason.
    """
    provider = provider_for(connection)
    if provider == "resend":
        _verify_resend(connection)
        return
    if provider == "ses":
        _verify_ses(connection)
        return
    _verify_smtp(connection)


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------


def _smtp_settings(connection: Any) -> dict[str, Any]:
    credentials = credentials_of(connection)
    port = credentials.get("port")
    try:
        port_number = int(port) if port is not None else 587
    except (TypeError, ValueError):
        port_number = 587
    security = str(credentials.get("security") or "starttls").lower()
    return {
        "host": str(credentials.get("host") or ""),
        "port": port_number,
        "username": str(credentials.get("username") or ""),
        "password": str(credentials.get("password") or ""),
        # Two mutually exclusive modes, because Django's backend treats both as
        # booleans and a connection with both set raises at use time rather than
        # at configure time.
        "use_tls": security == "starttls",
        "use_ssl": security == "ssl",
    }


#: Ports an SMTP host may listen on. A number outside this is not a port, and
#: `getaddrinfo` raises `OverflowError` rather than anything the connect view
#: catches — so an operator typo became a 500 instead of a field error.
MIN_PORT = 1
MAX_PORT = 65535


def check_destination(host: str, port: int) -> None:
    """Refuse an SMTP destination this deployment must not connect to.

    SECURITY-BASELINE §6 is written about URLs, but the reason it exists is a
    server opening a connection somewhere a *user* chose, and that is exactly
    what an operator-supplied SMTP host is. `manage_channels` is a workspace
    permission, not a deployment one, so on a multi-tenant install a workspace
    admin could otherwise point this at loopback, at the cloud metadata service,
    or at an internal relay, and have the connect step probe it for them.

    The classification is ``apps.common.outbound.refusal_for``, so the rules are
    the SSRF guard's and a category added there is denied here too. **Every**
    resolved address is checked, not just the first, so a hostname answering with
    one public and one private address is refused.

    The escape hatch is ``EMAIL_SMTP_ALLOW_INTERNAL``, and it is a *different*
    flag from the guard's ``EXTERNAL_REQUEST_ALLOW_PRIVATE`` on purpose. For HTTP,
    loopback is never a legitimate integration target. For SMTP it is one of the
    commonest setups there is — a local postfix, a relay sidecar — so a
    single-tenant deployment needs a way to say so, and a multi-tenant one needs
    the default to stay closed.

    Issue #92: this used to be a pre-flight check and nothing more, which left
    the window ``guarded_request`` closes for HTTP — between the lookup here and
    ``smtplib``'s own, the answer can change. :func:`resolved_destination` now
    returns the address it validated and :func:`smtp_connection` connects to
    *that*, so the check and the connect can no longer disagree.
    """
    resolved_destination(host, port)


def resolved_destination(host: str, port: int) -> str:
    """The literal address to connect to, or ``""`` when pinning is waived.

    Every rule :func:`check_destination` documents, plus the thing that makes
    pinning possible: the caller is handed back the address that passed, so the
    connection goes to a value that has already been checked rather than to
    whatever a second lookup returns.

    ``""`` means "connect by name": either the internal-SMTP escape hatch is on,
    or the host is already a literal. Both are cases where pinning would add
    nothing.
    """
    if not MIN_PORT <= port <= MAX_PORT:
        raise APIError(f"{port} is not a port number.", status_code=400, code="bad_port")
    if getattr(settings, "EMAIL_SMTP_ALLOW_INTERNAL", False):
        return ""
    return _checked_address(host, subject="That mail server")


def _checked_address(host: str, *, subject: str) -> str:
    """Validate every address ``host`` resolves to and return the first.

    The one place the non-HTTP egress paths classify an address, so SMTP (#92)
    and boto3 (#91) cannot drift apart or from the guard: the classification is
    ``apps.common.outbound.refusal_for``, and a category added there is denied
    on both.

    **Every** resolved address is checked, not just the one returned, so a
    hostname answering with one public and one private address is refused
    outright rather than pinned to whichever came first.
    """
    addresses = resolve_host(host)
    if not addresses:
        raise APIError(f"{subject}'s hostname does not resolve.", status_code=400, code="dns")
    for address in addresses:
        refusal = refusal_for(address)
        if refusal:
            # The address is deliberately not named: this message reaches a
            # workspace admin, and confirming which internal addresses exist is
            # the reconnaissance the check exists to prevent.
            raise APIError(
                f"{subject} resolves to {refusal}, which this deployment will not connect to.",
                status_code=400,
                code="blocked_host",
            )
    return str(addresses[0])


def smtp_connection(connection: Any) -> Any:
    """Django's SMTP backend, configured for this connection.

    **The documented test seam.** ``telegram._client`` is the equivalent for an
    HTTP adapter; this is the one for a mail one, and the tests monkeypatch it
    to point at a local dummy server or at ``locmem``. Nothing else in this
    module builds an SMTP connection.
    """
    from django.core.mail import get_connection

    # `config`, not `settings`: this module imports django.conf.settings, and a
    # local of that name would shadow it for anything added below.
    config = _smtp_settings(connection)
    if not config["host"]:
        raise APIError("This email connection has no SMTP host stored.")
    address = resolved_destination(config["host"], config["port"])
    backend = get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        fail_silently=False,
        timeout=SMTP_TIMEOUT,
        **config,
    )
    if address:
        # `connection_class` is set in SMTP EmailBackend.__init__ but is not on
        # BaseEmailBackend, which is what get_connection is typed as returning.
        backend.connection_class = _pinned_smtp_class(  # type: ignore[attr-defined]
            backend.connection_class,  # type: ignore[attr-defined]
            address,
        )
    return backend


def _pinned_smtp_class(base: type, address: str) -> type:
    """``base``, but connecting to ``address`` instead of re-resolving the name.

    Issue #92. Django's backend owns the socket, so the address
    :func:`resolved_destination` validated was previously discarded and
    ``smtplib`` looked the name up again — the DNS-rebinding window
    ``guarded_request`` closes for HTTP by connecting to the literal it checked.

    ``_get_socket`` is the seam, and it is the right one for all three security
    modes. ``SMTP_SSL._get_socket`` calls ``super()`` and then wraps with
    ``server_hostname=self._host``, and ``starttls()`` wraps with ``self._host``
    too — both the *name*, which is what certificate validation must see. So the
    name still governs TLS and EHLO while the connection goes to the address we
    checked, which is exactly the split the HTTP guard makes.
    """

    class PinnedSMTP(base):  # type: ignore[misc, valid-type]
        pinned_address = address

        def _get_socket(self, host: str, port: int, timeout: Any) -> Any:
            if timeout is not None and not timeout:
                # smtplib's own guard: 0 would mean a non-blocking socket.
                raise OSError("Non-blocking socket (timeout=0) is not supported")
            return socket.create_connection((self.pinned_address, port), timeout, getattr(self, "source_address", None))

    PinnedSMTP.__name__ = f"Pinned{base.__name__}"
    PinnedSMTP.__qualname__ = PinnedSMTP.__name__
    return PinnedSMTP


def _mime(envelope: Envelope) -> EmailMessage:
    """The multipart/alternative document, text part first (RFC 2046 §5.1.4).

    Order matters and is not cosmetic: a client picks the *last* part it can
    render, so text before HTML is what makes the HTML the one that shows.
    """
    message = EmailMessage()
    message["Subject"] = _header_safe(envelope.subject)
    message["From"] = _header_safe(envelope.sender())
    message["To"] = _header_safe(envelope.to)
    if envelope.message_id:
        message["Message-ID"] = _header_safe(envelope.message_id)
    for name, value in envelope.headers.items():
        message[_header_safe(name)] = _header_safe(value)
    message.set_content(envelope.text or " ")
    if envelope.html:
        message.add_alternative(envelope.html, subtype="html")
    return message


def _header_safe(value: Any) -> str:
    """A header value with everything that could start a new header removed.

    Belt and braces: the adapter composes every one of these from its own
    constants and from values the renderer already bounded, so nothing
    contact-supplied reaches a header. It costs one pass and removes the whole
    class, which is worth it on the one code path that writes SMTP headers.
    """
    return "".join(char for char in str(value) if char not in "\r\n\x00")


#: One open SMTP backend per worker thread, keyed by connection.
#:
#: Building a fresh one per message meant a TCP connect, a TLS handshake and an
#: AUTH round trip for **every** email — ten of each per second at the platform's
#: default rate, paid inside the worker slot the token bucket is holding, and
#: enough rapid reconnecting for some relays to start refusing. Django's SMTP
#: backend is happy to stay open across sends, so it is kept and reused.
#:
#: Thread-local rather than a shared pool because ``smtplib.SMTP`` is not
#: thread-safe, and each worker thread sends serially.
_SMTP_POOL = threading.local()


def _pool_key(connection: Any) -> tuple[Any, ...]:
    """What has to be equal for a cached backend to still be the right socket.

    The connection id is **not** enough. An operator editing the host, the port,
    the encryption or the username changes where this backend should be talking
    without changing which row it belongs to, and a pool keyed on the id alone
    would go on using the old socket until something else evicted it. The
    password is deliberately absent: it does not select a destination, and the
    key is held in memory for the life of the thread.
    """
    config = _smtp_settings(connection)
    return (
        str(connection.pk),
        config["host"],
        config["port"],
        config["use_tls"],
        config["use_ssl"],
        config["username"],
    )


def _pooled_smtp(connection: Any) -> Any:
    """This thread's open backend for ``connection``, opening or replacing it as needed."""
    key = _pool_key(connection)
    cached = getattr(_SMTP_POOL, "entry", None)
    if cached is not None:
        cached_key, backend = cached
        if cached_key == key and getattr(backend, "connection", None) is not None:
            return backend
        _close_pooled()
    backend = smtp_connection(connection)
    backend.open()
    _SMTP_POOL.entry = (key, backend)
    return backend


def _close_pooled() -> None:
    """Drop this thread's backend. Never raises: closing a dead socket is fine."""
    cached = getattr(_SMTP_POOL, "entry", None)
    _SMTP_POOL.entry = None
    if cached is None:
        return
    try:
        cached[1].close()
    except Exception:  # noqa: BLE001 - a connection being closed is already going away
        logger.debug("Closing a pooled SMTP connection raised; dropping it anyway.")


def _deliver_smtp(connection: Any, envelope: Envelope) -> str:
    """Hand the message to the configured relay. Returns our own Message-ID.

    SMTP has no id to give back — acceptance by the relay is the whole receipt,
    which is why SPEC §6.7 says the row goes to ``sent`` on SMTP accept. Our
    ``Message-ID`` is returned so the row still carries something an operator can
    grep a mail log for.

    The connection is pooled per thread (see :data:`_SMTP_POOL`). A relay that
    has silently dropped an idle connection is indistinguishable from one that
    is down until the write fails, so a **transport** failure is retried once on
    a fresh connection; a failure carrying an SMTP reply code is the relay
    talking and is never retried here — the send pipeline owns that decision.
    """
    try:
        return _send_smtp_once(connection, envelope, reuse=True)
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
        if _smtp_code(exc) is not None:
            raise _smtp_error(exc) from exc
        _close_pooled()
    try:
        return _send_smtp_once(connection, envelope, reuse=False)
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
        _close_pooled()
        raise _smtp_error(exc) from exc


def _smtp_error(exc: Exception) -> APIError:
    """Map an smtplib failure onto this project's error vocabulary."""
    code = _smtp_code(exc)
    if code is None:
        # A relay that could not be reached at all, or an error carrying no
        # reply code. Retryable: the pipeline treats a missing status as
        # transient.
        return APIError(f"The mail server could not be reached: {type(exc).__name__}")
    # SMTP's own permanent/transient split, which lines up exactly with the
    # HTTP one `apps.messaging.services._record_api_error` already applies:
    # 5xx is "do not try this again", 4xx is "try later". Mapped onto HTTP
    # status codes so the pipeline needs no SMTP knowledge at all — and note
    # the inversion, because SMTP numbers them the opposite way from HTTP.
    status = 400 if 500 <= code < 600 else 503
    return APIError("The mail server refused the message.", status_code=status, code=str(code))


def _send_smtp_once(connection: Any, envelope: Envelope, *, reuse: bool) -> str:
    from django.core.mail import EmailMultiAlternatives

    if not reuse:
        _close_pooled()
    backend = _pooled_smtp(connection)
    message = EmailMultiAlternatives(
        subject=_header_safe(envelope.subject),
        body=envelope.text or " ",
        from_email=_header_safe(envelope.sender()),
        to=[_header_safe(envelope.to)],
        connection=backend,
        headers={_header_safe(name): _header_safe(value) for name, value in _smtp_headers(envelope).items()},
    )
    if envelope.html:
        message.attach_alternative(envelope.html, "text/html")
    sent = message.send()
    if not sent:
        raise APIError("The mail server accepted no recipients.", status_code=400, code="no_recipients")
    return envelope.message_id


def _smtp_code(exc: Exception) -> int | None:
    """The SMTP reply code behind an exception, or ``None`` if it carries none.

    ``SMTPRecipientsRefused`` needs its own branch and it is the case that
    matters most: a rejected mailbox is the commonest permanent failure there
    is, and that class is **not** an ``SMTPResponseException`` — it carries a
    ``recipients`` dict of ``address -> (code, message)`` instead of an
    ``smtp_code``. Reading only ``smtp_code`` therefore classified every "no
    such mailbox" as a transient failure, and the send pipeline spent the full
    five-attempt backoff ladder rediscovering that the address is still wrong.
    """
    refused = getattr(exc, "recipients", None)
    if isinstance(refused, dict) and refused:
        first = next(iter(refused.values()))
        if isinstance(first, tuple) and first and isinstance(first[0], int):
            return first[0]
    code = getattr(exc, "smtp_code", None)
    return code if isinstance(code, int) and code > 0 else None


def _smtp_headers(envelope: Envelope) -> dict[str, str]:
    """``envelope.headers`` plus the Message-ID, which Django sets separately."""
    headers = dict(envelope.headers)
    if envelope.message_id:
        headers["Message-ID"] = envelope.message_id
    return headers


def _verify_smtp(connection: Any) -> None:
    """Open the connection and close it. An auth failure surfaces here."""
    backend = smtp_connection(connection)
    try:
        backend.open()
    except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
        raise APIError(f"Could not sign in to the mail server: {type(exc).__name__}") from exc
    finally:
        try:
            backend.close()
        except Exception:  # noqa: BLE001 - closing a failed connection is best effort
            logger.debug("Closing the SMTP probe connection raised; ignoring.")


# ---------------------------------------------------------------------------
# Resend
# ---------------------------------------------------------------------------


def _resend_key(connection: Any) -> str:
    key = credentials_of(connection).get("api_key")
    if not isinstance(key, str) or not key:
        raise APIError("This email connection has no Resend API key stored.")
    return key


def _deliver_resend(connection: Any, envelope: Envelope) -> str:
    body: dict[str, Any] = {
        "from": envelope.sender(),
        "to": [envelope.to],
        "subject": envelope.subject,
        "headers": {name: value for name, value in _smtp_headers(envelope).items()},
    }
    if envelope.html:
        body["html"] = envelope.html
    if envelope.text:
        body["text"] = envelope.text
    result = request_json(
        "POST",
        f"{RESEND_API_ROOT}/emails",
        json=body,
        headers=_resend_headers(connection),
        timeout=RESEND_TIMEOUT,
    )
    provider_id = result.get("id")
    return provider_id if isinstance(provider_id, str) else envelope.message_id


def _resend_headers(connection: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {_resend_key(connection)}", "Content-Type": "application/json"}


def _verify_resend(connection: Any) -> None:
    """List the account's domains — the cheapest authenticated Resend call."""
    request_json(
        "GET",
        f"{RESEND_API_ROOT}/domains",
        headers=_resend_headers(connection),
        timeout=RESEND_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# SES
# ---------------------------------------------------------------------------


def ses_client(connection: Any, service: str = "sesv2") -> Any:
    """A boto3 client for this connection's SES account.

    **The boto3 seam.** This function and nothing else in the app constructs
    one, and the import is deferred inside it so a deployment with no SES
    connection never loads botocore — the same arrangement, for the same reason,
    as ``apps.media_library.storage._client_and_bucket``.

    ``service`` is ``"sesv2"`` for sending and ``"sns"`` for confirming a bounce
    topic's subscription; both authenticate with the same stored key pair.
    """
    import boto3

    credentials = credentials_of(connection)
    key_id = str(credentials.get("access_key_id") or "")
    secret = str(credentials.get("secret_access_key") or "")
    region = str(credentials.get("region") or "")
    if not key_id or not secret or not region:
        raise APIError("This email connection has no complete set of SES credentials stored.")
    client = boto3.client(
        service,
        region_name=region,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
    )
    return guard_boto_client(client)


def guard_boto_client(client: Any) -> Any:
    """Register the address check on ``client`` and return it.

    Issue #91. SECURITY-BASELINE §6 says every server-initiated request goes
    through the guard "no exceptions", and ``tests/test_ssrf_call_sites.py``
    asserts that structurally — for httpx. boto3 passed neither door because it
    is not httpx: botocore owns its own transport, so nothing validated the host
    its endpoint was built from.

    ``before-send`` is botocore's last hook before the request goes on the wire,
    and a handler returning ``None`` lets it proceed. So the rules are
    :func:`_checked_address`'s, which are ``apps.common.outbound.refusal_for``'s
    — one classification for both non-HTTP paths and for the guard itself.

    **This validates; it does not pin.** botocore owns the connection pool and
    gives no address seam the way ``smtplib._get_socket`` does, so the residual
    rebinding window SMTP no longer has is still open here. That is the honest
    limit of option 1 in the issue, and it is recorded in
    ``docs/security-audit.md`` rather than left to be rediscovered. The endpoint
    host is also derived from an operator-supplied region rather than from
    contact input, and #93 constrains that region's shape, so what remains is
    narrow.
    """

    def _check_endpoint(request: Any = None, **_kwargs: Any) -> None:
        host = urlsplit(getattr(request, "url", "") or "").hostname or ""
        if not host:
            raise APIError("That AWS endpoint has no host.", status_code=400, code="blocked_host")
        _checked_address(host, subject="That AWS endpoint")
        return None

    client.meta.events.register("before-send.*", _check_endpoint)
    return client


def _deliver_ses(connection: Any, envelope: Envelope) -> str:
    """Send the raw MIME document, so our own headers reach the recipient.

    ``Raw`` rather than ``Simple``: the simple form takes a subject and two
    bodies and builds the MIME itself, which means it decides the headers — and
    ``List-Unsubscribe`` is not one it would keep. SPEC §6.7 makes that header
    mandatory on every email, so the document has to be ours.
    """
    raw = _mime(envelope).as_bytes()
    try:
        # Inside the try as well: building the client resolves credentials and
        # can itself fail on a DNS or configuration problem, and that is an SES
        # failure like any other rather than an unhandled exception on the send
        # path.
        client = ses_client(connection)
        result = client.send_email(Content={"Raw": {"Data": raw}})
    except APIError:
        raise
    except Exception as exc:  # noqa: BLE001 - botocore's exceptions are built at runtime
        raise _ses_error(exc) from exc
    provider_id = result.get("MessageId") if isinstance(result, dict) else None
    return provider_id if isinstance(provider_id, str) else envelope.message_id


def _verify_ses(connection: Any) -> None:
    """Read the account's sending status — cheap, and refuses a bad key pair."""
    try:
        client = ses_client(connection)
        client.get_account()
    except APIError:
        raise
    except Exception as exc:  # noqa: BLE001 - as above
        raise _ses_error(exc) from exc


def _ses_error(exc: Exception) -> APIError:
    """Map a botocore exception onto this project's error vocabulary.

    Read off the response dict rather than by exception class, because botocore
    builds its error classes at runtime and importing them here would undo the
    deferred-import seam this module exists to keep.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return APIError(f"SES could not be reached: {type(exc).__name__}")
    metadata = response.get("ResponseMetadata")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    code = str((response.get("Error") or {}).get("Code") or "")[:64]
    if status == 429 or code in {"Throttling", "TooManyRequestsException"}:
        return RateLimitError("SES is throttling this account.", code=code)
    if isinstance(status, int):
        return APIError("SES refused the message.", status_code=status, code=code)
    return APIError("SES refused the message.", code=code)
