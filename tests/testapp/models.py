"""Concrete models used only by the test suite.

``apps.common`` ships abstract models and custom fields; exercising them needs
a real table. This app is added to ``INSTALLED_APPS`` in
``config/settings/test.py`` only, and never ships.

No migrations: ``create_test_db`` runs ``migrate --run-syncdb``, which creates
tables for migration-less apps. That keeps the scaffold's promise of no
migrations beyond Django's own built-ins.
"""

from django.db import models

from apps.common.encryption import EncryptedJSONField, EncryptedTextField
from apps.common.models import BaseModel


class EncryptionProbe(BaseModel):
    """A BaseModel subclass carrying one field of each encrypted type."""

    label = models.CharField(max_length=100, blank=True)
    secret = EncryptedTextField(null=True, blank=True)
    payload = EncryptedJSONField(null=True, blank=True)

    class Meta:
        app_label = "testapp"
