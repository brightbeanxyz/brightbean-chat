"""The two plans, as constants rather than rows.

There is deliberately **no** ``Plan`` or ``Price`` table. Stripe holds the
prices, and a database mirror of them is a second source of truth that drifts
silently the first time somebody edits one in the dashboard — the same argument
``apps/media_library/quotas.py`` makes about denormalised counters. What lives
here is the part Stripe does not know: which limits each plan carries, and the
English a reader sees on the billing page. The price *ids* are environment
variables, because they differ between the test and live accounts.

**The numbers mirror ManyChat's free plan**, which is the brief. Read off their
pricing page rather than a summary of it: 25 active contacts a month, any two
channels, up to four active automations, one user, a basic inbox. Their older
1,000-contact tier is gone and blog posts still quoting it are describing a plan
nobody can sign up for.

Two places where a literal transcription would have been wrong, both argued in
``docs/billing.md`` rather than hidden:

* **Broadcasts are not gated.** ManyChat puts them two rungs up. Here the
  active-contact meter already bounds them by arithmetic — a free organization
  cannot reach a twenty-sixth person, whatever surface it uses to try — so a
  second gate would need its own copy, its own tests and its own downgrade
  semantics while forbidding nothing the first one allows.
* **Stored contacts are not capped.** ManyChat caps contacts you *talk to*, not
  rows you keep. Capping storage would break CSV import for exactly the person
  a free tier exists to convert.

``None`` means unlimited, everywhere in this module. Not ``0``, and not
``sys.maxsize``: a sentinel that is falsy would make ``if limit:`` silently mean
"unlimited is zero", and a very large integer would make an over-limit message
read "0 of 9223372036854775807 used".
"""

from dataclasses import dataclass


class PlanKey:
    """The two plan identifiers. Stored on nothing — derived, always."""

    FREE = "free"
    PAID = "paid"


@dataclass(frozen=True)
class Limits:
    """What one plan allows. ``None`` is unlimited.

    Frozen because these are module-level singletons shared by every request in
    the worker, and a mutable one would let a caller's ``limits.seats = 99``
    leak into every subsequent response.
    """

    active_contacts_per_month: int | None
    channels: int | None
    active_automations: int | None
    seats: int | None
    workspaces: int | None
    api_access: bool


#: ManyChat's free plan, transcribed.
FREE_LIMITS = Limits(
    active_contacts_per_month=25,
    channels=2,
    active_automations=4,
    seats=1,
    # Not a ManyChat concept — ManyChat has no workspaces. It closes the hole
    # that billing sits on the organization while contacts, channels and flows
    # are workspace-scoped: without it a free organization multiplies every
    # other number on this list by clicking "New workspace".
    workspaces=1,
    api_access=False,
)

#: The paid plan, and also every organization on an install with no Stripe
#: configured. Those two are the same object on purpose: a self-hoster is not
#: running a generous free tier, they are running the whole product, and one
#: constant means there is no second definition of "unlimited" to drift.
UNLIMITED = Limits(
    active_contacts_per_month=None,
    channels=None,
    active_automations=None,
    seats=None,
    workspaces=None,
    api_access=True,
)

LIMITS_BY_PLAN: dict[str, Limits] = {
    PlanKey.FREE: FREE_LIMITS,
    PlanKey.PAID: UNLIMITED,
}


@dataclass(frozen=True)
class PlanCopy:
    """The English for one column of the comparison table."""

    key: str
    name: str
    tagline: str
    price_note: str
    features: tuple[str, ...]


#: The feature bullets, in the same order in both columns so a reader can scan
#: across. Written from the limits above rather than beside them, because a
#: bullet that disagrees with the constant it describes is the failure mode this
#: page has — and ``apps/billing/tests/test_plans.py`` asserts every number that
#: appears in a free bullet is the number the free limit actually carries.
PLAN_COPY: tuple[PlanCopy, ...] = (
    PlanCopy(
        key=PlanKey.FREE,
        name="Free",
        tagline="Everything you need to try it properly.",
        price_note="Free forever",
        features=(
            "25 contacts a month",
            "Connect 2 channels",
            "4 active automations",
            "1 user",
            "Shared inbox, labels and reminders",
            "Contacts, tags and segments",
        ),
    ),
    PlanCopy(
        key=PlanKey.PAID,
        name="Pro",
        tagline="Everything, with nothing counted.",
        price_note="Billed monthly or yearly",
        features=(
            "Unlimited contacts",
            "Every channel",
            "Unlimited automations and sequences",
            "Unlimited users",
            "Shared inbox, labels and reminders",
            "Contacts, tags and segments",
            "Public API and outbound webhooks",
        ),
    ),
)
