# Facebook Messenger

Page-scoped messaging through Meta's Send API: DMs, the message-tag system that
governs sending outside the 24-hour window, m.me referral links, delivery and
read receipts, and comment-to-DM.

The most expensive channel to set up in this project — a Meta app, a page, App
Review and Business Verification — and the one with the strictest rules about
what you may send and when.

Specification: `docs/SPEC.md` §6.4 (the channel), §8 (the compliance rules) and
§10 (the triggers). Implementation: `apps/channels/providers/messenger.py`, the
connect flow in `apps/channels/messenger_oauth.py`, and the webhook-side parts
shared with Instagram in `apps/channels/providers/meta_common.py`.

The Graph *client* is not shared: Messenger talks to `graph.facebook.com` and
Instagram to `graph.instagram.com`, so each adapter keeps its own — see
`docs/channels/instagram.md` for the sibling.

## Connecting a page

### 1. Create the Meta app

At [developers.facebook.com](https://developers.facebook.com/apps/), create an
app of type **Business** and add two products to it:

- **Messenger** — the channel itself.
- **Facebook Login for Business** — how an operator grants access to a page.

### 2. Register the redirect URI

Under *Facebook Login for Business → Settings → Valid OAuth Redirect URIs*, add
this deployment's callback **exactly**:

```
https://<your-host>/channels/messenger/callback/
```

Meta matches it character for character. The connect page shows the value this
deployment will send, computed from `APP_URL` rather than from the browser's
address bar, so what you paste and what is sent cannot disagree behind a proxy.

The callback deliberately carries no workspace id: Meta whitelists one URI per
app, and a per-workspace path would mean one whitelist entry per tenant. The
workspace travels in the signed `state` instead.

### 3. Give this deployment the app credentials

Either per workspace or organization, under **Settings → Credentials**, or for
the whole deployment in the environment:

```
PLATFORM_MESSENGER_CLIENT_ID=<app id>
PLATFORM_MESSENGER_CLIENT_SECRET=<app secret>
PLATFORM_MESSENGER_VERIFY_TOKEN=<any long random string you choose>
```

Resolution is SPEC §4's chain: a workspace override beats the organization's app,
which beats the environment, and a level is only used if it is complete. Meta's
own console says *App ID* and *App Secret*; `app_id`/`app_secret` are accepted as
aliases of the OAuth spelling.

### 4. Point the webhook at this deployment

Under *Messenger → Settings → Webhooks*, add a callback URL and the verify token
you just chose:

- **Callback URL**: `https://<your-host>/webhooks/messenger/`
- **Verify token**: the value of `PLATFORM_MESSENGER_VERIFY_TOKEN`

Meta immediately performs a `GET` with `hub.challenge`; the endpoint answers it
only if the token matches, and **404s if no verify token is configured** — an
endpoint that cannot verify anything should not advertise that it exists.

Subscribe the app to these fields:

`messages`, `messaging_postbacks`, `messaging_referrals`, `message_deliveries`,
`message_reads`, `feed`

`feed` is only needed for SPEC §10's comment trigger; the rest are the DM channel.

### 5. Connect the page

**Settings → Channels → Facebook Messenger → set it up**
(`/w/<workspace>/settings/channels/messenger/connect/`), then *Continue with
Facebook*. You sign in, choose which pages to share, and pick one to connect.

Connecting a page does three things, in this order:

1. stores the page access token, **encrypted**;
2. calls `subscribed_apps` to subscribe the page to the fields above;
3. sets the page's **Get Started** button, so SPEC §10's welcome trigger can
   fire.

If step 2 fails the connection is removed rather than left in the list looking
healthy while nothing is ever delivered to it. If step 3 fails the connection
stands and you are told: everything except the welcome trigger works without it.

Messenger is not offered in the generic "Add a channel" form. That form creates a
connection row and nothing else, which here would be a page with no token whose
every send fails.

### Permissions requested

| Scope | Why |
|---|---|
| `pages_messaging` | Send and receive DMs. The channel itself. |
| `pages_show_list` | Makes `/me/accounts` return anything, so you can pick a page. |
| `pages_manage_metadata` | Permits `subscribed_apps` and the Get Started button. Without it a page connects and then silently never delivers. |
| `pages_read_engagement` | Read the comment that fires a comment trigger. |
| `pages_manage_engagement` | Post the public reply and the like. |

### App Review and Business Verification

Meta grants **Standard Access** to an app's own developers and testers only. To
serve a page you do not own — that is, to serve customers — you need:

- **App Review** for `pages_messaging` and, if you use the comment trigger,
  `pages_manage_engagement` and `pages_read_engagement`;
- **Business Verification** of the business behind the app;
- a privacy policy URL and a data-deletion callback on the app.

Until that is granted, connect a page you administer with a developer or tester
role on the app. Everything works; only who may connect is limited.

## The webhook

One URL per deployment, `/webhooks/messenger/`, shared by every workspace's pages
(SPEC §7.1). The connection is resolved from the page id inside the payload —
`entry[].id` — and each entry resolves its own, so a delivery legitimately
spanning two pages is filed against both rather than attributed to whichever one
carried it. The whole batch costs one connection query, not one per entry.

A delivery can span **workspaces** too, when one Meta app is configured in the
environment and several workspaces connect pages under it. Those entries are kept,
because both pages are signed for by the same app secret — so whoever produced a
valid signature holds the authority for both. If either workspace overrides the
app with its own credentials the secrets differ, and its entries are dropped:
SPEC §4's per-workspace override is a real tenant boundary and stays one.

Every delivery is signed: `X-Hub-Signature-256`, HMAC-SHA256 of the **raw body**
under the **app secret**, compared in constant time before the JSON is parsed.
Rotating a connection's webhook secret from the settings page therefore changes
nothing for Messenger and does not break delivery — Meta signs with the app, and
the app secret is rotated in Meta's console.

A deployment with no app secret configured refuses every delivery, because with
no secret there is no way to tell a real one from a forged one.

### What arrives

| Field | Becomes | Notes |
|---|---|---|
| `messages` | `message` | Echoes of our own sends (`is_echo`) are dropped; ingesting them would file every outbound message as inbound and reopen the messaging window on our own traffic. Attachments arrive as URLs, which are **recorded, never fetched**. |
| `messages` with `quick_reply` | `postback` | A tapped chip is a button press. Treating it as a message would let the chip's label fire a keyword trigger and let a default reply answer a button the flow itself offered. |
| `messaging_postbacks` | `postback` | `GET_STARTED` fires SPEC §10's welcome trigger. Meta sends no id for a postback, so one is derived from the content **and the platform's own timestamp** — without the timestamp two presses of the same button would deduplicate into one, and using our clock when the payload's is unreadable would make every redelivery a new event. |
| `messaging_referrals` | `referral` | `m.me/<page>?ref=<ref>` → SPEC §10's Ref URL trigger. A ref also arrives *inside* the get-started postback on a first contact, and that produces both events rather than one. |
| `message_deliveries` | `delivery_status` | One event per message id Meta names. |
| `message_reads` | `delivery_status` | Meta sends a **watermark**, not message ids, so it is resolved against this contact's own recent outbound messages — scoped to the person, bounded, read-only. A watermark that cannot be read marks **nothing**: falling back to "now" would be the most permissive cutoff there is. |
| `feed` (`item: comment`, `verb: add`) | `comment` | The page's own comments are ignored, or our public reply would fire the trigger at ourselves. |

Anything else — `optin`, `account_linking`, reactions, message edits, the
handover protocol's `standby` batches — is dropped rather than half-parsed.

## What goes out

The flow engine renders one abstract message and
`apps.channels.downgrade.downgrade` adapts it before the adapter sees it, so
these limits are enforced in one place rather than per platform.

| Limit | Value | Where it is applied |
|---|---|---|
| Message text | 2000 characters | Downgrade renderer, from `capabilities.max_text_len` |
| Buttons | 3 | `capabilities.max_buttons` — Meta's own cap |
| Quick replies | 13 | `capabilities.max_quick_replies` — Meta's own cap |
| Button / quick-reply title | 20 characters | The adapter |
| Carousel elements | 10 per template | The adapter; a longer gallery becomes several templates rather than losing cards |
| Postback payload | 1000 bytes | The adapter |

Cards and carousels are **native** here — they become generic templates — which
is the main way Messenger differs from Telegram on the wire.

Buttons have no `reply_markup` equivalent: they ride *inside* a template, so a
text message with buttons becomes a **button template**, whose text caps at 640
characters rather than 2000 (an over-long message is split and only the tail
carries the buttons). When the last part of a message is an attachment there is
nowhere to put a button, so postback buttons become **quick replies** instead — a
quick reply comes back as a `button_id` exactly like a postback does, so the
semantics survive. URL buttons in that position are dropped rather than turned
into chips that do nothing when tapped.

Postback payloads are `node_id:button_id`, the same encoding Telegram uses. The
colon is always present, even for a message with no node behind it, so decoding
is unambiguous whatever a button id contains.

One abstract message can become several Send API calls, and that sequence is
**not atomic**. If the third of three fails, the first two have arrived and the
retry sends all three again — idempotency is keyed on the message row (SPEC §9.4)
and there is one row. The contact sees a duplicate rather than a gap.

## The 24-hour window and message tags

This is the part that gets pages restricted, so it is worth reading in full.

A page may message a person freely for **24 hours** after their last message.
Outside that window:

| Who is sending | What happens |
|---|---|
| Automation or a broadcast | Refused unless the message carries one of the three non-promotional tags. |
| An agent, in the inbox, within 7 days of the last inbound message | Allowed, tagged `HUMAN_AGENT`. |
| An agent, past 7 days | Refused unless tagged like automation. |

The three tags, and what Meta permits each for:

- `CONFIRMED_EVENT_UPDATE` — reminders for an event the person signed up for;
- `POST_PURCHASE_UPDATE` — information about a transaction they made;
- `ACCOUNT_UPDATE` — a change to their account or application.

**Promotional content sent under a message tag violates Meta's policy and can
disable the page.** That sentence is not advice this document is adding: it is
part of `allowed_use_text` on the policy row, and SPEC §6.4 requires the
broadcast composer to display it verbatim whenever an operator selects a tag.

`HUMAN_AGENT` is **agent-only and hard-coded** (SPEC §22). A flow author who sets
it on an automation node does not get it: the compliance engine replaces the tag
rather than passing the caller's through, so the send is refused like any other
untagged automation.

None of this is implemented in the adapter. It is one row of data in
`apps/channels/policy.py` and one shared engine in `apps/messaging/compliance.py`
— there is no Messenger branch anywhere in `apps/messaging/`, and a test asserts
that there is not.

Sponsored messages and the Marketing Messages API are **out of scope in v1**, as
is one-time notification.

## Triggers

| Trigger | Fires on |
|---|---|
| **Welcome** | The Get Started button, configured at connect time. |
| **Ref URL** | `m.me/<page>?ref=<ref>`, standalone or inside the first get-started postback. |
| **Keyword** / **Default reply** | Ordinary inbound messages. |
| **Comment** | A comment on one of the page's posts. |

### Comment to DM

SPEC §10, and the most involved path in the channel. When a comment matches a
trigger:

1. the comment is **claimed** in the same request, in the database — so a
   redelivery, and a second comment from the same person on the same post, are
   refused by a unique constraint rather than by a check that could race;
2. the rest is queued, because a public reply, a like and a private reply are
   three round trips to Meta and the webhook has a 1.5-second budget for
   everything (SPEC §7.1);
3. one queued action posts the public reply and the like, if the trigger asks for
   them;
4. a **second** queued action opens the DM thread and starts the flow — and the
   flow's **first message is the private reply**, addressed by comment id.

The claim itself, and the decision to answer it, are platform-agnostic: L4-A's
routing stage takes the guard and hands off through
`apps.flows.triggers.comments`' responder registry, which Instagram registers
against too. Only the answering is Messenger's.

Steps 3 and 4 are two queue rows rather than one, and the split is deliberate.
Meta gives no way to make a comment or a like idempotent, so that half runs **at
most once**: a retry would put a second public reply under a customer's comment,
which is worse than the one a transient failure costs. The DM half is idempotent
by construction and is the half the public reply just promised, so it **is**
retried. With one shared row, anything that went wrong opening the thread put the
public reply back on the queue too.

Point 4 is a Meta rule rather than a stylistic choice: a page may send **exactly
one** message in reply to a comment, and only within **7 days** of it. An opener
followed by the flow's real first message would have the second one refused. Past
the seven days nothing is claimed at all, because claiming would spend the
once-per-person-per-post guard on a reply the platform will not accept.

**So a comment trigger's flow should open with a single message.** "One message"
is Meta's count of Send API calls, not of blocks — a captioned image is two calls,
and so is text past 2000 characters or a gallery of more than ten cards. Only the
first can carry the private reply: addressed to the comment the rest exceed the
allowance, and addressed to the person they land outside a 24-hour window that
only opens when *they* reply. When the first node renders to more than one call
the adapter sends the first and drops the rest with a warning naming the count, so
it shows up in the log rather than as a message that quietly fails at the platform.

The adapter cannot see *which* send it is about to make, so the claim is offered
only to a send in the **ten minutes** after it is recorded, not for the platform's
full seven days. That covers the real case — the worker starts the flow and its
first node sends — and excludes the one that would go wrong: a flow that opens
with a condition or a delay leaves the claim standing, and days later an agent's
inbox reply or a broadcast would otherwise be delivered as a reply to a stale
comment, spending the one private reply Meta allows. Past the ten minutes the
flow's first message goes out as an ordinary DM through the 24-hour window the
comment opened — a plainer reply, not a failed one.

The **post picker** in the trigger's configuration lists the page's recent posts
so a trigger can be scoped to specific ones without pasting ids by hand.

## Rate limits

Meta's Send API limit scales with the page's audience, and the platform answers
**429** when it is exceeded. There is no throttle in the adapter, deliberately:

- **Global**: the connection's token bucket (`apps.messaging.buckets`), refilled
  at `rate_default = 40.0` from `apps.channels.policy`.
- **Per recipient**: already guaranteed by the shape of the system. SPEC §9.6
  serialises everything one contact does behind an advisory lock and SPEC §9.2
  allows one live execution per contact, so two messages to the same person are
  never in flight at once. A sleep in the adapter would be held *inside* that
  lock.

When Meta throttles anyway, the send pipeline reschedules rather than failing the
message.

Two errors mean something durable rather than "try later":

- **190** (or a 401) — the page token is dead. The connection moves to *Needs
  reconnection*, workspace admins are notified, and nothing is sent on it until
  it is reconnected.
- **551** — the person blocked the page, deleted their account, or the thread is
  gone. An opt-out is recorded for that identity and nothing is sent to them
  again.

## Secrets

The page access token *is* the page: anyone holding it can read every message
sent to it and send as it. It is stored encrypted, never rendered back, and only
ever sent in an `Authorization` header — never in a URL, because `httpx` logs the
URL of every request it makes.

The OAuth token exchange is **POSTed with a form body** for the same reason,
against every example in Meta's own documentation: a `GET` there would write a
live app secret and a single-use authorization code into the application log.

`apps.common.logging` scrubs Meta's `EAA…` token shape and `…secret=`-style
values out of anything that reaches a log regardless.

## Out of scope in v1

Sponsored messages and the Marketing Messages API, one-time notification, the
handover protocol, Messenger for Instagram (that is a separate API — see
`docs/channels/instagram.md`), persistent menus, and message reactions.
