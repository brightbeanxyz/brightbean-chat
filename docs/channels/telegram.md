# Telegram

The first channel BrightBean Chat shipped, and the cheapest one to set up: a
token from BotFather, a webhook, done. No OAuth, no app review, no business
verification.

Specification: `docs/SPEC.md` §6.2 (the channel) and §16 (the flow preview).
Implementation: `apps/channels/providers/telegram.py`.

## Connecting a bot

1. **Create the bot.** Open [@BotFather](https://t.me/BotFather) in Telegram and
   send `/newbot`. Give it a display name and a username ending in `bot`.
   BotFather replies with a token that looks like `123456789:AA…`.

2. **Paste the token** into *Settings → Channels → Telegram → set it up*
   (`/w/<workspace>/settings/channels/telegram/connect/`).

   The app calls `getMe` to check the token and learn the bot's id and
   username, creates the connection, and then calls `setWebhook` with a freshly
   generated `secret_token`. Nothing is stored until `getMe` succeeds, and the
   connection is rolled back if `setWebhook` fails — a bot Telegram will not
   deliver to is not a connection.

3. **Send the bot `/start`.** The channels list shows a *Last event* column;
   when it moves from "Nothing received yet" to a timestamp, inbound is working.

The token is stored encrypted (`EncryptedJSONField`) and is never displayed
again. It is the entire credential: anyone holding it can read every message
sent to the bot and send as the bot. To rotate it, revoke it with BotFather and
reconnect.

**Rotating the webhook secret** (Settings → Channels → the connection → Rotate
secret) re-runs `setWebhook` for you, because Telegram holds the secret over its
API rather than in a console you could paste into. The page after rotating says
whether that worked; if it did not, the channel rejects every delivery until you
rotate again.

Disconnecting calls `deleteWebhook`, so a removed bot stops delivering. If that
call fails — Telegram down, network gone — the connection is still removed and
the failure is logged; the alternative is an operator who cannot disconnect.

### The webhook has to be reachable

Telegram will only deliver to a **public HTTPS URL with a valid certificate**.
It will not deliver to `localhost`, to a private IP, to plain HTTP, or to a
self-signed certificate that it has not been given.

For local development, put a tunnel in front of the dev server and connect
through the tunnel's hostname. There is no "poll instead" mode: this project
uses webhooks only (SPEC §7.1).

Every workspace's bots share one URL, `/webhooks/telegram/`. The connection is
identified by the secret Telegram echoes in `X-Telegram-Bot-Api-Secret-Token`,
which is compared in constant time against the stored value. A delivery with a
wrong or missing secret gets a 403, exactly like a delivery naming a connection
that does not exist — the two are indistinguishable on purpose.

## What arrives

`allowed_updates` is set to `message` and `callback_query`. Everything else is
refused at Telegram's end rather than delivered and discarded.

| What the contact does | Event | Notes |
|---|---|---|
| Sends text | `message` | |
| Sends a photo, audio, voice note, video, video note, animation, document or sticker | `message` | `file_id`s land in `payload.media_ids`; the caption becomes the text |
| Shares a contact or a location | `message` | Rendered as a sentence — SPEC §7.2 has no richer field |
| Presses an inline button | `postback` | `payload.button_id` from `callback_data`; the button's spinner is cleared by a queued `answerCallbackQuery`, not inline, so it stays off the webhook's ack path |
| Opens `t.me/<bot>?start=<ref>` | `referral` | `payload.ref` — SPEC §10's Ref URL trigger |
| Sends a bare `/start` | `message` | `payload.extra["command"] == "start"` — see below |

**Bare `/start` is a message, not its own event type.** SPEC §10 lists *welcome*
as a trigger type, and there is no `welcome` member of `EventType` — the contact
really did send a message. The adapter flags it in `payload.extra` so the
welcome trigger keys off a flag rather than string-matching `/start`.

`update_id` is the deduplication key, so Telegram's own retries are recognised
and skipped (SPEC §7.1 step 2).

Media is **not** fetched server-side. A Telegram `file_id` is not a URL, and
turning it into one requires a `getFile` call that returns a link expiring in an
hour; storing the id and resolving it on demand is the honest shape, and it also
keeps the adapter clear of SECURITY-BASELINE §6.

## What goes out

The flow engine renders one abstract message and
`apps.channels.downgrade.downgrade` adapts it before the adapter sees it, so
these limits are enforced in one place rather than per platform.

| Limit | Value | Where it is applied |
|---|---|---|
| Message text | 4096 characters | Downgrade renderer, from `capabilities.max_text_len` |
| Media caption | 1024 characters | The adapter — an over-long caption is sent as a following message rather than truncated |
| `callback_data` | 64 **bytes** | The adapter |
| Inline buttons | 10 | `capabilities.max_buttons`; Telegram documents no hard cap, this keeps a keyboard usable |

Buttons become an **inline keyboard**, one button per row. A postback button's
`callback_data` is `node_id:button_id` (SPEC §6.2). The colon is always present,
even for a message with no node behind it — an inbox reply or an API send
encodes as `:button_id` — so the decoding is unambiguous whatever a button id
contains. When the two ids together exceed 64 bytes the node id is dropped,
because the button id is the half the engine matches on. Quick replies alone become a **one-time reply keyboard**; when a
message has both, the quick replies join the inline keyboard, since Telegram
allows only one `reply_markup` per message and a quick reply comes back as a
`button_id` either way.

Buttons beyond the tenth are appended to the text as numbered options ("Reply 1
for ..."), and the number is matchable: the sending node records the same
mapping in its wait config, so a contact who types "1" takes the branch they
were offered.

Cards and carousels have no native form here, so the downgrade renderer turns
them into image + text messages before the adapter runs.

One abstract message can therefore become several Bot API calls, and that
sequence is **not atomic**. If the third of three fails, the first two have
arrived, and the retry sends all three again — idempotency is keyed on the
message row (SPEC §9.4) and there is one row. The contact sees a duplicate
rather than a gap, which is the right direction to fail in for a message the
flow author meant to send; making it exact needs per-part progress on the
message row, which is a decision for every adapter rather than this one.

Media is addressed by URL or by `file_id` in the same field — Telegram accepts
both. A media-library asset arrives as its signed delivery URL, minted upstream
at send time.

## Rate limits

Telegram publishes roughly **1 message per second per chat** and roughly **30
per second overall**. Neither is enforced by a throttle in the adapter, and that
is deliberate:

- **Global**: the connection's token bucket (`apps.messaging.buckets`), refilled
  at `rate_default = 25.0` from `apps.channels.policy` — a little under
  Telegram's ceiling.
- **Per chat**: already guaranteed by the shape of the system. SPEC §9.6
  serialises everything one contact does behind an advisory lock and SPEC §9.2
  allows one live execution per contact, so two messages to the same chat are
  never in flight at once. A sleep in the adapter would be held *inside* that
  lock — it would lengthen the lock, not make sends safer.

When Telegram throttles anyway it answers **429** with `retry_after`, in the
`Retry-After` header or as `parameters.retry_after` in the body. Both are read,
and the send pipeline reschedules the message for that many seconds rather than
failing it.

A **403** means the contact blocked the bot, deleted their account, or the chat
is gone. All three mean "never send here again", so the adapter records an
opt-out for that identity and the message fails permanently.

## No messaging window

Telegram has none: a bot may message a contact at any time, and
`policy.window_hours` is `None`. The one gate is that the contact must have
messaged the bot at least once, which the compliance engine enforces as
`identity.opt_in` — set when their first inbound event is persisted. Broadcasts
are allowed.

## Testing a draft: "Test on Telegram"

SPEC §16's preview. In the flow builder, **Test on Telegram** asks the server for
a deep link, and opening it runs the flow's **draft** version against your own
chat with the bot.

- The link looks like `t.me/<bot>?start=preview-<handle>` and expires in 15
  minutes. Press the button again for a new one.
- The first chat to open it claims it. Nobody else's chat can use it afterwards,
  so two people testing at once do not collide.
- The resulting execution is flagged `preview`, and analytics exclude it — a
  few test runs cannot move a flow's reported numbers.
- Starting a preview **supersedes** whatever else the contact was running, which
  is the same rule every flow start follows (SPEC §9.2).
- An expired, tampered or already-claimed link does nothing at all: no preview,
  no error, no reply. There is nothing to tell an outsider apart from a `/start`
  payload that was never a preview link.

Requires a connected Telegram bot; the button says so and links to the connect
page when there is none.

**Why a handle and not a signed token.** SECURITY-BASELINE §4 puts every
unauthenticated token route on the shared signing utility, and issue #12 was
written as `?start=preview-<signed token>`. Telegram makes that impossible: a
deep-link `start` payload is capped at 64 characters and restricted to
`[A-Za-z0-9_-]`, while a `django.core.signing` token is longer and contains `:`
and `.`. The link carries 192 bits of `secrets.token_urlsafe` instead, and
`FlowPreviewLink` holds its HMAC — a fixed-length equality lookup, keyed on
`SECRET_KEY` — plus the expiry. Expiry, constant-time verification, generic
failure and unguessability all survive; only the container changed.

## Out of scope in v1

Groups and channels, inline mode, Telegram Payments, Telegram Stars, Web Apps,
message editing and deletion, and polls. `allowed_updates` reflects that.
