"""How the builder page behaves when its bundle is, and is not, built.

The bundle is a gitignored build artefact, so "not built" is an ordinary state
of a working checkout — not an error case that only happens when something is
broken. Both branches are driven hermetically here rather than depending on
whether `npm run build:js` happened to have run.
"""

from unittest import mock

import pytest
from django.test import override_settings
from django.urls import reverse

from apps.flows.services import create_flow
from apps.flows.templatetags.flow_builder import BUNDLE_CSS, BUNDLE_JS, flow_builder_bundle

pytestmark = pytest.mark.django_db

MANIFEST_STORAGE = {"staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"}}


def edit_url(tenancy, flow):
    return reverse("flows:edit", kwargs={"workspace_id": tenancy.workspace.pk, "flow_id": flow.pk})


@pytest.fixture
def built_bundle(tmp_path):
    """A static root holding a plausible bundle, wired in as a finder root.

    django.test.signals clears the staticfiles finders' caches on a
    STATICFILES_DIRS change, so the override really takes. This makes the
    "bundle present" branch reachable on a machine that has never run
    `npm run build:js`; where one has, the real bundle answers instead and the
    assertions are the same either way.
    """
    directory = tmp_path / "flows" / "builder"
    directory.mkdir(parents=True)
    (directory / "builder.js").write_text("/* island */\n", encoding="utf-8")
    (directory / "builder.css").write_text("/* island */\n", encoding="utf-8")
    with override_settings(STATICFILES_DIRS=[str(tmp_path)]):
        yield tmp_path


class TestTheTag:
    def test_it_answers_the_two_urls_when_the_bundle_is_built(self, built_bundle):
        bundle = flow_builder_bundle()

        assert bundle is not None
        assert bundle.js.endswith(BUNDLE_JS)
        assert bundle.css.endswith(BUNDLE_CSS)
        assert bundle.js.startswith("/static/")

    def test_it_answers_none_when_the_bundle_was_never_built(self):
        with mock.patch("apps.flows.templatetags.flow_builder.finders.find", return_value=None):
            assert flow_builder_bundle() is None

    def test_half_a_bundle_is_no_bundle(self):
        """A stylesheet with no script paints an empty frame, which reads as a
        bug rather than as a missing build step.

        Patched rather than staged on disk: a developer who has run
        `make frontend` has a real bundle under apps/flows/static/, and the
        app-directories finder would answer from it whatever a temporary static
        root contains.
        """
        with mock.patch(
            "apps.flows.templatetags.flow_builder.finders.find",
            side_effect=lambda path: None if path.endswith(".js") else "/somewhere/builder.css",
        ):
            assert flow_builder_bundle() is None

    def test_manifest_storage_answers_none_instead_of_raising(self):
        """Production's storage raises ValueError for a path it never collected.
        Letting that reach the view would turn a missing build into a 500."""
        with override_settings(STORAGES=MANIFEST_STORAGE):
            assert flow_builder_bundle() is None


class TestThePage:
    def test_it_loads_the_island_when_the_bundle_is_built(self, tenancy, client_for, built_bundle):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome", user=tenancy.owner)

        body = client_for(tenancy.owner).get(edit_url(tenancy, flow)).content.decode()

        assert '<script type="module"' in body
        assert BUNDLE_JS in body
        assert BUNDLE_CSS in body
        assert "could not load" not in body

    def test_it_degrades_to_a_clear_error_when_the_bundle_is_missing(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome", user=tenancy.owner)

        with mock.patch("apps.flows.templatetags.flow_builder.finders.find", return_value=None):
            response = client_for(tenancy.owner).get(edit_url(tenancy, flow))
        body = response.content.decode()

        assert response.status_code == 200
        assert '<script type="module"' not in body
        assert "The flow builder could not load." in body
        assert "make frontend" in body
        # The mount div and its data attributes stay put either way, so nothing
        # about the contract with the island depends on the build having run.
        assert 'id="flow-builder"' in body

    def test_the_island_script_carries_the_csp_nonce(self, tenancy, client_for, built_bundle):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome", user=tenancy.owner)

        body = client_for(tenancy.owner).get(edit_url(tenancy, flow)).content.decode()

        assert 'type="module"' in body
        script = body[body.index('<script type="module"') :]
        assert "nonce=" in script[: script.index(">")]

    def test_the_page_hands_the_island_the_media_picker_url(self, tenancy, client_for):
        flow = create_flow(workspace=tenancy.workspace, name="Welcome", user=tenancy.owner)

        body = client_for(tenancy.owner).get(edit_url(tenancy, flow)).content.decode()

        picker = reverse("media:picker", kwargs={"workspace_id": tenancy.workspace.pk})
        assert f'data-media-picker-url="{picker}"' in body
