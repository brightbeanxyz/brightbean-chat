"""BaseModel behaviour."""

import uuid

import pytest

from tests.testapp.models import EncryptionProbe


@pytest.mark.django_db
class TestBaseModel:
    def test_primary_key_is_a_uuid7(self):
        probe = EncryptionProbe.objects.create(label="first")

        assert isinstance(probe.pk, uuid.UUID)
        assert probe.pk.version == 7

    def test_primary_keys_are_time_ordered(self):
        keys = [EncryptionProbe.objects.create(label=str(i)).pk for i in range(20)]

        assert keys == sorted(keys)

    def test_timestamps_are_set_on_create(self):
        probe = EncryptionProbe.objects.create(label="stamped")

        assert probe.created_at is not None
        assert probe.updated_at is not None

    def test_updated_at_advances_and_created_at_does_not(self):
        probe = EncryptionProbe.objects.create(label="original")
        created_at, first_update = probe.created_at, probe.updated_at

        probe.label = "changed"
        probe.save()
        probe.refresh_from_db()

        assert probe.created_at == created_at
        assert probe.updated_at > first_update

    def test_carries_no_tenancy_foreign_key(self):
        """Guards the Layer-0 scope line: tenancy is issue #31, not this one."""
        field_names = {field.name for field in EncryptionProbe._meta.get_fields()}

        assert {"workspace", "organization"} & field_names == set()
