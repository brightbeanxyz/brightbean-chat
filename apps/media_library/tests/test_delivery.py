"""The public delivery route (SECURITY-BASELINE §9, §4).

This is the app's internet-facing surface: no session, no workspace, and a
signed token as the only credential. Two things have to hold — a token cannot be
forged or repurposed, and whatever comes back cannot execute in a browser.
"""

import pytest
from django.urls import reverse

from apps.common.signing import sign
from apps.media_library.delivery import PURPOSE, delivery_token, delivery_url
from apps.media_library.models import MediaAsset
from apps.media_library.services import create_asset, delete_asset
from apps.media_library.tests import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture
def asset(workspace):
    return create_asset(
        workspace=workspace,
        uploaded_file=f.upload(f.real_png(), name="logo.png"),
        uploaded_by=None,
    )


@pytest.fixture
def pdf_asset(workspace):
    return create_asset(
        workspace=workspace,
        uploaded_file=f.upload(f.PDF, name="terms.pdf"),
        uploaded_by=None,
    )


def _url(asset, thumbnail=False):
    return reverse("media_delivery", kwargs={"token": delivery_token(asset, thumbnail=thumbnail)})


class TestLocalDiskResponses:
    def test_an_image_is_served_inline_with_its_sniffed_type(self, client, asset):
        response = client.get(_url(asset))
        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert response["Content-Disposition"].startswith("inline")

    def test_a_pdf_is_served_as_an_attachment(self, client, pdf_asset):
        """Attachment is what makes a document harmless even without nosniff."""
        response = client.get(_url(pdf_asset))
        assert response["Content-Disposition"].startswith("attachment")

    def test_nosniff_is_always_present(self, client, asset, pdf_asset):
        for target in (asset, pdf_asset):
            assert client.get(_url(target))["X-Content-Type-Options"] == "nosniff"

    def test_no_session_is_needed(self, client, asset):
        """The fetcher is a messaging platform, not a signed-in browser."""
        assert client.get(_url(asset)).status_code == 200

    def test_a_thumbnail_is_served_as_a_jpeg(self, client, asset):
        response = client.get(_url(asset, thumbnail=True))
        assert response.status_code == 200
        assert response["Content-Type"] == "image/jpeg"

    def test_the_response_never_carries_a_session_cookie(self, client, asset):
        response = client.get(_url(asset))
        assert "sessionid" not in response.cookies


class TestFilenameCannotInjectAHeader:
    @pytest.mark.parametrize(
        "hostile",
        [
            'a.png"; attachment; filename="evil.html',
            "a.png\r\nX-Injected: yes",
            "a‮.png",
        ],
    )
    def test_hostile_filenames_are_neutralised(self, client, workspace, hostile):
        asset = create_asset(
            workspace=workspace,
            uploaded_file=f.upload(f.real_png(), name=hostile),
            uploaded_by=None,
        )
        response = client.get(_url(asset))
        disposition = response["Content-Disposition"]
        assert "\r" not in disposition and "\n" not in disposition
        assert disposition.count('"') == 2
        assert "X-Injected" not in response


class TestTokensCannotBeForgedOrRepurposed:
    def test_a_tampered_token_404s(self, client, asset):
        token = delivery_token(asset)
        assert client.get(reverse("media_delivery", kwargs={"token": token[:-4] + "AAAA"})).status_code == 404

    def test_a_token_minted_for_another_purpose_404s(self, client, asset):
        """Same SECRET_KEY, different salt — an unsubscribe token is not a key."""
        token = sign({"a": str(asset.pk)}, purpose="unsubscribe")
        assert client.get(reverse("media_delivery", kwargs={"token": token})).status_code == 404

    def test_an_unknown_payload_version_404s(self, client, asset):
        token = sign({"a": str(asset.pk)}, purpose=PURPOSE, version=99)
        assert client.get(reverse("media_delivery", kwargs={"token": token})).status_code == 404

    def test_a_malformed_token_404s(self, client):
        assert client.get(reverse("media_delivery", kwargs={"token": "not-a-token"})).status_code == 404

    def test_a_token_for_a_nonexistent_asset_404s(self, client, workspace):
        ghost = f.make_asset(workspace)
        token = delivery_token(ghost)
        MediaAsset.objects.for_workspace(workspace).filter(pk=ghost.pk).delete()
        assert client.get(reverse("media_delivery", kwargs={"token": token})).status_code == 404


class TestDeletionRevokesEveryUrl:
    def test_deleting_an_asset_404s_a_url_already_handed_out(self, client, asset):
        url = _url(asset)
        assert client.get(url).status_code == 200
        delete_asset(asset)
        assert client.get(url).status_code == 404


class TestS3Path:
    """The S3 backend, exercised through the storage seam with no bucket."""

    @pytest.fixture
    def recorded(self, monkeypatch):
        calls: dict = {}

        class FakeClient:
            def generate_presigned_url(self, operation, Params, ExpiresIn):  # noqa: N803 - boto3's own spelling
                calls["operation"] = operation
                calls["params"] = Params
                calls["expires"] = ExpiresIn
                return "https://bucket.example.test/signed?sig=abc"

        from apps.media_library import storage

        monkeypatch.setattr(storage, "is_s3_backend", lambda: True)
        monkeypatch.setattr(storage, "_client_and_bucket", lambda: (FakeClient(), "bucket"))
        monkeypatch.setattr(storage, "_normalize", lambda name: name)
        return calls

    def test_the_response_redirects_to_storage(self, client, asset, recorded):
        response = client.get(_url(asset))
        assert response.status_code == 302
        assert response["Location"].startswith("https://bucket.example.test/")

    def test_the_content_type_is_pinned_to_the_sniffed_mime(self, client, asset, recorded):
        client.get(_url(asset))
        assert recorded["params"]["ResponseContentType"] == "image/png"

    def test_the_disposition_is_pinned_too(self, client, pdf_asset, recorded):
        """S3 cannot return nosniff, so attachment carries the whole weight."""
        client.get(_url(pdf_asset))
        assert recorded["params"]["ResponseContentDisposition"].startswith("attachment")

    def test_a_custom_domain_without_a_signer_falls_back_to_proxying(self, client, asset, recorded, settings):
        """common.W001's failure mode: presigning is off, so a redirect would 403."""
        settings.AWS_S3_CUSTOM_DOMAIN = "cdn.example.test"
        settings.AWS_CLOUDFRONT_KEY_ID = ""
        settings.AWS_CLOUDFRONT_KEY = ""
        response = client.get(_url(asset))
        assert response.status_code == 200
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "params" not in recorded

    def test_a_custom_domain_with_a_signer_still_redirects(self, client, asset, recorded, settings):
        settings.AWS_S3_CUSTOM_DOMAIN = "cdn.example.test"
        settings.AWS_CLOUDFRONT_KEY_ID = "K123"
        settings.AWS_CLOUDFRONT_KEY = "-----BEGIN RSA PRIVATE KEY-----"
        assert client.get(_url(asset)).status_code == 302


class TestDeliveryUrl:
    def test_it_is_absolute_because_the_consumer_has_no_origin(self, asset, settings):
        settings.APP_URL = "https://chat.example.test"
        assert delivery_url(asset).startswith("https://chat.example.test/m/")

    def test_it_is_built_from_app_url_not_from_a_request(self, asset, settings):
        """The send path runs in a worker, where there is no request to read."""
        settings.APP_URL = "https://other.example.test/"
        assert delivery_url(asset).startswith("https://other.example.test/m/")
