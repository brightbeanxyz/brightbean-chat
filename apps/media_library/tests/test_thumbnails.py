"""Image thumbnails, and the pixel bomb they must not decode."""

import pytest

from apps.media_library.services import create_asset
from apps.media_library.tests import factories as f
from apps.media_library.thumbnails import make_thumbnail

pytestmark = pytest.mark.django_db


def _create(workspace, content, name):
    return create_asset(workspace=workspace, uploaded_file=f.upload(content, name=name), uploaded_by=None)


class TestImages:
    def test_an_image_gets_a_thumbnail_and_its_dimensions(self, workspace):
        asset = _create(workspace, f.real_png(64, 32), "logo.png")
        assert asset.thumbnail
        assert (asset.width, asset.height) == (64, 32)

    def test_the_thumbnail_is_stored_beside_the_asset_under_a_server_chosen_key(self, workspace):
        asset = _create(workspace, f.real_png(), "logo.png")
        assert asset.thumbnail.name == f"media/{workspace.pk}/thumbs/{asset.pk}.jpg"


class TestOtherKinds:
    @pytest.mark.parametrize(("content", "name"), [(f.MP3, "song.mp3"), (f.MP4, "clip.mp4"), (f.PDF, "terms.pdf")])
    def test_no_thumbnail_and_no_attempt_to_decode(self, workspace, content, name):
        asset = _create(workspace, content, name)
        assert not asset.thumbnail
        assert (asset.width, asset.height) == (0, 0)


class TestPixelBomb:
    def test_a_huge_declared_size_is_refused_before_decoding(self, workspace, settings):
        """A small file declaring enormous dimensions is a denial of service."""
        settings.MEDIA_MAX_IMAGE_PIXELS = 100
        result = make_thumbnail(f.upload(f.real_png(64, 32)))
        assert result.content is None
        # The dimensions are still read — they come from the header, not the pixels.
        assert (result.width, result.height) == (64, 32)

    def test_the_upload_still_succeeds_without_a_thumbnail(self, workspace, settings):
        """A thumbnail is a convenience; the asset is the product."""
        settings.MEDIA_MAX_IMAGE_PIXELS = 100
        asset = _create(workspace, f.real_png(64, 32), "big.png")
        assert asset.pk is not None
        assert not asset.thumbnail


class TestCorruptImages:
    def test_a_png_header_with_no_image_behind_it_does_not_fail_the_upload(self, workspace):
        """f.PNG is a valid signature followed by zeroes — Pillow cannot open it."""
        asset = _create(workspace, f.PNG, "truncated.png")
        assert asset.pk is not None
        assert not asset.thumbnail
