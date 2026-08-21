"""The upload chokepoint: sniff, then size, then quota."""

import pytest

from apps.media_library.mimes import UnsupportedMediaError
from apps.media_library.models import MediaAsset
from apps.media_library.quotas import QuotaExceededError
from apps.media_library.services import create_asset
from apps.media_library.tests import factories as f

pytestmark = pytest.mark.django_db


def _create(workspace, content, **kwargs):
    return create_asset(
        workspace=workspace,
        uploaded_file=f.upload(content, **kwargs),
        uploaded_by=None,
    )


class TestAcceptsRealMedia:
    @pytest.mark.parametrize(
        ("content", "mime", "kind"),
        [
            (f.PNG, "image/png", "image"),
            (f.MP3, "audio/mpeg", "audio"),
            (f.MP4, "video/mp4", "video"),
            (f.PDF, "application/pdf", "file"),
        ],
    )
    def test_one_asset_per_kind(self, workspace, content, mime, kind):
        asset = _create(workspace, content)
        assert (asset.mime, asset.kind) == (mime, kind)
        assert asset.size == len(content)

    def test_the_stored_mime_comes_from_the_bytes_not_the_header(self, workspace):
        """The client says PNG and sends JPEG. The bytes win."""
        asset = _create(workspace, f.JPEG, name="lying.png", content_type="image/png")
        assert asset.mime == "image/jpeg"

    def test_the_storage_key_contains_no_part_of_the_uploaded_name(self, workspace):
        asset = _create(workspace, f.PNG, name="../../etc/passwd.png")
        assert "passwd" not in asset.file.name
        assert ".." not in asset.file.name
        assert asset.file.name == f"media/{workspace.pk}/{asset.pk}.png"

    def test_the_extension_comes_from_the_sniffed_type(self, workspace):
        """A JPEG named .png is stored as .jpg, so S3's own type guess agrees."""
        asset = _create(workspace, f.JPEG, name="lying.png")
        assert asset.file.name.endswith(".jpg")

    def test_the_display_filename_is_kept_but_stripped_of_path(self, workspace):
        asset = _create(workspace, f.PNG, name="holiday/photos/logo.png")
        assert asset.filename == "logo.png"


class TestRejectsHostileUploads:
    def test_html_disguised_as_png(self, workspace):
        with pytest.raises(UnsupportedMediaError):
            _create(workspace, f.HTML_AS_PNG, name="logo.png", content_type="image/png")
        assert MediaAsset.objects.for_workspace(workspace).count() == 0

    def test_svg_carrying_script(self, workspace):
        with pytest.raises(UnsupportedMediaError):
            _create(workspace, f.SVG_WITH_SCRIPT, name="icon.svg", content_type="image/svg+xml")

    def test_a_rejected_upload_leaves_nothing_in_storage(self, workspace, settings):
        from pathlib import Path

        with pytest.raises(UnsupportedMediaError):
            _create(workspace, f.HTML_AS_PNG, name="logo.png")
        root = Path(settings.MEDIA_ROOT)
        assert not root.exists() or not list(root.rglob("*.*"))


class TestSizeLimits:
    def test_per_kind_cap(self, workspace, settings):
        settings.MEDIA_MAX_UPLOAD_BYTES_IMAGE = 10
        with pytest.raises(QuotaExceededError) as exc:
            _create(workspace, f.PNG)
        assert "image" in str(exc.value)

    def test_a_file_over_every_cap_is_refused_before_it_is_read(self, workspace, settings):
        """The cheap reject: nothing sniffs a file no kind could accept."""
        for name in (
            "MEDIA_MAX_UPLOAD_BYTES_IMAGE",
            "MEDIA_MAX_UPLOAD_BYTES_AUDIO",
            "MEDIA_MAX_UPLOAD_BYTES_VIDEO",
            "MEDIA_MAX_UPLOAD_BYTES_FILE",
        ):
            setattr(settings, name, 4)
        # Deliberately unrecognisable bytes: if this raised the sniffer's error
        # rather than the quota's, the size check would not have run first.
        with pytest.raises(QuotaExceededError):
            _create(workspace, b"\x00\x01\x02\x03" * 16)

    def test_workspace_quota(self, workspace, settings):
        settings.MEDIA_WORKSPACE_QUOTA_BYTES = len(f.PNG) + 1
        _create(workspace, f.PNG)
        with pytest.raises(QuotaExceededError) as exc:
            _create(workspace, f.PNG)
        assert "allowance" in str(exc.value)

    def test_the_quota_is_counted_per_workspace_not_per_organization(self, tenancy, other_tenancy, settings, workspace):
        settings.MEDIA_WORKSPACE_QUOTA_BYTES = len(f.PNG)
        _create(workspace, f.PNG)
        # A different tenant is unaffected by this workspace being full.
        _create(other_tenancy.workspace, f.PNG)

    def test_a_quota_rejection_leaves_no_orphan(self, workspace, settings):
        from pathlib import Path

        settings.MEDIA_WORKSPACE_QUOTA_BYTES = 1
        with pytest.raises(QuotaExceededError):
            _create(workspace, f.PNG)
        root = Path(settings.MEDIA_ROOT)
        assert not root.exists() or not list(root.rglob("*.png"))


