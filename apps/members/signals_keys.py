"""Session keys shared between the members and accounts apps.

Its own module so ``apps.members.views`` does not have to import
``apps.accounts.signals`` (which imports allauth and the members services), and
so the key has exactly one definition.
"""

PENDING_INVITE_SESSION_KEY = "pending_invite_token"
