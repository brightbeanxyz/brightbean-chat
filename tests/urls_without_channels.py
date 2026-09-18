"""The product's URLconf with the channels app's routes left out.

A deployment can leave an app out — ``config/urls.py`` already mounts
``apps.analytics`` through ``_if_installed`` for exactly that — and the shell's
context processor runs on every response, error pages included. So "the channel
routes are not there" has to be an ordinary answer (no block), never an
exception, and that is what this URLconf is for.

Never referenced by ``config/urls.py``; a test points ``ROOT_URLCONF`` here.
"""

from typing import Any

from config import urls as product_urls


def _mentions_channels(entry: Any) -> bool:
    """Whether this entry carries the ``channels`` namespace.

    Matched on the namespace rather than on the route string: the connect
    flows, the settings pages and the public webhook endpoints all live under
    different prefixes, and it is the reversible names the sidebar asks for.
    """
    return getattr(entry, "namespace", None) == "channels"


urlpatterns = [entry for entry in product_urls.urlpatterns if not _mentions_channels(entry)]
