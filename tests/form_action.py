"""The inventory of origins a form submission may end its redirect chain on.

Chrome and Safari check ``form-action`` against the **whole redirect chain** a
form submission produces, not only the form's immediate target, and against the
policy of the page that held the form. Firefox does not, and the specification
question is still open as `w3c/webappsec-csp#8
<https://github.com/w3c/webappsec-csp/issues/8>`_ — but two of the three engines
enforce it, so it is the behaviour this application has to be correct against.

That is issue #115: the Messenger connect page posts to itself, answers 302 to
``https://www.facebook.com/...``, and Chrome refuses the navigation because
``form-action`` said ``'self'``. The button appears to do nothing. Nothing
server-side is wrong, which is exactly why no server-side test caught it.

Four integrations have that shape. ``DESTINATIONS`` is the whole inventory and
``tests/test_form_action_destinations.py`` sweeps it, holding the policy to it
from both ends: every entry is permitted, and nothing is permitted that is not
an entry. A fifth integration arriving without a row fails the sweep rather than
shipping broken in Chrome — the ``tests/ssrf.py`` bargain, applied to traffic
leaving by the browser instead of by httpx.

A library rather than a test module because three suites need the same two
questions answered — what does this page's ``form-action`` say, and does it
permit this redirect — and the first version of this had that parsing copied
into each of them. ``assert_page_permits_its_redirect`` is the whole per-flow
assertion; ``apps/channels/tests/test_messenger_connect.py``,
``apps/channels/tests/test_instagram_connect.py`` and
``apps/billing/tests/test_checkout_flow.py`` each point it at their own page.
"""

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from apps.billing.services import STRIPE_HOST_SUFFIX
from config.settings.base import csp_origin


@dataclass(frozen=True)
class Destination:
    """One provider a form submission is allowed to finish its navigation on."""

    label: str
    #: A CSP source expression, exactly as ``config/settings/base.py`` spells it.
    source: str
    why: str


DESTINATIONS = (
    Destination(
        label="Messenger",
        source="https://*.facebook.com",
        why=(
            "Facebook Login for Business. apps/channels/views_messenger.py's connect page starts the "
            "flow on a POST so the outbound leg carries a CSRF token, and answers a redirect to the "
            "login dialog. The domain rather than www. alone, because Meta moves its own dialog to m. "
            "on a phone and to business. for Business logins, inside the same navigation. Issue #115."
        ),
    ),
    Destination(
        label="Instagram (apex)",
        source="https://instagram.com",
        why=(
            "The apex redirects to www., and that redirect is itself a hop in the same navigation. "
            "CSP wildcards match subdomains but not the domain, so it needs its own entry."
        ),
    ),
    Destination(
        label="Instagram",
        source="https://*.instagram.com",
        why=(
            "Instagram API with Instagram Login. apps/channels/views_instagram.py, the same shape as "
            "Messenger and for the same reason."
        ),
    ),
    Destination(
        label="Stripe (apex)",
        source="https://stripe.com",
        why=f"apps/billing/services._stripe_url accepts the apex as well as {STRIPE_HOST_SUFFIX}.",
    ),
    Destination(
        label="Stripe",
        source="https://*.stripe.com",
        why=(
            "Hosted Checkout and the Customer Portal, from apps/billing/views.py. Exactly the set "
            "apps/billing/services._stripe_url accepts, because a URL that function passes and this "
            "policy refuses is a redirect the browser drops in silence."
        ),
    ),
    Destination(
        label="Google SSO",
        source="https://accounts.google.com",
        why=(
            "allauth's provider login view. SOCIALACCOUNT_LOGIN_ON_GET is False, which is what makes "
            "this a form submission rather than a link — see config/settings/base.py."
        ),
    ),
)

#: A page that renders for anonymous clients, so a caller that only wants to
#: read the policy need not build a tenancy first.
ANY_PAGE = "/accounts/login/"


def sources(client: Any, page: str = ANY_PAGE) -> list[str]:
    """The ``form-action`` sources a browser would be handed for ``page``."""
    response = client.get(page)
    return sources_of(response)


def sources_of(response: Any) -> list[str]:
    """The ``form-action`` sources carried by an already-fetched response."""
    header = response.headers.get("Content-Security-Policy", "")
    for part in header.split("; "):
        if part == "form-action" or part.startswith("form-action "):
            return part.removeprefix("form-action").split()
    raise AssertionError(f"no form-action directive in the policy: {header!r}")


def origin_of(url: str) -> str:
    """The origin a CSP source would have to match for ``url`` to be allowed.

    ``csp_origin`` rather than ``urlparse(...).netloc``: it is the function that
    builds the storage origins in the policy itself, and it drops userinfo and
    brackets IPv6 literals, neither of which a bare netloc does.
    """
    origin = csp_origin(url)
    assert origin is not None, f"not a URL with an origin: {url!r}"
    return origin


def permits(sources_: list[str], url: str) -> bool:
    """Would this ``form-action`` list allow a navigation to ``url``?

    Host-source matching, narrowed to the two shapes this policy uses: an exact
    origin, and ``scheme://*.domain``, which CSP defines as matching any
    subdomain but **not** the domain itself. Keyword sources like ``'self'``
    never match an off-origin URL and are ignored.
    """
    parsed = urlparse(url)
    for source in sources_:
        if source.startswith("'"):
            continue
        wanted = urlparse(source)
        if wanted.scheme != parsed.scheme or not wanted.hostname or not parsed.hostname:
            continue
        if wanted.hostname.startswith("*."):
            if parsed.hostname.endswith(wanted.hostname[1:]):
                return True
        elif wanted.hostname == parsed.hostname:
            return True
    return False


def assert_page_permits_its_redirect(client: Any, page: str, submits_to: str | None = None, **post: Any) -> str:
    """POST a page's form and assert *that page's* policy allows where it lands.

    The two halves of issue #115 read off one request each: a correct 3xx to a
    provider is still a button that does nothing unless the page that carried
    the form said that origin was allowed. Returns the ``Location``, so a caller
    can go on to assert what is in it.

    ``submits_to`` is for the forms that post somewhere other than the page they
    are on — the Google button on the login page posts to allauth's own view.
    The policy read is always the page's, never the target's, because that is
    the one the browser checks.
    """
    allowed = sources_of(client.get(page))

    response = client.post(submits_to or page, post or None)

    assert "Location" in response.headers, (
        f"{page} answered {response.status_code} with no redirect; this assertion needs the flow to start"
    )
    location = response["Location"]
    assert permits(allowed, location), (
        f"{page} redirects to {origin_of(location)}, which its own form-action does not permit: "
        f"{' '.join(allowed)}. Chrome and Safari drop that navigation — see tests/form_action.py."
    )
    return location
