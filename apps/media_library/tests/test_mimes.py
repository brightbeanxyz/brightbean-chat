"""The sniffer (SECURITY-BASELINE §9).

Everything downstream — the stored ``mime``, the ``kind``, the storage-key
extension, the inline/attachment decision — is derived from this module's
verdict. If it can be fooled, nothing after it helps.
"""

import pytest

from apps.media_library.mimes import (
    ALLOWED_MIMES,
    INLINE_SAFE_MIMES,
    MediaKind,
    UnsupportedMediaError,
    kind_for,
    sniff,
)
from apps.media_library.tests import factories as f


class TestRecognisesRealFiles:
    @pytest.mark.parametrize(
        ("content", "expected"),
        [
            (f.JPEG, "image/jpeg"),
            (f.PNG, "image/png"),
            (f.GIF, "image/gif"),
            (f.WEBP, "image/webp"),
            (f.WAV, "audio/wav"),
            (f.MP3, "audio/mpeg"),
            (f.MP3_FRAME, "audio/mpeg"),
            (f.OGG, "audio/ogg"),
            (f.MP4, "video/mp4"),
            (f.MOV, "video/quicktime"),
            (f.M4A, "audio/mp4"),
            (f.WEBM, "video/webm"),
            (f.PDF, "application/pdf"),
        ],
    )
    def test_every_allowed_signature(self, content, expected):
        assert sniff(f.upload(content)) == expected

    def test_riff_is_three_formats_and_byte_eight_decides(self):
        """WebP, WAV and AVI share a signature; only one of them is an image."""
        assert sniff(f.upload(f.WEBP)) == "image/webp"
        assert sniff(f.upload(f.WAV)) == "audio/wav"
        with pytest.raises(UnsupportedMediaError):
            sniff(f.upload(f.AVI))

    def test_ftyp_brand_separates_audio_from_video(self):
        """An M4A is audio. Sending it as video is what the platform rejects."""
        assert kind_for(sniff(f.upload(f.M4A))) == MediaKind.AUDIO
        assert kind_for(sniff(f.upload(f.MP4))) == MediaKind.VIDEO
        assert kind_for(sniff(f.upload(f.MOV))) == MediaKind.VIDEO


class TestRejectsHostileUploads:
    def test_html_disguised_as_png(self):
        """The stored-XSS payload the whole module exists for."""
        with pytest.raises(UnsupportedMediaError) as exc:
            sniff(f.upload(f.HTML_AS_PNG, name="logo.png", content_type="image/png"))
        assert "HTML" in str(exc.value)

    def test_svg_carrying_script(self):
        with pytest.raises(UnsupportedMediaError) as exc:
            sniff(f.upload(f.SVG_WITH_SCRIPT, name="icon.svg", content_type="image/svg+xml"))
        assert "SVG" in str(exc.value)

    def test_svg_is_not_in_the_allowlist_at_all(self):
        assert "image/svg+xml" not in ALLOWED_MIMES
        assert "text/html" not in ALLOWED_MIMES

    def test_zip_based_documents(self):
        with pytest.raises(UnsupportedMediaError) as exc:
            sniff(f.upload(f.ZIP, name="report.docx"))
        assert "Zip" in str(exc.value)

    def test_matroska_names_itself(self):
        """Same EBML signature as WebM — the DocType is the only difference."""
        with pytest.raises(UnsupportedMediaError) as exc:
            sniff(f.upload(f.MKV, name="clip.webm"))
        assert "Matroska" in str(exc.value)

    @pytest.mark.parametrize("payload", [b"\xff", b"\xfb", b"I", b"\x1a"])
    def test_a_single_byte_is_rejected_not_a_crash(self, payload):
        """Every branch has to survive a buffer shorter than it wants to read.

        ``head[1]`` in the MP3 frame-sync test used to run unguarded, so a
        one-byte file whose only byte was 0xFF raised IndexError and the upload
        endpoint answered 500.
        """
        with pytest.raises(UnsupportedMediaError):
            sniff(f.upload(payload))

    def test_empty_file(self):
        with pytest.raises(UnsupportedMediaError):
            sniff(f.upload(b""))

    def test_truncated_header(self):
        with pytest.raises(UnsupportedMediaError):
            sniff(f.upload(b"\x89PN"))

    def test_unrecognised_bytes(self):
        with pytest.raises(UnsupportedMediaError):
            sniff(f.upload(b"\x00\x01\x02\x03" * 16))

    def test_the_rejection_never_echoes_the_declared_type(self):
        """Repeating the client's header is misleading when it is the lie."""
        with pytest.raises(UnsupportedMediaError) as exc:
            sniff(f.upload(f.HTML_AS_PNG, name="a.png", content_type="image/png"))
        assert "image/png" not in str(exc.value)


class TestInlineSafety:
    def test_only_non_file_kinds_render_inline(self):
        assert "application/pdf" not in INLINE_SAFE_MIMES
        assert "image/png" in INLINE_SAFE_MIMES
        assert "video/mp4" in INLINE_SAFE_MIMES

    def test_every_inline_type_is_an_allowed_type(self):
        assert set(ALLOWED_MIMES) >= INLINE_SAFE_MIMES
