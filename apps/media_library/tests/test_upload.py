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


class TestStorageIsNeverLeftOrphaned:
    def _stored_files(self, settings):
        from pathlib import Path

        root = Path(settings.MEDIA_ROOT)
        return sorted(p.name for p in root.rglob("*") if p.is_file()) if root.exists() else []

    def test_a_failing_thumbnail_write_takes_the_original_with_it(self, workspace, settings, monkeypatch):
        """The thumbnail write used to sit outside the cleanup guard.

        A failure there propagated with the original already in storage and no
        row pointing at it, and this app has no orphan sweep to find it later.
        """
        from apps.media_library import services as services_module

        real_save = services_module.default_storage.save
        calls = {"n": 0}

        def fail_on_the_second_write(name, content):
            calls["n"] += 1
            if calls["n"] == 2:  # the thumbnail
                raise OSError("no space left on device")
            return real_save(name, content)

        monkeypatch.setattr(services_module.default_storage, "save", fail_on_the_second_write)

        with pytest.raises(OSError):
            _create(workspace, f.real_png(), name="logo.png")

        assert calls["n"] == 2, "the test must actually reach the thumbnail write"
        assert self._stored_files(settings) == []

    def test_a_failing_row_insert_removes_both_objects(self, workspace, settings, monkeypatch):
        def explode(*args, **kwargs):
            raise RuntimeError("database went away")

        monkeypatch.setattr("apps.media_library.models.MediaAsset.save", explode)

        with pytest.raises(RuntimeError):
            _create(workspace, f.real_png(), name="logo.png")

        assert self._stored_files(settings) == []


