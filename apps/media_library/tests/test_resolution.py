"""``resolve()`` — the send path's contract (SPEC §11.1, §9.5)."""

import pytest

from apps import media_library
from apps.media_library.resolution import MediaNotFoundError, resolve
from apps.media_library.services import delete_asset
from apps.media_library.tests import factories as f

pytestmark = pytest.mark.django_db


class TestHappyPath:
    def test_it_returns_the_documented_three_keys(self, workspace):
        asset = f.make_asset(workspace)
        assert set(resolve(asset.pk, workspace=workspace)) == {"url", "mime", "kind"}

    def test_the_values_come_from_the_asset(self, workspace):
        asset = f.make_asset(workspace, mime="video/mp4", kind="video")
        resolved = resolve(asset.pk, workspace=workspace)
        assert (resolved["mime"], resolved["kind"]) == ("video/mp4", "video")
        assert "/m/" in resolved["url"]

    def test_a_string_id_works_because_block_config_is_json(self, workspace):
        asset = f.make_asset(workspace)
        assert resolve(str(asset.pk), workspace=workspace)["mime"] == "image/png"

    def test_it_is_reachable_as_media_library_resolve(self, workspace):
        """The spelling issue #16 names, exported lazily to dodge the app registry."""
        asset = f.make_asset(workspace)
        assert media_library.resolve(asset.pk, workspace=workspace)["kind"] == "image"


class TestFailureIsOneException:
    def test_a_deleted_asset(self, workspace):
        asset = f.make_asset(workspace)
        delete_asset(asset)
        with pytest.raises(MediaNotFoundError):
            resolve(asset.pk, workspace=workspace)

    def test_another_workspaces_asset_is_indistinguishable_from_a_missing_one(self, tenancy, other_tenancy):
        theirs = f.make_asset(other_tenancy.workspace)
        with pytest.raises(MediaNotFoundError):
            resolve(theirs.pk, workspace=tenancy.workspace)

    def test_a_malformed_id_is_a_miss_not_a_crash(self, workspace):
        """Block config is user-authored; a typo must not 500 the worker."""
        with pytest.raises(MediaNotFoundError):
            resolve("not-a-uuid", workspace=workspace)

    def test_none_is_a_miss(self, workspace):
        with pytest.raises(MediaNotFoundError):
            resolve(None, workspace=workspace)

    def test_it_is_a_lookup_error_so_a_caller_can_catch_broadly(self, workspace):
        assert issubclass(MediaNotFoundError, LookupError)

    def test_the_workspace_keyword_is_required(self, workspace):
        """An id-only lookup on a tenant model is the hole scoping exists to close."""
        asset = f.make_asset(workspace)
        with pytest.raises(TypeError):
            resolve(asset.pk)
