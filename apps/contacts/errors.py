"""The refusals this app raises.

Its own module because ``models.py`` needs to raise (the derived-workspace
guard) and ``services.py`` and ``admin.py`` need to catch: putting the
vocabulary in ``services.py`` would make ``models -> services -> models``
circular.

Every one is a ``ValueError`` subclass, for the same reason
:class:`apps.members.services.MembershipError` is one — views already catch
``ValueError`` and render the message inline with
``messages.error(request, str(exc))``, while a caller that cares can be
specific. Messages are written to be shown to an end user, and they name
*fields*, never values: a custom-field value is contact PII heading for a log
line and, once issue #25 lands, an API error body.
"""

__all__ = ["ContactsError", "FieldTypeError", "WorkspaceMismatchError"]


class ContactsError(ValueError):
    """Base for every refusal this app raises."""


class WorkspaceMismatchError(ContactsError):
    """Two rows that must share a tenant do not."""


class FieldTypeError(ContactsError):
    """A custom-field write whose value does not match the field's declared type."""
