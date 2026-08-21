"""Abstract models shared by every app in the project."""

from django.db import models

from apps.common.uuid7 import uuid7


class BaseModel(models.Model):
    """Abstract base for every model in BrightBean Chat.

    Gives every table a UUIDv7 primary key and created/updated timestamps.

    UUID primary keys keep internal row counts out of URLs and make
    cross-workspace id guessing useless; v7 specifically keeps them
    time-ordered, so inserts stay at the right-hand edge of the index instead
    of scattering the way v4 does.

    Studio has no equivalent module and repeats ``uuid.uuid4`` plus a pair of
    timestamp fields across ~40 models; every later issue here inherits from
    this instead. Deliberately carries **no** tenancy foreign key — the
    workspace-scoped base model and manager are issue #31's.
    """

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
