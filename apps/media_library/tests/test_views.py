"""The library UI: what renders, for whom, and what a mutation returns."""

import pytest
from django.urls import reverse

from apps.media_library.models import MediaAsset
from apps.media_library.services import create_folder
from apps.media_library.tests import factories as f

pytestmark = pytest.mark.django_db


def _library(workspace, **params):
    from urllib.parse import urlencode

    url = reverse("media:library", kwargs={"workspace_id": workspace.pk})
    return f"{url}?{urlencode(params)}" if params else url


class TestLibraryPage:
    @pytest.mark.parametrize("role", ["admin", "editor", "agent", "viewer"])
    def test_every_member_can_browse(self, tenancy, client_for, role):
        assert client_for(tenancy.user_for(role)).get(_library(tenancy.workspace)).status_code == 200

    def test_anonymous_visitors_are_sent_to_the_login_page(self, client, workspace):
        response = client.get(_library(workspace))
        assert response.status_code == 302
        assert "/accounts/login/" in response["Location"]

    def test_the_drop_zone_is_hidden_from_roles_that_cannot_upload(self, tenancy, client_for):
        editor = client_for(tenancy.user_for("editor")).get(_library(tenancy.workspace))
        agent = client_for(tenancy.user_for("agent")).get(_library(tenancy.workspace))
        assert "media-dropzone" in editor.content.decode()
        assert "media-dropzone" not in agent.content.decode()

    def test_an_htmx_request_returns_only_the_grid(self, editor_client, workspace):
        f.make_asset(workspace)
        response = editor_client.get(_library(workspace), headers={"HX-Request": "true"})
        body = response.content.decode()
        assert 'id="media-grid"' in body
        assert "<html" not in body

    def test_the_grid_shows_this_workspaces_assets_only(self, editor_client, tenancy, other_tenancy):
        f.make_asset(tenancy.workspace, filename="ours.png")
        f.make_asset(other_tenancy.workspace, filename="theirs.png")
        body = editor_client.get(_library(tenancy.workspace)).content.decode()
        assert "ours.png" in body
        assert "theirs.png" not in body

    def test_the_folder_rail_is_in_tree_order(self, editor_client, workspace):
        """Indentation without tree ordering reads as the wrong parentage."""
        top = create_folder(workspace=workspace, name="Brand")
        create_folder(workspace=workspace, name="Logos", parent=top)
        create_folder(workspace=workspace, name="Adverts")
        body = editor_client.get(_library(workspace)).content.decode()
        assert body.index("Adverts") < body.index("Brand") < body.index("Logos")

    def test_search_narrows_the_grid(self, editor_client, workspace):
        f.make_asset(workspace, filename="quarterly.png")
        f.make_asset(workspace, filename="unrelated.png")
        body = editor_client.get(_library(workspace, q="quarterly")).content.decode()
        assert "quarterly.png" in body
        assert "unrelated.png" not in body

    def test_an_empty_library_says_so_rather_than_rendering_nothing(self, editor_client, workspace):
        assert "No media here yet" in editor_client.get(_library(workspace)).content.decode()

    def test_a_folder_id_from_another_workspace_404s(self, editor_client, tenancy, other_tenancy):
        theirs = create_folder(workspace=other_tenancy.workspace, name="Theirs")
        assert editor_client.get(_library(tenancy.workspace, folder=str(theirs.pk))).status_code == 404


class TestDetailPanel:
    def _url(self, workspace, asset):
        return reverse("media:asset_detail", kwargs={"workspace_id": workspace.pk, "asset_id": asset.pk})

    def test_it_renders_the_media_id_an_operator_needs(self, editor_client, workspace):
        asset = f.make_asset(workspace)
        body = editor_client.get(self._url(workspace, asset)).content.decode()
        assert str(asset.pk) in body
        assert "/m/" in body

    def test_an_agent_sees_the_panel_without_the_edit_controls(self, agent_client, workspace):
        asset = f.make_asset(workspace)
        body = agent_client.get(self._url(workspace, asset)).content.decode()
        assert "Alt text" not in body
        assert asset.filename in body


