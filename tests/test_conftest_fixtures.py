"""``conftest.py``'s autouse fixtures actually run for a marker-only test.

pytest-django's ``django_db`` *marker* grants database access without ever
adding the string ``"db"`` to ``request.fixturenames`` — only requesting the
``db``/``transactional_db`` fixture by name does that. ``_reset_active_language``
used to gate its cleanup on ``"db" in request.fixturenames``, so a class marked
``@pytest.mark.django_db`` that only requests ``client`` (exactly the shape
``apps.accounts.tests.test_language_middleware`` uses) never got the reset,
and an activated language could leak into whichever test ran next in the same
process (Copilot review, PR #1).

Two classes rather than one test with a manual ``translation.activate()``:
the fixture runs between tests, not within one, so the leak this guards
against can only be demonstrated across a test boundary. Declaration order
matters here — pytest preserves it within a file (no ``pytest-randomly`` in
this project, and ``--dist loadfile`` keeps one file on one worker) — so
``TestLeaksALanguage`` running before ``TestSeesNoLeak`` is relied upon, not
assumed.
"""

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import translation


@pytest.mark.django_db
class TestLeaksALanguage:
    def test_activates_ru_through_client_only_with_no_explicit_db_fixture(self, client):
        user = get_user_model().objects.create_user(
            email="leaks-a-language@example.test", password="not-a-real-password", language="ru"
        )
        client.force_login(user)
        client.get(reverse("settings_preferences"))

        # Sanity check on this test's own premise: if this ever stops being
        # true, the leak this file guards against can no longer happen this
        # way and the test below would pass vacuously.
        assert translation.get_language() == "ru"


class TestSeesNoLeak:
    def test_the_previous_test_left_no_language_active(self):
        assert translation.get_language() != "ru"
