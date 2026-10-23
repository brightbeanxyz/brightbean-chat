"""The workspace's Primary colour replaces the brand ramp on every page."""

import pytest

from apps.workspaces.models import Workspace


class TestPrimaryColorRgb:
    def test_a_hex_colour_becomes_its_channels(self):
        assert Workspace(primary_color="#4e5ab7").primary_color_rgb == "78, 90, 183"

    def test_no_colour_means_no_override(self):
        assert Workspace(primary_color="").primary_color_rgb == ""

    def test_anything_but_strict_hex_is_dropped(self):
        """The value is injected into a <style> block, so a bad one must
        produce nothing rather than reach the page."""
        assert Workspace(primary_color="red;}</style>").primary_color_rgb == ""


@pytest.mark.django_db
class TestThePageCarriesTheColour:
    def test_a_set_colour_overrides_the_brand_token(self, tenancy, client_for):
        tenancy.workspace.primary_color = "#4e5ab7"
        tenancy.workspace.save(update_fields=["primary_color"])

        response = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.pk}/", follow=True)

        assert b"--brand-500: #4e5ab7;" in response.content
        assert b"--brand-500-rgb: 78, 90, 183;" in response.content

    def test_no_colour_leaves_the_stylesheet_alone(self, tenancy, client_for):
        response = client_for(tenancy.owner).get(f"/w/{tenancy.workspace.pk}/", follow=True)

        assert b"--brand-500:" not in response.content
