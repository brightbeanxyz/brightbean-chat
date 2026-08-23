# WhatsApp

WhatsApp via Meta's **Cloud API**, direct — no Business Solution Provider in
between. It is the most involved channel to set up and the only one where
sending outside a 24-hour window costs money and needs Meta's prior approval of
every message.

Specification: `docs/SPEC.md` §6.5 (the channel), §8 (the compliance rules) and
§15 (template status polling).
Implementation: `apps/channels/providers/whatsapp.py` (the adapter) and
`apps/channels/whatsapp_templates.py` (templates).

## Connecting a number

You need a Meta app, a WhatsApp Business Account (WABA) with a phone number on
it, and a permanent system user token. There is no OAuth step: this is the
direct integration, so the credential is one you generate yourself.

1. **Create a Meta app** at [developers.facebook.com](https://developers.facebook.com)
   and add the **WhatsApp** product. Note the app id and app secret.

2. **Add a phone number** under WhatsApp → API Setup, and complete its
   verification. Copy the **phone number ID** — the numeric API id, *not* the
   phone number itself — and the **WhatsApp Business Account ID**.

3. **Create a system user** in Business Settings, give it access to the WABA,
   and generate a token with `whatsapp_business_messaging` and
   `whatsapp_business_management`. Choose **never expires**; a 60-day token
   turns into a silent outage two months after launch.

4. **Set the app credentials** on this deployment. Both are needed:

   ```
   PLATFORM_WHATSAPP_APP_ID=...
   PLATFORM_WHATSAPP_APP_SECRET=...
   PLATFORM_WHATSAPP_VERIFY_TOKEN=...   # any random string you also paste into Meta
   ```

   The credential chain only uses a level that is **complete** (`docs/SPEC.md`
   §4), so setting the secret without the id leaves the deployment with no
   credentials at all and every delivery failing signature verification. An
   organization can override both in the admin instead.

5. **Paste the ids and the token** into *Settings → Channels → WhatsApp → set it
   up* (`/w/<workspace>/settings/channels/whatsapp/connect/`).

   The app reads the phone number back from Meta to prove the token and to learn
   how the number displays, creates the connection, and subscribes the app to
   the WABA's webhooks. Nothing is stored until that first call succeeds, and
   the connection is removed again if the subscription fails — a number Meta
   will not deliver for is not a connection.

The token is stored encrypted (`EncryptedJSONField`) and never displayed again.
It is the entire credential: anyone holding it can read every message the number
receives and send as the business. To rotate it, revoke it in Business Settings
and reconnect.

WhatsApp is not offered in the generic "Add a channel" form. That form creates a
connection row and nothing else, which here would be a number whose every send
fails; the guided setup above is the only way in.

### The webhook

Every WhatsApp connection in a deployment shares one URL:

```
https://<your-host>/webhooks/whatsapp/
```

In the Meta app, under **WhatsApp → Configuration**, set that as the callback
URL, set the verify token to match `PLATFORM_WHATSAPP_VERIFY_TOKEN`, and
subscribe to the **`messages`** field. Meta answers the subscription check with
a `GET` carrying `hub.challenge`, which this deployment echoes only when the
verify token matches.

The URL has to be reachable from the public internet over HTTPS with a valid
certificate. For local development, put a tunnel in front of the dev server and
configure the tunnel's hostname.

Deliveries are authenticated by **`X-Hub-Signature-256`**, an HMAC-SHA256 of the
raw request body under the *app secret*. It is verified before the body is
parsed, in constant time, and a failure is indistinguishable from an unknown
number — both answer `403`. Which connection a delivery is for comes from
`metadata.phone_number_id` inside the payload, matched against the connection's
account identifier.

## What arrives

| WhatsApp | Becomes |
|---|---|
| `text` | `message` with `payload.text` |
| `image`, `video`, `audio`, `voice`, `document`, `sticker` | `message` with `payload.media_ids` and the caption as text |
| `interactive.button_reply` (reply buttons) | `postback` with `payload.button_id` |
| `interactive.list_reply` (list rows) | `postback` with `payload.button_id` |
| `button` (a template's quick reply) | `postback`, with no node prefix |
| `location`, `contacts` | `message`, as the sentence a person would have typed |
| `statuses[]` | `delivery_status`, updating the message it names |
| `reaction`, `order`, `system`, anything else | dropped |

A media **id** is carried rather than a URL. Resolving it needs a second Graph
call and produces a link that expires, so it is resolved on demand instead.

A contact is identified by their `wa_id`, which is an E.164 number. It is stored
as `+` plus that number, which is what lets a WhatsApp contact link to one
already captured over SMS or in the CRM (`apps/messaging/identities.py`).

Everything a delivery carries — message text, profile names, Meta's own error
prose — is attacker-controlled and is escaped on render, never trusted.

## What goes out

Inside the 24-hour window, ordinary **session messages**:

| Abstract block | On the wire |
|---|---|
| text | `type: text`, split at 4096 characters |
| image / video / audio / file | `type: image` / `video` / `audio` / `document` with a `link` |
| up to 3 buttons | `interactive` `type: button` — reply buttons |
| up to 10 quick replies | `interactive` `type: list` — a list with 10 rows |
| a 4th button, a URL button, a card, a gallery | numbered text options, produced by the shared downgrade renderer |

Link previews are off. Turning them on would make Meta fetch whatever URL a flow
author put in the text and render a card from it.

An interactive message's body caps at **1024** characters where a text message
caps at 4096, so a long message with buttons goes out as ordinary text followed
by the interactive part, in order, rather than being truncated.

Outside the window, a **template message** and nothing else.

## The 24-hour window

The window opens when a contact messages the number and closes 24 hours after
their last message. Inside it, anything may be sent. Outside it:

* a flow's `send_message` node fails with `needs_template`, follows its `default`
  edge, and leaves the reason on the message row — it is never silently dropped
  (SPEC §8);
* the same send carrying an approved template goes out normally;
* there is **no** human-agent extension. An inbox reply outside the window needs
  a template exactly like an automation does. (Instagram and Messenger have a
  7-day agent allowance; WhatsApp does not.)

None of this is WhatsApp-specific code. It is one row in
`apps/channels/policy.py` — `window_hours=24, outside_window="needs_template"` —
read as data by the one compliance engine.

If a send slips through and Meta refuses it asynchronously, error **131047**
("re-engagement message") comes back on a failed status and lands on the message
as the same `needs_template` code, so the two paths read alike.

## Templates

*Settings → Channels → WhatsApp → Message templates.*

A template is a message Meta reviews and approves in advance. Its changing parts
are numbered placeholders — `{{1}}`, `{{2}}` — numbered **per section**, so a
header's `{{1}}` and a body's `{{1}}` are two different values.

Write one, save it as a draft, and submit it. The live preview shows exactly
what a contact will see, rendered by the same substitution the send path uses.

**Category decides both price and scrutiny:**

| Category | For | Review |
|---|---|---|
| Utility | Order updates, appointment reminders, account notices | Usually quick |
| Authentication | One-time passcodes | Strict; Meta enforces its own format |
| Marketing | Anything promotional | Strictest, and the most expensive to send |

**Review realities.** Most templates come back within minutes; some take a day.
BrightBean Chat polls Meta hourly and notifies workspace admins when the answer
arrives. A rejected template shows Meta's own reason (commonly
`INVALID_FORMAT`, `ABUSIVE_CONTENT` or `INCORRECT_CATEGORY`) and can be edited
and resubmitted. A template that is approved and later **paused** for quality is
shown as not sendable, with `PAUSED` as the reason — it cannot be sent while
Meta has it paused, and failing closed is the only safe direction for something
a compliance rule depends on.

An approved or in-review template cannot be edited here: the copy under review
is the one Meta holds, and editing the local row would leave the two disagreeing
with nothing to say which is live. Delete it and write a new one.

Deleting removes it at Meta first, then locally. If Meta's delete fails, the
local row still goes and the failure is logged — an operator who cannot remove a
template from the product has no other way to fix it.

## Pricing

Meta bills you directly, per conversation, at rates that depend on category and
on the recipient's country. **BrightBean Chat never meters and never charges.**

*Settings → Channels → WhatsApp → Cost estimates* holds a per-category table you
fill in from your own rate card. Those numbers are shown beside a template and
in the broadcast composer so a send to ten thousand people does not come as a
surprise. Nothing in the product adds them up, stores them per message, or
refuses a send because of them (SPEC §22).

There is deliberately no live pricing lookup. A number this product fetched
would be wrong in a way that looked authoritative; a number you typed is wrong
in a way you can see.

## Limits

| | |
|---|---|
| Text body | 4096 characters |
| Interactive body | 1024 characters |
| Reply buttons | 3, titles 20 characters |
| List rows | 10, titles 24 characters |
| Media caption | 1024 characters (audio carries none) |
| Image | 5 MB |
| Audio, video | 16 MB |
| Document | 100 MB |
| Send rate | 20/second by default (`rate_default` in `apps/channels/policy.py`) |

The media ceilings are also what the media library warns against when you pick
an asset with WhatsApp selected — advisory only, since a library asset has no
destination until send time.

Meta additionally applies a **messaging limit** per number (1 000, 10 000,
100 000 or unlimited unique contacts in 24 hours), which rises with quality and
verification. It is enforced at Meta, not here; a send refused for it comes back
as a failed status.

There is no throttle in the adapter. The global rate is the connection's token
bucket, and two messages to one contact cannot be in flight at once because
everything a contact does is serialised behind one lock (SPEC §9.6). When Meta
disagrees anyway it answers `429` and the send pipeline reschedules.

## Out of scope in v1

Embedded signup (the in-product onboarding flow), catalogs, commerce and
payments (SPEC §1.1), live pricing APIs, and WhatsApp Flows (Meta's native
in-chat forms).