class TestBytesGoWhenTheRowGoes:
    """Storage cleanup hangs off post_delete, not off the service function.

    ``FileField`` has not deleted its own file since Django 1.3, and
    ``delete_asset`` only ever covered the one path that called it. Rows also
    vanish through cascades — ``Organization.hard_delete()`` reaches these
    through workspaces — and through the admin and any queryset ``.delete()``.

    The cleanup runs on ``transaction.on_commit``, so these tests go through
    ``django_capture_on_commit_callbacks``: pytest-django wraps each test in an
    atomic block it rolls back, and a callback registered there would otherwise
    never fire — the tests would pass while proving nothing.
    """

    def _files(self, settings):
        from pathlib import Path

        root = Path(settings.MEDIA_ROOT)
        return sorted(p.name for p in root.rglob("*") if p.is_file()) if root.exists() else []

    def test_the_service_path(self, workspace, settings, django_capture_on_commit_callbacks):
        from apps.media_library.services import delete_asset

        asset = _create(workspace, f.real_png(), name="logo.png")
        assert len(self._files(settings)) == 2  # original + thumbnail

        with django_capture_on_commit_callbacks(execute=True):
            delete_asset(asset)

        assert self._files(settings) == []

    def test_a_bare_row_delete(self, workspace, settings, django_capture_on_commit_callbacks):
        asset = _create(workspace, f.real_png(), name="logo.png")

        with django_capture_on_commit_callbacks(execute=True):
            asset.delete()

        assert self._files(settings) == []

    def test_a_queryset_delete(self, workspace, settings, django_capture_on_commit_callbacks):
        _create(workspace, f.real_png(), name="logo.png")

        with django_capture_on_commit_callbacks(execute=True):
            MediaAsset.objects.for_workspace(workspace).delete()

        assert self._files(settings) == []

    def test_a_cascade_from_the_workspace(self, tenancy, settings, django_capture_on_commit_callbacks):
        _create(tenancy.workspace, f.real_png(), name="logo.png")

        with django_capture_on_commit_callbacks(execute=True):
            tenancy.workspace.delete()

        assert self._files(settings) == []

    def test_a_cascade_from_the_organization(self, tenancy, settings, django_capture_on_commit_callbacks):
        """Organization.hard_delete reaches assets through its workspaces.

        On that path a leaked object is not a storage bill, it is user data
        surviving a deletion the product reported as done.
        """
        _create(tenancy.workspace, f.real_png(), name="logo.png")

        with django_capture_on_commit_callbacks(execute=True):
            tenancy.organization.delete()

        assert self._files(settings) == []

    def test_the_cleanup_is_deferred_to_commit(self, workspace, settings, django_capture_on_commit_callbacks):
        """Not merely "it deletes" — it must not delete before the row's removal
        is durable, or a rollback leaves a live row pointing at nothing."""
        asset = _create(workspace, f.real_png(), name="logo.png")

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            asset.delete()

            assert len(self._files(settings)) == 2, "bytes must survive until commit"

        assert len(callbacks) == 1, "the cleanup registered on_commit rather than running inline"

    def test_an_asset_with_no_stored_file_registers_nothing(self, workspace, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            MediaAsset.objects.create(workspace=workspace, filename="x", kind="image", mime="image/png").delete()

        assert callbacks == []

    def test_a_storage_failure_does_not_break_the_delete(
        self, workspace, monkeypatch, caplog, django_capture_on_commit_callbacks
    ):
        """The row is already gone, which is what revokes the delivery URLs."""
        import logging

        from apps.media_library import models as models_module

        asset = _create(workspace, f.real_png(), name="logo.png")

        def explode(name):
            raise OSError("bucket unreachable")

        monkeypatch.setattr(models_module.default_storage, "delete", explode)

        with (
            caplog.at_level(logging.WARNING, logger="apps.media_library.models"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            asset.delete()

        assert MediaAsset.objects.for_workspace(workspace).count() == 0
        assert "Could not delete stored media object" in caplog.text


class TestTheQuotaCountsEverythingStored:
    def test_a_thumbnail_is_charged_to_the_workspace(self, workspace):
        from apps.media_library.quotas import used_bytes

        asset = _create(workspace, f.real_png(), name="logo.png")

        assert asset.thumbnail_size > 0
        assert used_bytes(workspace) == asset.size + asset.thumbnail_size

    def test_a_kind_with_no_thumbnail_charges_only_its_own_bytes(self, workspace):
        from apps.media_library.quotas import used_bytes

        asset = _create(workspace, f.PDF, name="terms.pdf")

        assert asset.thumbnail_size == 0
        assert used_bytes(workspace) == asset.size

    def test_an_upload_whose_thumbnail_would_not_fit_is_refused(self, workspace, settings):
        """Counting thumbnails has to bind at the point of decision, not only in
        the number the usage bar renders."""
        payload = f.real_png()
        settings.MEDIA_WORKSPACE_QUOTA_BYTES = len(payload) + 1

        with pytest.raises(QuotaExceededError):
            _create(workspace, payload, name="logo.png")


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

    def test_a_one_byte_file_is_refused_rather_than_crashing(self, editor_client, workspace):
        """The sniffer's MP3 branch used to index past the end of the buffer."""
        response = editor_client.post(self._url(workspace), {"files": [f.upload(b"\xff", name="tiny.jpg")]})

        assert response.status_code == 400
        assert response.json()["errors"][0]["filename"] == "tiny.jpg"


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

    def test_the_refreshed_grid_keeps_the_filters_the_form_carried(self, editor_client, workspace):
        """The response reads request.POST, because request.GET is empty on a POST.

        Reading GET returned an unfiltered page-one grid, so uploading from
        inside a folder came back showing the whole library.
        """
        from apps.media_library.services import create_folder

        folder = create_folder(workspace=workspace, name="Brand")
        f.make_asset(workspace, filename="elsewhere.png")

        response = editor_client.post(
            self._url(workspace),
            {
                "files": [f.upload(f.real_png(), name="inside.png")],
                "folder": str(folder.pk),
                "kind": "image",
                "q": "inside",
            },
            headers={"HX-Request": "true"},
        )

        body = response.content.decode()
        assert "inside.png" in body
        assert "elsewhere.png" not in body

    def test_a_rejected_file_is_named_in_the_toast(self, editor_client, workspace):
        response = editor_client.post(
            self._url(workspace),
            {"files": [f.upload(f.HTML_AS_PNG, name="evil.png")]},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "evil.png" in response["HX-Trigger"]
        assert "warn" in response["HX-Trigger"]
