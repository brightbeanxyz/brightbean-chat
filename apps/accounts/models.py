"""The custom user model.

Ported from BrightBean Studio's ``apps/accounts/models.py``, trimmed to what
this project uses. Dropped: the TOTP columns (Studio declares them but ships no
2FA flow), ``avatar`` (an ImageField would pull in Pillow for one decorative
field — see ``apps.workspaces.models``), ``tos_accepted_at`` and its middleware,
``OAuthConnection`` (which duplicates allauth's own ``SocialAccount`` table) and
the vestigial custom ``Session`` model (the project uses Django's DB-backed
sessions).

``email`` is the username field; there is no username column at all.
"""

from typing import Any, cast

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

from apps.common.uuid7 import uuid7


class UserManager(BaseUserManager):
    """Email-keyed user creation.

    ``password`` is optional because a social signup arrives without one; those
    accounts get an unusable password, which is what
    ``AbstractBaseUser.set_unusable_password`` is for.
    """

    def create_user(self, email: str, password: str | None = None, **extra_fields: Any) -> "User":
        if not email:
            raise ValueError("Email is required")
        # ``self.model`` is typed as the manager's generic parameter; the cast
        # is only for the type checker, which cannot see that this manager is
        # only ever attached to ``User``.
        user = cast("User", self.model(email=self.normalize_email(email), **extra_fields))
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str | None = None, **extra_fields: Any) -> "User":
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if not extra_fields["is_staff"] or not extra_fields["is_superuser"]:
            # Studio lets create_superuser(is_staff=False) through, which
            # produces an account that cannot reach the admin it was created for.
            raise ValueError("A superuser must have is_staff=True and is_superuser=True.")
        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    # Not BaseModel: AbstractBaseUser brings its own field set and this model
    # predates any tenancy, but the pk is the same UUIDv7 the rest of the
    # project uses.
    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    # Which workspace to land in. Deliberately a bare UUID and **not** a foreign
    # key: deleting a workspace must not cascade into user rows, and a stale
    # value is revalidated against a live membership on every use
    # (``RBACMiddleware``), so there is nothing for referential integrity to buy.
    last_workspace_id = models.UUIDField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # noqa: RUF012

    class Meta:
        db_table = "accounts_user"
        ordering = ["email"]

    def __str__(self) -> str:
        return self.email

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.email:
            return self.email.split("@")[0]
        return "User"
