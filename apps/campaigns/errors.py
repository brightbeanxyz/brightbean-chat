"""The refusals this app raises.

Its own module so ``services.py`` can raise and ``views.py`` can catch without
either importing the other's world, matching ``apps/contacts/errors.py``.

Every one is a ``ValueError`` subclass, for the reason that module gives: views
already catch ``ValueError`` and render the message inline, while a caller that
cares can be specific. Messages are written to be shown to an end user and name
*things*, never values.
"""

__all__ = ["CampaignsError", "SequenceNotRunnableError", "WorkspaceMismatchError"]


class CampaignsError(ValueError):
    """Base for every refusal this app raises."""


class WorkspaceMismatchError(CampaignsError):
    """Two rows that must share a tenant do not."""


class SequenceNotRunnableError(CampaignsError):
    """A sequence nobody can be enrolled in — archived, or with no steps."""
