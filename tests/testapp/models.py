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


class QueueProbe(BaseModel):
    """One row per action the queue actually executed.

    The exactly-once assertion in ``apps/queueing/tests/test_concurrency.py``
    needs a side effect it can count, and counting in memory would not survive
    the threads that test runs. ``action_id`` is unique, so a second execution
    of one action is an ``IntegrityError`` at the moment it happens rather than
    a total that quietly reads 1001 at the end.
    """

    action_id = models.UUIDField(unique=True)
    worker = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        app_label = "testapp"
