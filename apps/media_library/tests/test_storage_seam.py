"""The S3 seam, exercised for real rather than monkeypatched away.

Every other S3 test replaces ``is_s3_backend``, ``_client_and_bucket`` and
``_normalize`` with stand-ins, which is what makes the delivery path testable
without a bucket — and also what left the seam's own code with no coverage at
all. Two of its three functions reach into django-storages internals
(``S3Storage._normalize_name``) and boto3 internals
(``storage.connection.meta.client``), and the neighbouring ``Storage._clean_name``
was already removed once in django-storages 1.14. A version bump could therefore
break every S3 delivery URL in production with the whole suite green.

So these tests assert against the real classes: that the attributes still exist,
and that ``_normalize`` really does apply the LOCATION prefix presign and the
stored object have to agree on.
"""

import pytest

from apps.media_library import storage


class TestTheApiWeDependOnStillExists:
    """A django-storages upgrade that moves these should fail here, loudly."""

    def test_s3_storage_still_has_normalize_name(self):
        from storages.backends.s3 import S3Storage

        assert hasattr(S3Storage, "_normalize_name")

    def test_clean_name_is_still_a_module_level_helper(self):
        """It moved off the class once already; _normalize imports it directly."""
        from storages.utils import clean_name

        assert callable(clean_name)

    def test_s3_storage_still_exposes_connection_and_bucket_name(self):
        """Both are instance attributes, so the check has to use an instance.

        ``bucket_name`` is assigned in __init__ from settings and is absent from
        the class, and ``connection`` is a lazy property — asking the class about
        either says nothing about what _client_and_bucket will find.
        """
        from storages.backends.s3 import S3Storage

        instance = S3Storage(bucket_name="test-bucket")

        assert instance.bucket_name == "test-bucket"
        assert isinstance(type(instance).connection, property), "connection must stay lazy, or importing it dials out"

    def test_the_module_path_is_the_one_is_s3_backend_matches_on(self):
        """is_s3_backend sniffs the module path to avoid importing boto3."""
        from storages.backends.s3 import S3Storage

        assert S3Storage.__module__.startswith("storages.backends.s3")


class TestNormalizeAgainstTheRealBackend:
    @pytest.fixture
    def s3_storage(self, monkeypatch):
        """A real S3Storage instance, never asked to talk to a bucket."""
        from storages.backends.s3 import S3Storage

        instance = S3Storage(bucket_name="test-bucket", location="uploads")
        monkeypatch.setattr(storage, "default_storage", instance)
        return instance

    def test_the_location_prefix_is_applied(self, s3_storage):
        """The prefix is exactly what makes presign and the stored object
        disagree when it is forgotten."""
        assert storage._normalize("media/ws/asset.png") == "uploads/media/ws/asset.png"

    def test_traversal_is_refused(self, s3_storage):
        from django.core.exceptions import SuspiciousOperation

        with pytest.raises(SuspiciousOperation):
            storage._normalize("../../../etc/passwd")

    def test_is_s3_backend_recognises_it(self, s3_storage):
        assert storage.is_s3_backend() is True

    def test_client_and_bucket_reads_the_documented_attributes(self, s3_storage, monkeypatch):
        """Exercises the boto3 attribute chain without a network call."""

        class FakeMeta:
            client = object()

        class FakeConnection:
            meta = FakeMeta()

        monkeypatch.setattr(type(s3_storage), "connection", property(lambda self: FakeConnection()))

        client, bucket = storage._client_and_bucket()

        assert client is FakeMeta.client
        assert bucket == "test-bucket"


class TestDetectionThroughDjangosLazyProxy:
    """The case the other S3 tests cannot see.

    They monkeypatch ``default_storage`` with a real ``S3Storage``, which is the
    only way to exercise the delivery path without a bucket — and it removes the
    ``LazyObject`` wrapper that production always has. ``type(default_storage)``
    answered ``DefaultStorage`` on every real deployment, so ``is_s3_backend()``
    reported False with S3 fully configured and the presigned redirect never
    ran. These configure the setting and inspect the proxy as it actually is.
    """

    @pytest.fixture
    def s3_configured(self, settings):
        """STORAGE_BACKEND=s3, through the real lazy proxy.

        ``STORAGES`` is what Django's storage handler reads; changing it emits
        setting_changed, which clears the cached backend, so ``default_storage``
        resolves afresh to S3Storage on next use.
        """
        settings.AWS_STORAGE_BUCKET_NAME = "test-bucket"
        settings.STORAGES = {
            **settings.STORAGES,
            "default": {"BACKEND": "storages.backends.s3.S3Storage"},
        }

    def test_the_proxy_is_still_a_lazy_object(self, s3_configured):
        """If Django ever stops wrapping it, this test is why the check changed."""
        from django.core.files.storage import default_storage
        from django.utils.functional import LazyObject

        assert isinstance(default_storage, LazyObject)

    def test_type_would_not_see_through_the_proxy(self, s3_configured):
        """The exact mistake, pinned: type() reports the wrapper, not the backend."""
        from django.core.files.storage import default_storage

        assert type(default_storage).__module__ == "django.core.files.storage"
        assert default_storage.__class__.__module__ == "storages.backends.s3"

    def test_s3_is_detected_when_configured(self, s3_configured):
        assert storage.is_s3_backend() is True

    def test_presigning_is_available_when_configured(self, s3_configured):
        assert storage.can_presign() is True

    def test_a_custom_domain_without_a_signer_still_disables_presigning(self, s3_configured, settings):
        settings.AWS_S3_CUSTOM_DOMAIN = "cdn.example.test"
        settings.AWS_CLOUDFRONT_KEY_ID = ""
        settings.AWS_CLOUDFRONT_KEY = ""

        assert storage.is_s3_backend() is True
        assert storage.can_presign() is False


class TestLocalBackendIsNotMistakenForS3:
    def test_is_s3_backend_is_false_on_local_disk(self):
        assert storage.is_s3_backend() is False

    def test_can_presign_is_false_on_local_disk(self):
        assert storage.can_presign() is False