class TestMutations:
    def test_editing_title_and_alt_text(self, editor_client, workspace):
        asset = f.make_asset(workspace)
        url = reverse("media:asset_edit", kwargs={"workspace_id": workspace.pk, "asset_id": asset.pk})
        response = editor_client.post(url, {"title": "Logo", "alt_text": "The company logo"})
        assert response.status_code == 204
        asset.refresh_from_db()
        assert (asset.title, asset.alt_text) == ("Logo", "The company logo")

    def test_moving_an_asset(self, editor_client, workspace):
        asset = f.make_asset(workspace)
        folder = create_folder(workspace=workspace, name="Brand")
        url = reverse("media:asset_move", kwargs={"workspace_id": workspace.pk, "asset_id": asset.pk})
        assert editor_client.post(url, {"folder": str(folder.pk)}).status_code == 204
        asset.refresh_from_db()
        assert asset.folder_id == folder.pk

    def test_deleting_an_asset(self, editor_client, workspace):
        asset = f.make_asset(workspace)
        url = reverse("media:asset_delete", kwargs={"workspace_id": workspace.pk, "asset_id": asset.pk})
        assert editor_client.post(url, headers={"HX-Request": "true"}).status_code == 204
        assert MediaAsset.objects.for_workspace(workspace).count() == 0

    @pytest.mark.parametrize("route", ["media:asset_edit", "media:asset_move", "media:asset_delete"])
    def test_roles_without_manage_media_are_refused(self, agent_client, workspace, route):
        asset = f.make_asset(workspace)
        url = reverse(route, kwargs={"workspace_id": workspace.pk, "asset_id": asset.pk})
        assert agent_client.post(url).status_code == 403

    def test_every_mutation_fires_the_grid_refresh_event(self, editor_client, workspace):
        asset = f.make_asset(workspace)
        url = reverse("media:asset_edit", kwargs={"workspace_id": workspace.pk, "asset_id": asset.pk})
        assert "mediaChanged" in editor_client.post(url, {"title": "x"})["HX-Trigger"]


class TestHtmxWiring:
    """Regressions found by driving the page in a browser, not by reading it."""

    def test_no_hx_post_form_falls_back_to_swapping_into_itself(self, editor_client, workspace):
        """htmx's default target is the element that made the request.

        Every hx-post form here must say where its response goes — an explicit
        hx-target for the ones that return markup, hx-swap="none" for the ones
        that answer 204 and carry their result in HX-Trigger. Left to the
        default, a 204 swaps nothing and looks fine, and then one day the
        response has a body — a session-expiry redirect to the login page — and
        the login form renders inside the sidebar. Seen in a browser exactly
        that way.
        """
        f.make_asset(workspace)
        pages = [
            editor_client.get(_library(workspace)).content.decode(),
            editor_client.get(
                reverse(
                    "media:asset_detail",
                    kwargs={"workspace_id": workspace.pk, "asset_id": f.make_asset(workspace).pk},
                )
            ).content.decode(),
        ]
        for body in pages:
            for form in body.split("<form")[1:]:
                head = form.split(">")[0]
                if "hx-post" not in head:
                    continue
                declares_destination = 'hx-swap="none"' in head or "hx-target=" in head
                assert declares_destination, f"hx-post form with no destination: {head[:120]}"

    def test_the_folder_rail_refreshes_itself_on_mediachanged(self, editor_client, workspace):
        """It lives outside #media-grid, so the grid's refresh does not cover it."""
        body = editor_client.get(_library(workspace)).content.decode()
        rail = body.split('id="media-folders"')[1].split(">")[0]
        assert "mediaChanged from:body" in rail

    def test_the_rail_fragment_is_served_by_the_same_endpoint(self, editor_client, workspace):
        create_folder(workspace=workspace, name="Brand")
        response = editor_client.get(_library(workspace, fragment="folders"), headers={"HX-Request": "true"})
        body = response.content.decode()
        assert 'id="media-folders"' in body
        assert "Brand" in body
        assert 'id="media-grid"' not in body


class TestRenderedUrls:
    """In-app markup uses relative delivery URLs; consumers get absolute ones."""

    def test_a_card_thumbnail_is_relative_to_this_origin(self, editor_client, workspace, settings):
        settings.APP_URL = "https://configured-elsewhere.example"
        f.make_asset(workspace, thumbnail="media/test/thumb.jpg")
        body = editor_client.get(_library(workspace)).content.decode()
        assert 'src="/m/' in body
        # An absolute URL built from a stale APP_URL renders a broken image and,
        # off-origin, is blocked by the CSP's img-src 'self'.
        assert "configured-elsewhere" not in body

    def test_the_picker_still_hands_out_absolute_urls(self, editor_client, workspace, settings):
        settings.APP_URL = "https://chat.example.test"
        f.make_asset(workspace)
        url = reverse("media:picker", kwargs={"workspace_id": workspace.pk})
        assert editor_client.get(url).json()["results"][0]["url"].startswith("https://chat.example.test/m/")


class TestNavigation:
    def test_the_sidebar_links_to_the_library(self, editor_client, workspace):
        body = editor_client.get(_library(workspace)).content.decode()
        assert reverse("media:library", kwargs={"workspace_id": workspace.pk}) in body