class TestUploadEndpoint:
    def _url(self, workspace):
        from django.urls import reverse

        return reverse("media:upload", kwargs={"workspace_id": workspace.pk})

    def test_editor_can_upload(self, editor_client, workspace, png_upload):
        response = editor_client.post(self._url(workspace), {"files": [png_upload]})
        assert response.status_code == 200
        assert MediaAsset.objects.for_workspace(workspace).count() == 1

    @pytest.mark.parametrize("role", ["agent", "viewer"])
    def test_roles_without_manage_media_are_refused(self, tenancy, client_for, role, png_upload):
        client = client_for(tenancy.user_for(role))
        response = client.post(self._url(tenancy.workspace), {"files": [png_upload]})
        assert response.status_code == 403
        assert MediaAsset.objects.for_workspace(tenancy.workspace).count() == 0

    def test_a_batch_reports_each_file_separately(self, editor_client, workspace):
        response = editor_client.post(
            self._url(workspace),
            {"files": [f.upload(f.real_png(), name="ok.png"), f.upload(f.HTML_AS_PNG, name="evil.png")]},
        )
        body = response.json()
        assert len(body["uploaded"]) == 1
        assert len(body["errors"]) == 1
        assert body["errors"][0]["filename"] == "evil.png"

    def test_the_batch_size_is_capped(self, editor_client, workspace, settings):
        settings.MEDIA_MAX_FILES_PER_UPLOAD = 2
        response = editor_client.post(
            self._url(workspace),
            {"files": [f.upload(f.real_png(), name=f"{i}.png") for i in range(3)]},
        )
        assert response.status_code == 400
        assert MediaAsset.objects.for_workspace(workspace).count() == 0

    def test_no_files_is_a_400_not_a_crash(self, editor_client, workspace):
        assert editor_client.post(self._url(workspace)).status_code == 400


class TestHtmxUpload:
    """The path the drop zone actually takes.

    `upload` used to re-render the grid by calling the `library` view, which is
    decorated `@require_GET` — so a POST answered 405 with an empty body. htmx
    does not swap a 405, so the upload worked and the page showed nothing.
    """

    def _url(self, workspace):
        from django.urls import reverse

        return reverse("media:upload", kwargs={"workspace_id": workspace.pk})

    def test_it_returns_the_refreshed_grid(self, editor_client, workspace):
        response = editor_client.post(
            self._url(workspace),
            {"files": [f.upload(f.real_png(), name="chart.png")]},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        body = response.content.decode()
        assert 'id="media-grid"' in body
        assert "chart.png" in body

    def test_a_success_carries_a_toast(self, editor_client, workspace):
        response = editor_client.post(
            self._url(workspace),
            {"files": [f.upload(f.real_png(), name="chart.png")]},
            headers={"HX-Request": "true"},
        )
        assert "showToast" in response["HX-Trigger"]
        assert "1 uploaded" in response["HX-Trigger"]

    def test_a_rejected_file_is_named_in_the_toast(self, editor_client, workspace):
        response = editor_client.post(
            self._url(workspace),
            {"files": [f.upload(f.HTML_AS_PNG, name="evil.png")]},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "evil.png" in response["HX-Trigger"]
        assert "warn" in response["HX-Trigger"]
