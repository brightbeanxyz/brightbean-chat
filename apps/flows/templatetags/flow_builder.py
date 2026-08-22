"""The one ``<script>`` tag for the React island, and the honest absence of it.

The bundle is a build artefact (``npm run build:js``) and gitignored, exactly
like the Tailwind stylesheet, so any tree that has not run the frontend build
has no bundle at all. A literal ``{% static 'flows/builder/builder.js' %}``
cannot express that:

* ``apps.common.tests.test_shell.TestStaticReferences`` walks every template for
  that pattern and requires a finder to resolve it, so the literal would turn
  "the island has not been built yet" into a failing test on a developer's
  machine; and
* under whitenoise's ``CompressedManifestStaticFilesStorage`` it turns the same
  absence into a 500, when issue #10 asks for the page to degrade to a clear
  error a self-hoster can act on.

So the lookup happens here, it answers ``None`` rather than raising, and
``templates/flows/edit.html`` decides what to render. The Dockerfile's ``test
-s`` guards and CI's manifest assertion are what keep a release build from
quietly shipping the degraded page.
"""

from typing import NamedTuple

from django import template
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import ManifestFilesMixin, staticfiles_storage

register = template.Library()

__all__ = ["BUNDLE_CSS", "BUNDLE_JS", "BuilderBundle", "flow_builder_bundle"]

#: Fixed names, because Django content-hashes at ``collectstatic``. Vite emits
#: no hash of its own — see frontend/builder/vite.config.mts for why a second,
#: nested hash would only make the filename unknowable to this module.
BUNDLE_JS = "flows/builder/builder.js"
BUNDLE_CSS = "flows/builder/builder.css"


class BuilderBundle(NamedTuple):
    """The island's two asset URLs."""

    js: str
    css: str


def _asset_url(path: str) -> str | None:
    """The static URL for ``path``, or ``None`` when it was never built."""
    try:
        url = staticfiles_storage.url(path)
    except ValueError:
        # ManifestFilesMixin.stored_name raises for a path absent from
        # staticfiles.json. That is production's signal for "never collected".
        return None

    if isinstance(staticfiles_storage, ManifestFilesMixin):
        return url

    # development.py and test.py both swap in plain StaticFilesStorage, whose
    # url() happily builds a URL for a file that does not exist. Ask a finder,
    # or every un-built tree would render a <script> pointing at a 404.
    return url if finders.find(path) is not None else None


@register.simple_tag
def flow_builder_bundle() -> BuilderBundle | None:
    """The island's asset URLs, or ``None`` when the bundle is not built.

    Both halves or neither: a stylesheet with no script paints an empty canvas
    frame and a script with no stylesheet renders React Flow unstyled, and both
    read as a bug rather than as a missing build step.
    """
    js = _asset_url(BUNDLE_JS)
    css = _asset_url(BUNDLE_CSS)
    if js is None or css is None:
        return None
    return BuilderBundle(js=js, css=css)
