"""Byte fixtures and object builders shared by the media-library tests.

Real magic bytes, not mocks. The sniffer's whole job is reading these, so a test
that hands it a fake signature proves nothing about the thing being defended.
"""

import io
from typing import Any

from django.core.files.uploadedfile import SimpleUploadedFile

# Minimal but genuine headers. Only the first 64 bytes are ever read.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 48
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 48
GIF = b"GIF89a" + b"\x00" * 58
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 44
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 44
AVI = b"RIFF\x24\x00\x00\x00AVI LIST" + b"\x00" * 44
MP3 = b"ID3\x03\x00\x00\x00" + b"\x00" * 57
MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 60
OGG = b"OggS\x00\x02" + b"\x00" * 58
MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 44
MOV = b"\x00\x00\x00\x20ftypqt  " + b"\x00" * 44
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 44
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 20 + b"\x42\x82\x84webm" + b"\x00" * 32
MKV = b"\x1aE\xdf\xa3" + b"\x00" * 20 + b"\x42\x82\x88matroska" + b"\x00" * 28
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 48

# The hostile set from the issue's acceptance criteria.
HTML_AS_PNG = b"<html><body><script>alert(1)</script></body></html>" + b" " * 20
SVG_WITH_SCRIPT = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
ZIP = b"PK\x03\x04\x14\x00" + b"\x00" * 58


def upload(content: bytes, name: str = "file.bin", content_type: str = "application/octet-stream") -> Any:
    """An uploaded file whose declared name and type are deliberately wrong.

    Callers pass a misleading ``name``/``content_type`` on purpose: nothing in
    this app is allowed to read either one.
    """
    uploaded = SimpleUploadedFile(name, content, content_type=content_type)
    uploaded.size = len(content)
    return uploaded


def real_png(width: int = 12, height: int = 8) -> bytes:
    """A PNG Pillow can actually decode, for the thumbnail tests."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 40, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


def make_asset(workspace: Any, **overrides: Any) -> Any:
    """An asset row without going through the upload pipeline."""
    from apps.media_library.models import MediaAsset

    fields: dict[str, Any] = {
        "workspace": workspace,
        "filename": "logo.png",
        "kind": "image",
        "mime": "image/png",
        "size": len(PNG),
        "file": "media/test/logo.png",
    }
    fields.update(overrides)
    return MediaAsset.objects.create(**fields)
