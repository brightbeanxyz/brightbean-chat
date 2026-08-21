"""Folders: nesting limits, name collisions, and what a delete does to contents."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.media_library.models import MAX_FOLDER_DEPTH, MediaAsset, MediaFolder
from apps.media_library.services import create_folder, delete_folder, move_asset, rename_folder
from apps.media_library.tests import factories as f

pytestmark = pytest.mark.django_db


class TestNesting:
    def test_folders_nest_up_to_the_cap(self, workspace):
        top = create_folder(workspace=workspace, name="Brand")
        middle = create_folder(workspace=workspace, name="Logos", parent=top)
        assert middle.depth == 1

    def test_the_cap_is_enforced(self, workspace):
        parent = None
        for level in range(MAX_FOLDER_DEPTH):
            parent = create_folder(workspace=workspace, name=f"L{level}", parent=parent)
        with pytest.raises(ValidationError) as exc:
            create_folder(workspace=workspace, name="too deep", parent=parent)
        assert "nested" in str(exc.value)


class TestNameCollisions:
    def test_two_siblings_cannot_share_a_name(self, workspace):
        top = create_folder(workspace=workspace, name="Brand")
        create_folder(workspace=workspace, name="Logos", parent=top)
        with pytest.raises(ValidationError):
            create_folder(workspace=workspace, name="Logos", parent=top)

    def test_two_root_folders_cannot_share_a_name_either(self, workspace):
        """NULLs never collide in SQL, so the root level needs its own constraint."""
        create_folder(workspace=workspace, name="Brand")
        with pytest.raises(ValidationError):
            create_folder(workspace=workspace, name="Brand")

    def test_different_parents_may_reuse_a_name(self, workspace):
        a = create_folder(workspace=workspace, name="Campaign A")
        b = create_folder(workspace=workspace, name="Campaign B")
        create_folder(workspace=workspace, name="Images", parent=a)
        create_folder(workspace=workspace, name="Images", parent=b)

    def test_different_workspaces_may_reuse_a_name(self, tenancy, other_tenancy):
        create_folder(workspace=tenancy.workspace, name="Brand")
        create_folder(workspace=other_tenancy.workspace, name="Brand")

    def test_renaming_onto_a_sibling_is_refused(self, workspace):
        create_folder(workspace=workspace, name="Brand")
        other = create_folder(workspace=workspace, name="Ads")
        with pytest.raises(ValidationError):
            rename_folder(other, "Brand")


class TestBreadthIsCapped:
    """Depth was capped from the start; breadth was not.

    Three surfaces render the whole folder set unpaginated — the picker payload,
    the move dropdown and the sidebar rail — so the limit belongs at creation,
    where one guard bounds all three.
    """

    def test_the_cap_is_enforced(self, workspace, settings):
        settings.MEDIA_MAX_FOLDERS_PER_WORKSPACE = 2
        create_folder(workspace=workspace, name="One")
        create_folder(workspace=workspace, name="Two")

        with pytest.raises(ValidationError) as exc:
            create_folder(workspace=workspace, name="Three")

        assert "maximum" in str(exc.value)

    def test_the_cap_is_per_workspace(self, tenancy, other_tenancy, settings):
        settings.MEDIA_MAX_FOLDERS_PER_WORKSPACE = 1
        create_folder(workspace=tenancy.workspace, name="Ours")

        create_folder(workspace=other_tenancy.workspace, name="Theirs")

    def test_deleting_one_makes_room_again(self, workspace, settings):
        settings.MEDIA_MAX_FOLDERS_PER_WORKSPACE = 1
        first = create_folder(workspace=workspace, name="One")
        delete_folder(first)

        create_folder(workspace=workspace, name="Two")


class TestDeletion:
    def test_contents_move_up_one_level_instead_of_being_destroyed(self, workspace):
        top = create_folder(workspace=workspace, name="Brand")
        middle = create_folder(workspace=workspace, name="Logos", parent=top)
        asset = f.make_asset(workspace, folder=middle)
        leaf = create_folder(workspace=workspace, name="Old", parent=middle)

        delete_folder(middle)

        asset.refresh_from_db()
        leaf.refresh_from_db()
        assert asset.folder_id == top.pk
        assert leaf.parent_id == top.pk

    def test_deleting_a_root_folder_leaves_its_assets_at_the_root(self, workspace):
        folder = create_folder(workspace=workspace, name="Brand")
        asset = f.make_asset(workspace, folder=folder)
        delete_folder(folder)
        asset.refresh_from_db()
        assert asset.folder_id is None
        assert MediaAsset.objects.for_workspace(workspace).count() == 1


class TestMovingAssets:
    def test_an_asset_moves_between_folders_and_back_to_the_root(self, workspace):
        folder = create_folder(workspace=workspace, name="Brand")
        asset = f.make_asset(workspace)
        assert move_asset(asset, folder).folder_id == folder.pk
        assert move_asset(asset, None).folder_id is None

    def test_an_asset_cannot_be_moved_into_another_workspaces_folder(self, tenancy, other_tenancy):
        asset = f.make_asset(tenancy.workspace)
        theirs = create_folder(workspace=other_tenancy.workspace, name="Theirs")
        with pytest.raises(ValidationError):
            move_asset(asset, theirs)


class TestEndpoints:
    def _create_url(self, workspace):
        return reverse("media:folder_create", kwargs={"workspace_id": workspace.pk})

    def test_an_editor_can_create_a_folder(self, editor_client, workspace):
        response = editor_client.post(self._create_url(workspace), {"name": "Brand"})
        assert response.status_code == 204
        assert MediaFolder.objects.for_workspace(workspace).count() == 1

    @pytest.mark.parametrize("role", ["agent", "viewer"])
    def test_roles_without_manage_media_cannot(self, tenancy, client_for, role):
        client = client_for(tenancy.user_for(role))
        assert client.post(self._create_url(tenancy.workspace), {"name": "Brand"}).status_code == 403

    def test_a_blank_name_is_refused_with_a_status_the_client_can_detect(self, editor_client, workspace):
        response = editor_client.post(self._create_url(workspace), {"name": "   "})

        assert response.status_code == 400
        assert MediaFolder.objects.for_workspace(workspace).count() == 0

    def test_a_duplicate_name_is_refused_with_a_readable_reason(self, editor_client, workspace):
        """400, not 204.

        A 204 made htmx report ``detail.successful``, so the new-folder form's
        reset-on-success guard fired on failure and no other caller could branch
        at all. The shell's toast host renders a short error body, so the reason
        still reaches the user.
        """
        create_folder(workspace=workspace, name="Brand")

        response = editor_client.post(self._create_url(workspace), {"name": "Brand"})

        assert response.status_code == 400
        body = response.content.decode()
        assert "already exists" in body
        # Django's default for a constraint violation names the constraint —
        # "Constraint “media_folder_unique_root_name” is violated." — which is a
        # database identifier shown to whoever typed the name.
        assert "Constraint" not in body
        assert "media_folder_unique" not in body
        assert len(body) < 300, "the toast host drops anything longer as boilerplate"

    def test_rename_and_delete_are_reachable_from_the_rail(self, editor_client, workspace):
        """Both endpoints were routed and gated from the start, and no template
        linked to either — so a mistyped folder name was permanent."""
        folder = create_folder(workspace=workspace, name="Brand")
        keys = {"workspace_id": workspace.pk, "folder_id": folder.pk}

        body = editor_client.get(reverse("media:library", kwargs={"workspace_id": workspace.pk})).content.decode()

        assert reverse("media:folder_rename", kwargs=keys) in body
        assert reverse("media:folder_delete", kwargs=keys) in body

    def test_the_rail_hides_those_controls_from_roles_that_cannot_manage(self, agent_client, workspace):
        folder = create_folder(workspace=workspace, name="Brand")
        keys = {"workspace_id": workspace.pk, "folder_id": folder.pk}

        body = agent_client.get(reverse("media:library", kwargs={"workspace_id": workspace.pk})).content.decode()

        assert "Brand" in body, "an Agent still browses folders"
        assert reverse("media:folder_rename", kwargs=keys) not in body
        assert reverse("media:folder_delete", kwargs=keys) not in body

    def test_renaming_through_the_endpoint(self, editor_client, workspace):
        folder = create_folder(workspace=workspace, name="Brand")
        url = reverse("media:folder_rename", kwargs={"workspace_id": workspace.pk, "folder_id": folder.pk})

        assert editor_client.post(url, {"name": "Brand assets"}).status_code == 204

        folder.refresh_from_db()
        assert folder.name == "Brand assets"

    def test_a_parent_from_another_workspace_404s(self, editor_client, workspace, other_tenancy):
        theirs = create_folder(workspace=other_tenancy.workspace, name="Theirs")
        response = editor_client.post(self._create_url(workspace), {"name": "Mine", "parent": str(theirs.pk)})
        assert response.status_code == 404
