"""The picker JSON contract.

Four later issues code against the shape documented in
``apps/media_library/picker.py``. These tests are what stops a field being
renamed out from under them.
"""

import pytest
from django.urls import reverse

from apps.media_library.picker import picker_payload
from apps.media_library.tests import factories as f

pytestmark = pytest.mark.django_db


def _url(workspace, **params):
    from urllib.parse import urlencode

    url = reverse("media:picker", kwargs={"workspace_id": workspace.pk})
    return f"{url}?{urlencode(params)}" if params else url


class TestDocumentedShape:
    def test_the_top_level_keys_are_exactly_the_documented_ones(self, editor_client, workspace):
        f.make_asset(workspace)
        body = editor_client.get(_url(workspace)).json()
        assert set(body) == {"results", "folders", "next_cursor"}

    def test_a_result_carries_every_documented_field(self, editor_client, workspace):
        f.make_asset(workspace, title="Logo", alt_text="The logo", width=800, height=600)
        result = editor_client.get(_url(workspace)).json()["results"][0]
        assert set(result) == {
            "id",
            "kind",
            "mime",
            "filename",
            "title",
            "alt_text",
            "size",
            "width",
            "height",
            "folder_id",
            "url",
            "thumbnail_url",
            "created_at",
            "platform_warnings",
        }

    def test_the_url_is_a_signed_delivery_url(self, editor_client, workspace):
        f.make_asset(workspace)
        assert "/m/" in editor_client.get(_url(workspace)).json()["results"][0]["url"]

    def test_thumbnail_url_is_null_when_there_is_no_thumbnail(self, editor_client, workspace):
        f.make_asset(workspace)
        assert editor_client.get(_url(workspace)).json()["results"][0]["thumbnail_url"] is None


class TestFilters:
    def test_search_covers_filename_title_and_alt_text(self, workspace):
        f.make_asset(workspace, filename="quarterly.png")
        f.make_asset(workspace, filename="a.png", title="Quarterly deck")
        f.make_asset(workspace, filename="b.png", alt_text="the quarterly chart")
        f.make_asset(workspace, filename="unrelated.png")
        results = picker_payload(workspace=workspace, term="quarterly")["results"]
        assert len(results) == 3

    def test_kind_filter(self, workspace):
        f.make_asset(workspace)
        f.make_asset(workspace, kind="video", mime="video/mp4", filename="clip.mp4")
        assert len(picker_payload(workspace=workspace, kind="video")["results"]) == 1

    def test_an_unknown_kind_is_ignored_rather_than_matching_nothing(self, workspace):
        f.make_asset(workspace)
        assert len(picker_payload(workspace=workspace, kind="banana")["results"]) == 1

    def test_root_folder_filter(self, workspace):
        from apps.media_library.services import create_folder

        folder = create_folder(workspace=workspace, name="Brand")
        f.make_asset(workspace, folder=folder)
        f.make_asset(workspace, filename="loose.png")
        assert len(picker_payload(workspace=workspace, folder="root")["results"]) == 1
        assert len(picker_payload(workspace=workspace, folder=str(folder.pk))["results"]) == 1

    def test_a_junk_folder_id_is_a_404_not_a_silent_empty_list(self, workspace):
        """The same answer the grid gives, and the same answer a foreign id gives.

        Silence would be worse than an error here: a stale folder id cached by a
        flow builder should say so rather than render an empty library.
        """
        from django.http import Http404

        f.make_asset(workspace, filename="loose.png")

        with pytest.raises(Http404):
            picker_payload(workspace=workspace, folder="not-a-uuid")

    def test_another_workspaces_folder_id_is_a_404_too(self, workspace, other_tenancy):
        from django.http import Http404

        from apps.media_library.services import create_folder

        theirs = create_folder(workspace=other_tenancy.workspace, name="Theirs")

        with pytest.raises(Http404):
            picker_payload(workspace=workspace, folder=str(theirs.pk))


class TestPagination:
    def test_the_cursor_walks_every_row_exactly_once(self, workspace):
        for i in range(7):
            f.make_asset(workspace, filename=f"{i}.png")

        seen, cursor = [], ""
        for _ in range(10):
            page = picker_payload(workspace=workspace, limit=3, cursor=cursor)
            seen.extend(r["id"] for r in page["results"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert len(seen) == 7
        assert len(set(seen)) == 7

    def test_a_malformed_cursor_restarts_instead_of_failing(self, workspace):
        f.make_asset(workspace)
        assert len(picker_payload(workspace=workspace, cursor="!!!not-base64!!!")["results"]) == 1

    def test_the_limit_is_clamped(self, workspace):
        from apps.media_library.picker import MAX_LIMIT

        assert picker_payload(workspace=workspace, limit=10_000)["results"] == []
        assert MAX_LIMIT < 10_000


class TestPlatformWarnings:
    def test_warnings_are_empty_without_a_platform(self, workspace):
        f.make_asset(workspace, kind="video", mime="video/mp4", size=100 * 1024 * 1024)
        assert picker_payload(workspace=workspace)["results"][0]["platform_warnings"] == []

    def test_an_oversized_asset_warns_for_the_named_platform(self, workspace):
        f.make_asset(workspace, kind="video", mime="video/mp4", size=100 * 1024 * 1024)
        warnings = picker_payload(workspace=workspace, platform="whatsapp")["results"][0]["platform_warnings"]
        assert warnings and "WhatsApp" in warnings[0]

    def test_a_platform_that_does_not_take_the_kind_says_so(self, workspace):
        f.make_asset(workspace, kind="file", mime="application/pdf", filename="terms.pdf")
        warnings = picker_payload(workspace=workspace, platform="sms")["results"][0]["platform_warnings"]
        assert warnings and "does not accept" in warnings[0]

    def test_warnings_never_filter_the_results(self, workspace):
        """The target platform is not fixed until send time; advice, not a gate."""
        f.make_asset(workspace, kind="video", mime="video/mp4", size=100 * 1024 * 1024)
        assert len(picker_payload(workspace=workspace, platform="whatsapp")["results"]) == 1

    def test_an_unknown_platform_is_silent(self, workspace):
        f.make_asset(workspace, size=100 * 1024 * 1024)
        assert picker_payload(workspace=workspace, platform="carrier-pigeon")["results"][0]["platform_warnings"] == []


class TestAccess:
    def test_an_agent_may_read_the_picker(self, agent_client, workspace):
        """Attaching existing media in the inbox (#24) is not a manage_media act."""
        f.make_asset(workspace)
        assert agent_client.get(_url(workspace)).status_code == 200

    def test_another_tenants_workspace_404s(self, client_for, tenancy, other_tenancy):
        outsider = client_for(other_tenancy.owner)
        assert outsider.get(_url(tenancy.workspace)).status_code == 404

    def test_only_this_workspaces_assets_are_listed(self, editor_client, tenancy, other_tenancy):
        f.make_asset(tenancy.workspace, filename="ours.png")
        f.make_asset(other_tenancy.workspace, filename="theirs.png")
        results = editor_client.get(_url(tenancy.workspace)).json()["results"]
        assert [r["filename"] for r in results] == ["ours.png"]
