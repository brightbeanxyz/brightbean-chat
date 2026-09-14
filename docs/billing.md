# Billing (optional)

**If you are self-hosting BrightBean Chat, you do not need this page.** Leave the
Stripe settings empty — which is the default — and the software has exactly one
tier with every feature in it: no billing page, no plan, no limit counted, and no
checkout or webhook endpoint to secure. That is [SPEC §1.1](SPEC.md#11-non-goals-do-not-build),
and `apps/billing/tests/test_self_hosted_is_unlimited.py` is what keeps it true.

This page is for an operator running BrightBean Chat as a paid service.

## What it does

Two plans. **Free** carries the limits below; **Pro** carries none. A customer
subscribes through Stripe's hosted Checkout and manages the subscription through
Stripe's Customer Portal. Nothing about cards, addresses or invoices is stored
here — the only Stripe data in the database is a customer id, a subscription id
and its status.

| Free | Pro |
|---|---|
| 25 contacts reached per month | unlimited |
| 2 channel connections | unlimited |
| 4 active automations | unlimited |
| 1 user | unlimited |
| 1 workspace | unlimited |
| no public API | public API and outbound webhooks |

The numbers live in `apps/billing/plans.py` and are the only place to change them.

**The amounts on the page are display copy; Stripe is what charges.** The page
quotes $15 a month, or $12 a month billed yearly, from constants in that file.
Nothing in it reaches a Checkout session — the view maps the interval a customer
picked onto a configured price id, and Stripe bills whatever that price says. So
if you change an amount in the Stripe dashboard, change it in `plans.py` too, or
the page quotes one number and the card is charged another. A test checks the
"Save 20%" badge against the two amounts, so at least the discount cannot drift
from the prices it describes.

**"Contacts reached" is a monthly set, not a running total.** A person counts
once in a calendar month, however many messages pass, and counts whether they
wrote to you or you wrote to them. Deleting a contact frees their slot. Two
consequences worth knowing before a customer asks:

- **Inbound is counted but never blocked.** You cannot refuse a message somebody
  has already sent. So inbound traffic a free customer does not control can use
  up their allowance, and the refusal then lands on the thing they do control,
  which is starting new conversations.
- **Anyone who writes to you is already counted**, so replying to them is always
  allowed. The limit only ever bites on reaching somebody new.

**A downgrade deletes nothing.** A customer who cancels with eight automations
and five channels keeps all of it, running and editable. What stops is adding
more — and, once the monthly allowance is gone, reaching new people. Data export
and GDPR erasure keep working regardless.

## Setting it up

### 1. In the Stripe dashboard

Nothing here creates these for you.

1. A **product** for the paid plan.
2. Two **prices** on it: one monthly, one yearly. Note both price ids (`price_…`).
3. A **Customer Portal configuration** (Settings → Billing → Customer portal).
   Note its id (`bpc_…`). Decide there what customers may do — change plan,
   cancel, update payment method — because that configuration is the whole
   portal UI.
4. A **webhook endpoint** pointing at `https://your-host/webhooks/stripe/`,
   subscribed to exactly these five events:

   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `customer.deleted`

   Note its signing secret (`whsec_…`). Anything else you subscribe to is
   recorded and ignored, which is deliberate: answering 4xx to an event type we
   do not handle is how Stripe disables an endpoint.

### 2. In the environment

```
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_ID_MONTHLY=price_...
STRIPE_PRICE_ID_YEARLY=price_...
STRIPE_PORTAL_CONFIGURATION_ID=bpc_...
```

Billing switches on when the secret key **and both price ids** are set. The
webhook secret is deliberately not part of that switch, because the real setup
order is keys first and endpoint second — so the UI can go live before the
webhook does. Until the webhook secret is set, `/webhooks/stripe/` answers 404
and `manage.py check` warns (`billing.W002`). Both secrets are credentials;
treat them the way you treat `SECRET_KEY`.

### 3. Check it

```bash
python manage.py check
```

`billing.W001` means a price id is missing and billing is still off.
`billing.W002` means nothing will hear back from Stripe: a customer can complete
checkout and stay on the free plan.

Then walk it in test mode: subscribe, land back on the billing page showing Pro,
open Manage billing, cancel, and confirm the page shows the end date. Finally set
`STRIPE_SECRET_KEY=` empty and confirm the product is unlimited again.

## Switching billing on for an existing deployment

**Every existing organization becomes a free one the moment you set these
variables.** There is no grandfathering and no backfill — if you have customers
who should be on the paid plan, subscribe them before you throw the switch, or
they will hit the free limits with no warning. `manage.py check` will not tell
you this; it is a product decision, not a configuration error.

## When something goes wrong

**A customer paid and is still on the free plan.** The webhook did not arrive, or
failed. `reconcile_pending_checkouts` runs hourly, asks Stripe directly for any
checkout left unconfirmed for more than fifteen minutes, and repairs it. Wait for
the next sweep, or run housekeeping by hand. If it keeps happening, check the
webhook's delivery log in Stripe and that `STRIPE_WEBHOOK_SECRET` matches the
endpoint you registered — a mismatched secret answers 403 on every delivery.

**Stripe reports 403s on the endpoint.** Either the signing secret is wrong, or
the deliveries are more than five minutes old
(`STRIPE_WEBHOOK_TOLERANCE_SECONDS`), which usually means a clock problem on this
side.

**Stripe reports 404s on the endpoint.** `STRIPE_WEBHOOK_SECRET` is unset or
blank. An endpoint that cannot verify anything does not advertise that it exists.

**You deleted an organization that had a live subscription.** Deleting an
organization removes its billing row but **does not cancel the subscription** —
cancel it in the Stripe dashboard. That is deliberate: organization deletion also
runs unattended from a GDPR sweep, and moving money from inside one is the wrong
place for it. A warning is logged when it happens.

**A customer's card bounced.** They stay on the paid plan while Stripe retries —
`past_due` still entitles — and the Customer Portal is where they fix it. They
drop to free only when Stripe gives up and the subscription is `canceled`.

## Notes for anyone changing this

- **The webhook is the only writer of subscription state.** The browser's return
  from Checkout is decoration; a return URL is something a customer can edit.
- **`checkout.session.completed` writes no subscription fields** — it binds the
  customer id to the organization and nothing else. Subscription state comes only
  from `customer.subscription.*`, so there is one writer and one ordering
  question instead of two.
- **Nothing gates on `current_period_end`.** The webhook is the authority; a
  clock-based gate would lock a paying customer out during a webhook outage.
- **Delayed payment methods are not handled.** Enabling SEPA, bank debit or any
  other asynchronous method in Stripe means handling
  `checkout.session.async_payment_succeeded` and `_failed`, which this does not.
- **Dunning email is not built.** `customer.subscription.updated` already moves a
  bounced customer to `past_due`; that is the hook to build on.
