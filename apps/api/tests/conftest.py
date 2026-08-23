"""Fixtures for the public API's tests.

Two things every test here needs and neither of which the root ``conftest.py``
can supply: a minted key with its plaintext (the plaintext exists nowhere after
issuance, so it has to be captured at creation), and a client that speaks HTTPS
— the auth path refuses a bearer over plain HTTP outside ``DEBUG``, and Django's
test client is plain HTTP unless told otherwise.
"""

from __future__ import annotations

from typing import Any

import pytest

from apps.api import keys as key_tokens
from apps.api.models import ApiKey, OutboundWebhook


def make_key(workspace: Any, *, scopes: tuple[str, ...] = ("read", "write"), name: str = "test key") -> tuple[Any, str]:
    """An ``ApiKey`` plus its plaintext.

    Built through the same token helper the issuance service uses, so the row
    satisfies every constraint a real key does. Bypasses
    ``services.issue_api_key`` on purpose: most tests here are about the *auth*
    path and should not also depend on the issuer's memberships being right.
    """
    minted = key_tokens.mint()
    api_key = ApiKey.objects.create(
        workspace=workspace,
        name=name,
        scopes=list(scopes),
        lookup_prefix=minted.lookup_prefix,
        token_digest=minted.token_digest,
    )
    return api_key, minted.plaintext


def bearer(plaintext: str) -> dict[str, Any]:
    """Header kwargs for the test client, HTTPS included.

    ``secure=True`` is not optional: ``ApiKeyAuth`` refuses a bearer that
    arrived over plain HTTP outside ``DEBUG``, because by then the credential
    has been logged by every hop that saw it.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {plaintext}", "secure": True}


@pytest.fixture
def api_key(db, tenancy):
    """A read+write key for the standard tenancy."""
    return make_key(tenancy.workspace)


@pytest.fixture
def auth(api_key):
    """Request kwargs that authenticate as ``api_key``."""
    return bearer(api_key[1])


@pytest.fixture
def webhook(db, tenancy):
    """An enabled endpoint subscribed to every event, with a readable secret."""
    endpoint = OutboundWebhook(
        workspace=tenancy.workspace,
        url="https://receiver.example.com/hooks",
        events=["contact.created", "contact.tag_added", "message.received", "execution.completed"],
    )
    endpoint.rotate_secret()
    endpoint.save()
    return endpoint
