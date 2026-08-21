"""Template context owned by the accounts app.

Kept out of ``apps/common/context_processors.py``: that file belongs to issue
#32 (the sidebar/shell context), and two branches writing the same new module is
the one merge conflict this split was arranged to avoid.
"""

from typing import Any

from django.conf import settings
from django.http import HttpRequest


def auth_providers(request: HttpRequest) -> dict[str, Any]:
    """Whether Google SSO is actually usable.

    allauth registers the provider whenever the app is in INSTALLED_APPS, so
    ``{% get_providers %}`` yields a Google button on a deployment that never
    set the credentials — and clicking it fails somewhere inside the OAuth
    redirect. Templates gate on this instead.
    """
    return {
        "google_configured": bool(
            getattr(settings, "GOOGLE_AUTH_CLIENT_ID", "") and getattr(settings, "GOOGLE_AUTH_CLIENT_SECRET", "")
        )
    }
