"""Building a ``Content-Disposition`` a filename cannot break out of.

One function, and it lives here because two apps serve bytes under a filename
somebody else chose: :mod:`apps.media_library.delivery` for an uploaded asset
and :mod:`apps.channels.media` for a contact's inbound attachment. It used to
live in ``apps.media_library.storage``, which meant the channels app imported
the media library's django-storages layer — at ``ChannelsConfig.ready()`` time,
through the Telegram adapter — to reach a pure string function that touches no
storage backend at all.

SECURITY-BASELINE §9 is what the function is for. The disposition decides
whether a browser renders bytes or saves them, so it is the header that keeps a
stored-XSS payload inert, and a name that can inject a second header would undo
that from the inside.
"""

from urllib.parse import quote

__all__ = ["MAX_FILENAME_CHARS", "content_disposition"]

#: How much of a filename survives into the header. The name is attacker-chosen
#: on both call sites, and a header is not the place to discover how long a
#: string a stranger can make.
MAX_FILENAME_CHARS = 200


def content_disposition(*, inline: bool, filename: str) -> str:
    """A ``Content-Disposition`` value that a filename cannot break out of.

    Filenames reach two header-shaped places — a local response header and the
    ``ResponseContentDisposition`` parameter of a presigned S3 URL — and both
    are injection sinks for a name containing a quote or a newline. Control
    characters are dropped, the ASCII fallback is stripped to a conservative
    set, and the real name rides in the RFC 5987 ``filename*`` form where
    percent-encoding makes it inert.
    """
    safe = "".join(char for char in filename if char.isprintable() and char not in '"\\')[:MAX_FILENAME_CHARS]
    ascii_fallback = "".join(char if 32 <= ord(char) < 127 else "_" for char in safe) or "download"
    disposition = "inline" if inline else "attachment"
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(safe or 'download')}"
