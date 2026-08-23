# Instagram

Instagram automation runs on the **Instagram API with Instagram Login**
(`graph.instagram.com`), against Instagram **professional** accounts — Business
or Creator. There is no Facebook Page in the middle and no Facebook Login: the
account authorises the app directly.

Specification: `docs/SPEC.md` §6.3 (the channel), §8 (the compliance rules),
§10 (the triggers), §13.2 and §19.
Implementation: `apps/channels/providers/instagram.py`,
`apps/channels/instagram_oauth.py`, `apps/channels/views_instagram.py`.

Instagram is the strictest channel in the product. Before you build a flow on
it, read [Sending rules](#sending-rules) — most of what people expect to be able
to do here, Meta does not allow.

---

## What you need before you start

Three things, and the third is the one that takes weeks rather than minutes.

1. **A Meta app** with the *Instagram* product added, in the Meta app dashboard.
2. **A professional Instagram account** (Settings → Account type and tools →
   Switch to professional account), with *Settings → Messages and story replies
   → Allow access to messages* turned on.
3. **Advanced Access, App Review and Business Verification** — required to serve
   any account other than the app owner's own. See
   [App review](#app-review-and-business-verification).

## Connecting an account

1. **Add the callback URL.** In the Meta app dashboard, under the Instagram
   product's *Business login settings*, add this deployment's redirect URI
   **exactly** as the connect page shows it:

   ```
   https://<your-host>/channels/instagram/callback/
   ```

   Meta matches this string character for character. It is one URL for the whole
   deployment — not one per workspace — because a Meta app has a single
   registered redirect URI. The workspace you are connecting into travels in a
   signed `state` parameter instead.

2. **Add the app credentials.** Copy the *Instagram app ID* and *Instagram app
   secret* into *Settings → Credentials → Instagram*, or set them on the
   deployment:

   ```
   PLATFORM_INSTAGRAM_CLIENT_ID=...
   PLATFORM_INSTAGRAM_CLIENT_SECRET=...
   PLATFORM_INSTAGRAM_VERIFY_TOKEN=...
   ```

   Resolution order is **workspace override → organization → deployment
   environment** (SPEC §4), so a single self-hosted app can serve every
   workspace, and one workspace can bring its own.

3. **Connect.** *Settings → Channels → Instagram → set it up*
   (`/w/<workspace>/settings/channels/instagram/connect/`), then authorise on
   Instagram. You come back with the account connected.

4. **Subscribe the app to the webhook fields.** This step is not automatable
   from here — it is a setting on your Meta app, not on the account. In the app
   dashboard, under *Instagram → Webhooks*, set the callback URL to

   ```
   https://<your-host>/webhooks/instagram/
   ```

   with `PLATFORM_INSTAGRAM_VERIFY_TOKEN` as the verify token, and subscribe to:

   `messages`, `messaging_postbacks`, `comments`, `mentions`,
   `message_deletions`.

   The verification `GET` is answered automatically. Without a configured verify
   token the endpoint answers **404** rather than advertising that it exists.

5. **Check it.** Send the account a DM. The channels list shows a *Last event*
   column; when it moves from "Nothing received yet" to a timestamp, inbound is
   working.

### Permissions requested

| Scope | What it buys |
|---|---|
| `instagram_business_basic` | The account's id and username. Required for everything else. |
| `instagram_business_manage_messages` | Reading and sending DMs, story replies, story mentions. |
| `instagram_business_manage_comments` | Reading comments and posting public replies — the comment trigger. |

All three are requested at connect time rather than incrementally. Connecting
without `manage_comments` would succeed and then fail every public reply, which
is a worse thing to discover later.

### The webhook has to be reachable

Meta will only deliver to a **public HTTPS URL with a valid certificate**. For
local development, put a tunnel in front of the dev server and register the
tunnel's hostname as both the redirect URI and the webhook URL. There is no
polling mode; this project uses webhooks only (SPEC §7.1).

Every workspace's accounts share one URL, `/webhooks/instagram/`. Deliveries are
signed with the **app secret** in `X-Hub-Signature-256`, verified as a raw-body
HMAC in constant time before any JSON is parsed. The connection is identified by
the Instagram account id in `entry[].id`. A delivery with a wrong or missing
signature gets a 403, exactly like one naming an account nobody has connected —
the two are indistinguishable on purpose.

The webhook secret shown on the connection page is **not** used by Instagram.
Meta signs with the app secret; rotating the per-connection secret changes
nothing here.

### Tokens

The connect flow exchanges the authorisation code for a short-lived token and
immediately trades that for a **long-lived** one, valid 60 days. Only the
long-lived token is stored, encrypted, and it is never displayed again.

An hourly housekeeping job refreshes any token inside seven days of expiry, so a
connected account should never need attention. If Meta refuses a refresh — the
account revoked access, the app lost a permission — the channel is flipped to
**Needs reconnection** and workspace admins get a notification. Reconnecting
through the same page repairs it in place: the conversations, contacts and
triggers on that connection are kept.

---

## What arrives

| What the contact does | Event | Notes |
|---|---|---|
| Sends a DM | `message` | Text and `attachments` (URLs, recorded, never fetched server-side) |
| Taps a quick reply | `postback` | `payload.button_id`, plus the text they tapped |
| Taps a button on a card | `postback` | `payload.button_id` from `node_id:button_id` |
| Opens an `ig.me` link with a ref | `referral` | `payload.ref` — SPEC §10's Ref URL trigger |
| Mentions the account in their story | `story_mention` | Opens the 24-hour window |
| Replies to one of the account's stories | `story_reply` | Carries the reply text, so keywords work |
| Comments on a post | `comment` | Comment id, post id, parent id, body |
| Unsends a DM | — | The stored message is redacted; see below |

**Echoes are filtered.** Messages the account sends — from this product or from
the Instagram app on a phone — come back as deliveries flagged `is_echo`, and
are dropped. So is a comment whose author is the account itself, which is what
stops a comment trigger from answering its own public reply forever.

**Mentions are dropped, and that is a limitation rather than a choice.** The
`mentions` field fires when somebody `@`s the account in their own comment or
caption. Meta's payload names the media, sometimes the comment, and the text —
but never the **author**. Without an author there is nothing to key the
once-per-commenter-per-post guard on and no address to open a DM to, and
emitting a comment event anyway gave every mention on a post the same empty
commenter: the first one claimed the guard and locked out everybody else, while
itself failing to open a thread. Answering mentions needs a
`GET /{ig-comment-id}?fields=from`, which is a Graph round trip inside the
webhook acknowledgement, so it is out of scope rather than done badly. The
parser is ready for the field if Meta ever adds it.

**A comment with no author is dropped too**, for the same reasons — and for one
more: the filter that stops the account answering its own public reply works by
comparing the author against the account, so an anonymous comment would slip
past it.

### Deleted messages

Instagram lets people unsend a DM, and SPEC §6.3 and §19 both require it to be
honoured. When a `message_deletions` delivery arrives, the stored message row is
**kept** and its body **redacted**: the thread keeps its shape and its
timestamps, the message's status becomes `deleted`, and the inbox renders a
tombstone reading *"This message was deleted."* in its place.

It applies in both directions — a contact unsending theirs, or the account
unsending one of ours.

### Follows

SPEC §10 lists a `follow` trigger, and the Instagram API with Instagram Login
publishes **no follow webhook field**. The trigger type, its parser and its
matcher all exist and are tested, and on a real deployment it simply never
fires. That is deliberate: SPEC §10 asks for it to "degrade gracefully if the
field is unavailable to the app", and an app granted the field in future needs
no code change.

Where a follow *does* arrive, it creates a contact with **no consent and no
messaging window** — following an account is a relationship, not permission to
message back — so a flow started from one cannot send until the person writes
first.

---

## What goes out

The flow engine renders one abstract message and this adapter turns it into
Instagram's Send API calls (`POST /me/messages`).

| Block | Becomes |
|---|---|
| Text | A text message, split to fit **1000 bytes** |
| Image / audio / video | An `attachment` by URL; the caption follows as its own bubble |
| File | Text — Instagram messaging has no generic document attachment |
| Card / gallery | A generic template, up to 10 elements per message |
| Buttons | Up to 3 `postback` / `web_url` buttons on a generic template |
| Quick replies | Up to 13 chips on a text message, labels cut at 20 characters |

Two consequences worth knowing before you build a flow:

**The cap is 1000 bytes, not characters.** A thousand characters of Japanese or
emoji is three or four thousand bytes, so long non-Latin messages split into more
bubbles than the character count suggests.

**Buttons need a card.** Instagram has no "text with buttons" message. When a
message has buttons, its trailing 80 characters become the title of a
single-element card and the rest stays as the bubble before it. Nothing is
duplicated and nothing is invented — but a long message with buttons arrives as
two bubbles. A message with buttons and *no* text anywhere has nothing to hang
them on, and they are left out with a warning in the log.

**Attachments have no captions.** Meta's attachment payload carries a URL and
nothing else, so a media block's caption is sent as the message after it.

**A caption-less media message cannot carry buttons or quick replies.** Buttons
need a card, and Meta requires a card to have a title; quick replies are only
accepted beside message text. A `send_message` node holding nothing but an image
has neither, so the interaction is dropped with a warning in the log rather than
rendered as numbered options that would resolve to nothing. Give the message a
text block or a caption and both work normally.

Typing indicators and read receipts are supported (`sender_action`) and are sent
before an inline reply, as SPEC §7.1 asks.

---

## Sending rules

This is the part that surprises people. Instagram is the most restrictive
channel in the product, and none of it is this project's choice.

| Situation | What happens |
|---|---|
| Within **24 hours** of the contact's last inbound message | Anything sends |
| Automation, outside 24 hours | **Blocked** |
| An agent reply from the inbox, within **7 days** of last inbound | Sends, tagged `HUMAN_AGENT` |
| An agent reply, beyond 7 days | **Blocked** |
| Broadcasts | **Never** — Instagram does not appear in the broadcast composer at all |
| A contact who has never messaged the account | **Blocked**, except a private reply to their comment |

The human-agent allowance is available to inbox sends only, hard-coded (SPEC
§22). A flow cannot set the tag to buy itself the extension: the compliance
engine replaces the tag on every outbound message rather than passing the
caller's through.

Rate limit: 8 sends per second per connection, through the per-connection token
bucket. Meta's own published ceiling for private replies is 750 per hour per
account, comfortably above that. On HTTP 429 the send is rescheduled with Meta's
own `Retry-After`.

---

## Comment to DM

SPEC §10's comment trigger, and the reason most people connect Instagram at all.

This works the same way whether or not the person has messaged the account
before. What differs is only the last step: somebody already in a DM thread gets
the flow's first message as an ordinary DM, because Meta does not need a private
reply to reach them.

When somebody comments on a post and the trigger matches — post scope, include
and exclude keywords, top-level only — three things happen, in order:

1. The comment is **claimed**, once. The same comment can never be answered
   twice, and with `once_per_contact_per_post` (the default) the same person
   gets one reply per post however many times they comment.
2. The **public reply** is posted under their comment, if the trigger configures
   one — a fixed text, or one picked at random from a list.
3. The **private reply** goes out as a DM. This is the flow's first message, and
   it is addressed by comment id rather than by user id, because Meta will not
   accept an ordinary DM to somebody who has never written to the account.
   Everything after it is an ordinary DM.

Meta's limits, which the product enforces rather than discovers:

- **One private reply per comment.** Ever.
- **Seven days** from the comment. A comment surfacing later is not claimed at
  all, so it does not spend that person's one reply on a message that would be
  refused.

And one rule that is the product's rather than Meta's: `once_per_contact_per_post`
(on by default) means one person gets one reply per post however many times they
comment on it. It applies to everybody — a repeat customer who has DM'd before is
not exempt.

The public reply and the private reply are **queued**, not sent inside the
webhook request — SPEC §7.1 budgets 1.5 seconds for the whole inline path, and
these are two round trips to Meta.

### Picking posts

The trigger's *Posts* setting takes either every post or a specific list.
*Choose from Instagram* lists the account's recent media with thumbnails;
clicking one adds its id. The box stays editable, so an older post that is not in
the first page is still configurable by pasting its id.

### "Like the comment" is not available on Instagram

SPEC §10 lists a `like_comment` switch on the comment trigger, and **the
Instagram API cannot do it**. Meta's IG Comment reference exposes `like_count` as
read-only and publishes exactly two write operations on a comment: `hide`, and
the `replies` edge. There is no like endpoint, documented or otherwise.

Rather than offer a switch that silently does nothing, the option is **hidden**
in the trigger form while Instagram is the only platform the trigger can fire
on. If you have a value stored from before — or from a trigger that also covers a
platform that does have such an API — it is preserved rather than cleared. The
adapter never attempts a like call.

---

## App review and business verification

**You can automate your own account today. You cannot automate anybody else's
until Meta says so.**

With Standard Access, a Meta app can only act on Instagram accounts whose users
have a role on the app itself — the owner, and anyone added as a developer or
tester. That is enough to build and test, and enough for a self-hoster running
their own account.

To connect accounts belonging to other people you need all three of:

- **Advanced Access** for `instagram_business_basic`,
  `instagram_business_manage_messages` and `instagram_business_manage_comments`;
- **App Review**, submitted per permission, with a screencast showing the
  end-to-end use of each one;
- **Business Verification** of the business that owns the app.

Budget weeks, not days, and expect at least one rejection asking for a clearer
screencast. This is a Meta process; nothing in this product changes it.

---

## Out of scope in v1

- **Broadcasts.** Instagram never appears in the composer (SPEC §13.2).
- **Facebook-Login-based Instagram.** This product uses Instagram Login only;
  the Page-linked variant is a different API surface with different permissions.
- **Liking comments**, for the reason above.
- **Hiding or deleting comments.** The API supports both; no trigger action
  exposes them yet.
- **Answering `@mentions`.** See above — Meta's payload names no author, and
  resolving one costs a Graph call on the webhook path.
- **Message reactions**, both directions.
- **Group threads.** Instagram has them; this product does not model them.
- **`ig.me` link generation.** The `ref_url` trigger matches an incoming ref;
  building the link is manual.
