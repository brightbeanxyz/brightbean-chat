"""Upload → folder move → search → pick → resolve, on both storage backends.

Issue #16's first acceptance criterion, end to end. Parametrised over local disk
and a mocked S3 so the two backends are proven to differ only where they are
meant to — where the bytes come back from.
"""

import pytest
from django.urls import reverse

from apps.media_library.delivery import read_token
from apps.media_library.picker import picker_payload
from apps.media_library.resolution import resolve
from apps.media_library.services import create_folder, move_asset
from apps.media_library.tests import factories as f

pytestmark = pytest.mark.django_db


def addressed(url: str) -> tuple[str, bool]:
    """What a delivery URL points at: ``(asset id, is thumbnail)``.

    Two delivery URLs for the same asset are *not* byte-identical. Every call to
    ``delivery_url`` mints a fresh token, and ``django.core.signing`` stamps each
    one with a second-resolution timestamp, so two calls that straddle a second
    boundary sign differently. Comparing the tokens directly is a coin flip on
    CI; comparing what they address is what the assertion actually means, and it
    checks the thumbnail flag as well.
    """
    return read_token(url.rstrip("/").rsplit("/", 1)[-1])


@pytest.fixture(params=["local", "s3"])
def backend(request, monkeypatch):
    """Either backend, behind the same seam production uses."""
    if request.param == "local":
        return request.param

    class FakeClient:
        def generate_presigned_url(self, operation, Params, ExpiresIn):  # noqa: N803 - boto3's own spelling
            return f"https://bucket.example.test/{Params['Key']}?sig=abc"

    from apps.media_library import storage

    monkeypatch.setattr(storage, "is_s3_backend", lambda: True)
    monkeypatch.setattr(storage, "_client_and_bucket", lambda: (FakeClient(), "bucket"))
    monkeypatch.setattr(storage, "_normalize", lambda name: name)
    return request.param


def test_the_full_round_trip(backend, editor_client, client, workspace):
    upload_url = reverse("media:upload", kwargs={"workspace_id": workspace.pk})

    # 1. Upload. The name and declared type are both wrong on purpose.
    response = editor_client.post(
        upload_url,
        {"files": [f.upload(f.real_png(40, 20), name="Quarterly Chart.png", content_type="text/html")]},
    )
    assert response.status_code == 200
    media_id = response.json()["uploaded"][0]

    # 2. Move it into a folder.
    from apps.media_library.models import MediaAsset

    asset = MediaAsset.objects.for_workspace(workspace).get(pk=media_id)
    folder = create_folder(workspace=workspace, name="Reports")
    move_asset(asset, folder)

    # 3. Search finds it by its display name, inside that folder.
    found = picker_payload(workspace=workspace, term="quarterly", folder=str(folder.pk))["results"]
    assert [r["id"] for r in found] == [media_id]

    # 4. The picker's payload is what a consumer stores and shows.
    picked = found[0]
    assert picked["mime"] == "image/png"  # sniffed, not the "text/html" claimed
    assert picked["kind"] == "image"
    assert picked["thumbnail_url"]

    # 5. resolve() gives the send path the same asset from the id alone.
    resolved = resolve(media_id, workspace=workspace)
    assert resolved["mime"] == "image/png"
    assert addressed(resolved["url"]) == addressed(picked["url"])

    # 6. And the URL serves the bytes, with no session anywhere in sight.
    delivery = client.get(resolved["url"].replace("http://localhost:8000", ""))
    if backend == "local":
        assert delivery.status_code == 200
        assert delivery["Content-Type"] == "image/png"
        assert delivery["X-Content-Type-Options"] == "nosniff"
    else:
        assert delivery.status_code == 302
        assert delivery["Location"].startswith("https://bucket.example.test/")
