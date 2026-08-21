"""Models shared by every app in the project."""

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


class RateLimitCounter(BaseModel):
    """One rate-limit counter, for one key, for one window.

    The algorithm — and the reasoning about why this is a table rather than a
    cache entry — lives in :mod:`apps.common.ratelimit`. The model is here
    because Django only discovers models declared in (or imported by)
    ``models.py``.
    """

    key = models.CharField(max_length=200, unique=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "common_rate_limit_counter"
        indexes = [models.Index(fields=["expires_at"], name="ratelimit_expires_idx")]

    def __str__(self) -> str:
        return f"{self.key} ({self.count})"
