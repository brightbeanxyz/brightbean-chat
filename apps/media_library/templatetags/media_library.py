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
    """The asset's public, signed delivery URL.

    Absolute, because the same markup is rendered into email bodies and picker
    payloads that leave this origin.
    """
    return delivery_url(asset, thumbnail=thumbnail)
