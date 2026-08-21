"""What an upload actually is, decided from its bytes (SECURITY-BASELINE §9).

"Content type determined by **sniffing**, not extension." Both of the values a
client supplies about a file — the multipart ``Content-Type`` header and the
filename's extension — are attacker-chosen and neither is consulted anywhere in
this app. :func:`sniff` reads the first :data:`HEAD_BYTES` and matches them
against an allowlist of magic-byte signatures; anything it does not recognise is
rejected outright rather than stored as "unknown".

That ordering is the whole defence. The attack it stops is stored XSS: upload an
HTML document called ``logo.png`` with ``Content-Type: image/png``, then get a
victim's browser to render it from the deployment's own origin. Studio's
sniffer already refuses that; this module adds the reasons — a rejection says
*why* — and the :data:`INLINE_SAFE_MIMES` set that decides which types the
delivery view is willing to serve with ``Content-Disposition: inline``.

The allowlist is deliberately narrow: images, audio, video and PDF. SVG is
absent on purpose (it is a script-carrying document that browsers render), and
so are the zip-based office formats. Widening the ``file`` kind is a separate
change with its own review, not a line appended here.
"""

import contextlib
from typing import Any, BinaryIO

from django.db import models

__all__ = [
    "ALLOWED_MIMES",
    "HEAD_BYTES",
    "INLINE_SAFE_MIMES",
    "MediaKind",
    "extension_for",
    "kind_for",
    "sniff",
]

#: How much of the file the signature table needs. 64 rather than Studio's 32:
#: the EBML DocType that separates WebM from Matroska sits past the 32-byte mark.
HEAD_BYTES = 64


class MediaKind(models.TextChoices):
    """The four block types SPEC §11.1 lets a ``send_message`` carry."""

    IMAGE = "image", "Image"
    AUDIO = "audio", "Audio"
    VIDEO = "video", "Video"
    FILE = "file", "File"


#: mime -> (kind, canonical extension). The extension is used for the
#: server-chosen storage key, never read back from one.
ALLOWED_MIMES: dict[str, tuple[str, str]] = {
    "image/jpeg": (MediaKind.IMAGE, "jpg"),
    "image/png": (MediaKind.IMAGE, "png"),
    "image/gif": (MediaKind.IMAGE, "gif"),
    "image/webp": (MediaKind.IMAGE, "webp"),
    "audio/mpeg": (MediaKind.AUDIO, "mp3"),
    "audio/ogg": (MediaKind.AUDIO, "ogg"),
    "audio/wav": (MediaKind.AUDIO, "wav"),
    "audio/mp4": (MediaKind.AUDIO, "m4a"),
    "video/mp4": (MediaKind.VIDEO, "mp4"),
    "video/quicktime": (MediaKind.VIDEO, "mov"),
    "video/webm": (MediaKind.VIDEO, "webm"),
    "application/pdf": (MediaKind.FILE, "pdf"),
}

#: Types the delivery view may serve ``inline``. Everything else — every
#: ``file``-kind asset — is served ``attachment``, which is what makes a
#: document harmless even in a browser that ignores ``nosniff``.
INLINE_SAFE_MIMES = frozenset(mime for mime, (kind, _) in ALLOWED_MIMES.items() if kind != MediaKind.FILE)


class UnsupportedMediaError(Exception):
    """An upload whose real content type is not in the allowlist.

    Carries a human-readable reason, because "unsupported file type" is a
    support ticket while "SVG images are not accepted" is a fix. The reason
    never echoes the client's declared content type: when the magic bytes
    disagree with the header, repeating the header is actively misleading.
    """


def kind_for(mime: str) -> str:
    return ALLOWED_MIMES[mime][0]


def extension_for(mime: str) -> str:
    return ALLOWED_MIMES[mime][1]


def _read_head(file_obj: BinaryIO | Any) -> bytes:
    """First :data:`HEAD_BYTES` of ``file_obj``, restoring the read position."""
    if not hasattr(file_obj, "read") or not hasattr(file_obj, "seek"):
        return b""
    try:
        file_obj.seek(0)
        head = file_obj.read(HEAD_BYTES)
    except (OSError, ValueError):
        return b""
    finally:
        with contextlib.suppress(OSError, ValueError):
            file_obj.seek(0)
    return bytes(head) if isinstance(head, bytes | bytearray) else b""


def _iso_bmff(head: bytes) -> str:
    """Split an ISO Base Media container by its brand.

    ``ftyp`` at offset 4 covers MP4, QuickTime and M4A alike, and the three are
    different *kinds* to this app — an M4A is audio, not video, and sending it
    as video is what the platform would reject. The brand at offset 8 is the
    only thing that tells them apart.
    """
    brand = head[8:12]
    if brand == b"qt  ":
        return "video/quicktime"
    if brand in (b"M4A ", b"M4B "):
        return "audio/mp4"
    return "video/mp4"


def _riff(head: bytes) -> str | None:
    """RIFF is three formats sharing one signature; byte 8 decides."""
    form = head[8:12]
    if form == b"WEBP":
        return "image/webp"
    if form == b"WAVE":
        return "audio/wav"
    return None  # AVI and the rest are not in the allowlist.


def _looks_like_markup(head: bytes) -> str | None:
    """Name the markup formats explicitly so the rejection is legible.

    These are the payloads that make the whole module necessary, so they get a
    reason rather than falling through to the generic "unrecognised" branch.
    """
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if b"<svg" in head[:HEAD_BYTES].lower():
        return "SVG images are not accepted: they are documents that can carry script."
    if stripped.startswith((b"<!doctype html", b"<html", b"<head", b"<script")):
        return "HTML is not accepted, whatever the file is named."
    if stripped.startswith((b"<?xml", b"<")):
        return "Markup documents are not accepted."
    return None


def sniff(file_obj: BinaryIO | Any) -> str:
    """Return the real MIME type of ``file_obj``, or raise.

    Raises :class:`UnsupportedMediaError` for anything outside
    :data:`ALLOWED_MIMES`. There is no "unknown" return value on purpose: a
    caller that receives one has to decide what to do with it, and the safe
    decision is the only one this app ever wants.
    """
    head = _read_head(file_obj)
    if not head:
        raise UnsupportedMediaError("The file is empty or could not be read.")

    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF"):
        riff = _riff(head)
        if riff:
            return riff
    if head[4:8] == b"ftyp":
        return _iso_bmff(head)
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head.startswith(b"\x1aE\xdf\xa3"):
        # EBML. WebM and Matroska share the signature and differ only in the
        # DocType string, which sits a few dozen bytes in.
        if b"webm" in head:
            return "video/webm"
        raise UnsupportedMediaError("Matroska (.mkv) video is not accepted; convert it to MP4 or WebM.")
    if head.startswith(b"ID3") or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        raise UnsupportedMediaError("Zip-based files (including .docx, .xlsx and .zip) are not accepted.")

    markup = _looks_like_markup(head)
    if markup:
        raise UnsupportedMediaError(markup)

    raise UnsupportedMediaError("Unrecognised file type. Accepted: images, audio, video and PDF.")


def accepted_upload_types() -> str:
    """The ``accept`` attribute for the file input, from the same table."""
    return ",".join(sorted(ALLOWED_MIMES))
