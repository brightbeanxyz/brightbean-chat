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
class Price:
    """What one billing interval costs, as the reader sees it.

    **These amounts are display copy and Stripe is what actually charges.**
    Nothing here reaches a Checkout session — the view maps an interval onto a
    configured price id and Stripe bills whatever that price says. So an
    operator who changes an amount in the Stripe dashboard has to change it here
    too, or the page quotes one number and the card is charged another. Reading
    the live amount from Stripe would close that gap and costs an API call on
    every page load; ``docs/billing.md`` carries the warning instead.
    """

    amount: str
    cadence: str
    note: str


#: Free. One price, and it never changes.
FREE_PRICE = Price(amount="$0", cadence="", note="Free forever")

#: Paid, per interval. Yearly is quoted *per month* so the comparison is like
#: for like — a reader seeing "$144" next to "$15" has to do arithmetic before
#: they can tell whether it is cheaper.
PAID_PRICES: dict[str, Price] = {
    "monthly": Price(amount="$15", cadence="/month", note="Billed monthly"),
    "yearly": Price(amount="$12", cadence="/month", note="Billed yearly, $144 up front"),
}

#: $180 a year against $144. Exact, so it is stated as a number rather than as
#: "two months free", which would be 2.4 and is the kind of rounding a reader
#: checks.
YEARLY_SAVING = "Save 20%"


@dataclass(frozen=True)
class Feature:
    """One bullet, and the glyph that stands for it.

    ``icon`` is a name from ``templates/partials/_nav_icon.html``'s vocabulary —
    the same one the sidebar draws from. Reusing it rather than inventing a
    second set means the glyph beside "Connect 2 channels" is the glyph on the
    Channels nav row, which is the whole value of a glyph: it is recognised
    before it is read.

    ``apps/billing/tests/test_billing_page.py`` asserts every name here is one
    that partial actually draws, because an unknown name renders a neutral dot
    rather than failing — kind in a nav row, invisible here.
    """

    icon: str
    text: str


@dataclass(frozen=True)
class PlanCopy:
    """The English for one column of the comparison table."""

    key: str
    name: str
    tagline: str
    features: tuple[Feature, ...]
    #: One price, or None when the column carries an interval toggle instead.
    price: Price | None = None
    #: The Pro column carries the product mark. Only one plan is being sold, and
    #: the emblem is what makes the column read as the thing on offer rather
    #: than as the right-hand half of a table.
    show_logo: bool = False

    @property
    def has_interval_choice(self) -> bool:
        return self.price is None


#: The feature bullets, in the same order in both columns so a reader can scan
#: across. Written from the limits above rather than beside them, because a
#: bullet that disagrees with the constant it describes is the failure mode this
#: page has.
PLAN_COPY: tuple[PlanCopy, ...] = (
    PlanCopy(
        key=PlanKey.FREE,
        name="Free",
        tagline="Enough to prove it works.",
        price=FREE_PRICE,
        features=(
            Feature("contacts", "25 contacts a month"),
            Feature("channels", "2 channels"),
            Feature("flows", "4 active automations"),
            Feature("user", "1 user"),
            Feature("inbox", "Shared inbox, labels and reminders"),
            Feature("tag", "Contacts, tags and segments"),
        ),
    ),
    PlanCopy(
        key=PlanKey.PAID,
        name="Pro Chat",
        tagline="Everything, with nothing counted.",
        features=(
            Feature("contacts", "Unlimited contacts"),
            Feature("channels", "Every channel"),
            Feature("flows", "Unlimited automations"),
            Feature("sequences", "Unlimited sequences and broadcasts"),
            Feature("users", "Unlimited users"),
            Feature("inbox", "Shared inbox, labels and reminders"),
            Feature("tag", "Contacts, tags and segments"),
            Feature("key", "Public API and outbound webhooks"),
        ),
        show_logo=True,
    ),
)
