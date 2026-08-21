"""Template access to the signed delivery URL.

Templates cannot call :func:`apps.media_library.delivery.delivery_url`
themselves, and annotating every queryset in every view that renders a card
would be the kind of thing one surface forgets. A tag keeps it in one place, and
means a picker mounted by a later issue renders the same markup.
"""

from typing import Any

from django import template

from apps.media_library.delivery import delivery_url

register = template.Library()


@register.simple_tag
def media_url(asset: Any, *, thumbnail: bool = False) -> str:
    """The asset's signed delivery URL, **relative** to this origin.

    Relative rather than absolute, which is the opposite of what the picker
    payload and :func:`apps.media_library.resolution.resolve` hand out — and for
    the opposite reason. Those two serve consumers with no origin of their own
    (a platform fetching an image, an email body), so they need
    ``APP_URL``-based absolute URLs. A page rendered by this server already has
    an origin, and using it has two advantages: the URL matches the CSP's
    ``'self'`` for ``img-src`` whatever ``APP_URL`` says, and a deployment whose
    ``APP_URL`` is stale or wrong still renders its own library correctly
    instead of showing a grid of broken images.
    """
    return delivery_url(asset, thumbnail=thumbnail, absolute=False)
