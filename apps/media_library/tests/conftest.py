"""Fixtures for the media-library suite.

Uploads write real bytes, so every test gets its own MEDIA_ROOT. Without this
the suite would accumulate files under the repository's ``test_media/``
directory and — worse — one test's stored object would be visible to the next.
"""

from typing import Any

import pytest

from apps.media_library.tests import factories as f


@pytest.fixture(autouse=True)
def _isolated_media_root(settings: Any, tmp_path: Any) -> None:
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.STORAGE_BACKEND = "local"


@pytest.fixture
def workspace(tenancy: Any) -> Any:
    return tenancy.workspace


@pytest.fixture
def editor_client(tenancy: Any, client_for: Any) -> Any:
    """A client holding ``manage_media`` — Editor is the lowest role that does."""
    return client_for(tenancy.user_for("editor"))


@pytest.fixture
def agent_client(tenancy: Any, client_for: Any) -> Any:
    """A client that may read the library but not write to it."""
    return client_for(tenancy.user_for("agent"))


@pytest.fixture
def png_upload() -> Any:
    """A real PNG, declared as something else so nothing can trust the label."""
    return f.upload(f.real_png(), name="logo.png", content_type="application/octet-stream")
