# SMS (Twilio)

Bring your own Twilio account. The number, the messages and the bill are yours;
BrightBean Chat holds the credentials, sends through the API, and hard-codes the
carrier compliance that is not optional.

Specification: `docs/SPEC.md` §6.6 (the channel), §11.9 (the `send_sms` node),
§19 (opt-out enforcement). Implementation:
`apps/channels/providers/sms.py`, `apps/channels/sms_compliance.py`,
`apps/channels/segments.py`, `apps/flows/engine/nodes/send_sms.py`.

## Connecting a number

1. **Get the credentials.** Open the [Twilio console](https://console.twilio.com/)
   and copy the **Account SID** (`AC…`) and the **Auth Token** from the
   dashboard.

2. **Decide what you send from.** Either a phone number you own, in E.164 form
   (`+15551234567`), or a **Messaging Service SID** (`MG…`). A messaging service
   is the right answer if you send at volume — it owns the number pool, the
   sticky sender and the A2P campaign — and a bare number is fine otherwise.
   Give one or the other; Twilio's API accepts one and rejects both.

3. **Paste them** into *Settings → Channels → SMS → set it up*
   (`/w/<workspace>/settings/channels/sms/connect/`).

   The app fetches the account to check the credentials, then fetches the number
   or the messaging service to check it belongs to that account. Nothing is
   stored until both succeed, so a wrong credential leaves no trace — and a
   number you do not own is caught now rather than on your first send.

4. **Paste the webhook URL back into Twilio.** The connection's page
   (*Settings → Channels →* the connection) shows it:

   ```
   https://<your deployment>/webhooks/sms/<connection id>/
   ```

   In the Twilio console, open the number (Phone Numbers → Manage → Active
   numbers → your number) or the messaging service (Messaging → Services → your
   service → Integration), and set **A message comes in** to that URL with the
   method **HTTP POST**.

   You do not need to set a status-callback URL by hand. Every message this app
   sends carries `StatusCallback` pointing at the same URL, which is how
   delivery receipts get back.

5. **Text the number.** The channels list shows a *Last event* column; when it
   moves from "Nothing received yet" to a timestamp, inbound is working.

The auth token is stored encrypted (`EncryptedJSONField`) and never displayed
again. It is the entire credential: anyone holding it can send as your number
and read your messages. To rotate it, roll it in the Twilio console and
reconnect.

SMS is not offered in the generic "Add a channel" form. That form creates a
connection row and nothing else, which here would be a number with no
credentials whose every send fails; the guided setup above is the only way in.

### The URL has to match exactly

Twilio signs every delivery with `X-Twilio-Signature`, an HMAC-SHA1 over **the
URL it was configured with** plus the POST parameters. A URL that differs by one
character — `http` for `https`, a missing trailing slash, a different host —
produces a different signature and every delivery is refused with a 403.

Two consequences worth knowing:

* **Set `APP_URL`.** The app builds the URL it shows you, the `StatusCallback` it
  sends, and the URL it verifies against from that one setting. If `APP_URL` does
  not match what you pasted into Twilio, nothing verifies.
* **Behind a reverse proxy**, the request the application sees is not the request
  Twilio made — the proxy terminates TLS and rewrites the host. `APP_URL` is
  checked first and is normally the answer. A proxy-declared host
  (`X-Forwarded-Host` / `X-Forwarded-Proto`) is honoured **only** when the peer
  is listed in `TRUSTED_PROXIES`, because that header is otherwise something any
  caller can set — and a caller who could choose the host could choose the string
  their forged signature was computed over.

The URL also has to be reachable from the public internet over HTTPS. For local
development, put a tunnel in front of the dev server, set `APP_URL` to the
tunnel's address, and paste that into Twilio.

## STOP, HELP and START

These are carrier requirements, not features. They are handled in core, **before
trigger matching**, and no flow can intercept or bypass them (SPEC §19).

| The contact texts | What happens |
|---|---|
| `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT` | The identity is suppressed immediately; a confirmation goes out; every later send to that number is refused with `opted_out`. |
| `HELP` | The help text is sent — **even if they have unsubscribed**. |
| `START`, `UNSTOP` | Consent is restored, with the moment and the reason recorded, and a confirmation goes out. |

Matching is case-insensitive on the trimmed message, and on the **whole**
message. "STOP" unsubscribes; "please stop sending these on Sundays" does not.
That is deliberate: only the contact can undo a suppression, and they have not
been told how, so a substring match would be an unrecoverable mistake.

All three keywords land in the conversation as ordinary inbound messages, so an
agent reading the thread sees what the contact wrote and the reply it produced.
None of them starts a flow: they are consumed before trigger matching, so a
keyword trigger on the word "STOP" cannot fire at somebody who just
unsubscribed.

The three replies are configurable at *Settings → Channels → SMS settings*
(`/w/<workspace>/settings/channels/sms/settings/`). Emptying a box restores the
default wording rather than sending nothing — the replies themselves cannot be
switched off.

Twilio also has its own opt-out handling (Advanced Opt-Out) and its own
suppression list. The two are complementary: if Twilio refuses a send with error
`21610` ("unsubscribed recipient") — because the contact opted out through
another application on the same number, or before this workspace existed — the
adapter records that as an opt-out here too, so the two lists converge.

### A2P 10DLC

US traffic sent from a 10-digit long code has to be registered: a **brand**, then
a **campaign**, with the number linked to it. Unregistered traffic is filtered or
blocked by the carriers, not by this app.

All of it happens in the Twilio console. BrightBean Chat surfaces a checklist on
the SMS settings page and nothing more — it does not perform, verify or monitor
registration, and ticking the boxes records only that you did it. Toll-free
verification and short codes are out of scope.

## What SMS can carry

| | |
|---|---|
| Text | Yes, up to 1600 characters (Twilio's concatenated ceiling) |
| Images (MMS) | Outbound yes; inbound recorded but not yet viewable (see below) |
| Audio, video, files | No |
| Buttons, quick replies | No — they become numbered options in the text |
| Messaging window | None. Opt-out is the only gate. |
| Send rate | 1 per second per connection, Twilio's long-code throughput |

**Inbound MMS is recorded, not yet rendered.** Twilio delivers picture messages
as `MediaUrl` values pointing at a REST resource under your account, which is not
a link a browser can follow: on an account with authenticated media it answers
401 to anyone without the Account SID and Auth Token, and on an account without
it, it answers to *anyone at all* — a contact's picture messages behind a URL we
would be handing out. So they are stored as media identifiers rather than as
attachments, which is the same call the Telegram adapter makes about its
`file_id`s, and neither channel resolves them yet. The message text arrives
normally; the picture is on the message row and needs a credentialed fetch to
display. Resolving them for both channels is its own piece of work.

A flow with buttons still works on SMS. The shared downgrade renderer
(`apps/channels/downgrade.py`) appends "Reply 1 for …" to the message, and a
contact who texts back `2` resumes the waiting node exactly as a button press
would. Nothing in the SMS adapter implements that; it declares what it can carry
and the renderer does the rest.

## Cost, and the segment counter

An SMS is billed per **segment**, not per message, and how many segments a
message costs depends on an encoding decision you never make explicitly:

* If every character fits the **GSM-7** alphabet, a single segment holds **160**
  characters.
* One character outside it — a curly quote pasted from a word processor, an em
  dash, an emoji — re-encodes the whole message as **UCS-2**, where a segment
  holds **70**.
* A message that does not fit one segment is split, and each part loses room to
  the concatenation header: **153** and **67**. So 161 GSM-7 characters cost two
  segments, not one and a bit.
* Ten characters cost two septets each in GSM-7: `^ { } \ [ ~ ] |` and `€`.

The composer and the Send SMS panel show this live, from
`apps/channels/segments.py`. Optionally set a **price per segment** on the SMS
settings page and the preview will estimate what a message costs to send.

That figure is a hint and nothing else. BrightBean Chat never meters or bills —
the same position SPEC §6.5 takes for WhatsApp. Your Twilio invoice is the real
number, and prices differ per destination and per campaign.

## The `send_sms` node

Sends a text message to a contact's phone identity, from anywhere in a flow —
including a flow running on another channel. A contact who started in a Telegram
chat and reaches a `send_sms` gets a text.

Config: `text` (with `{{placeholders}}`) and an optional `media_url`.
Handles: `default` and `error`.

It follows `error` when the workspace has no active SMS connection, when the
contact has no phone identity on it, or when the send is refused — which is what
an opted-out contact produces. It never creates an identity from `contact.phone`:
a number typed into a CRM field is not consent to text it, and fabricating one
would route straight past the compliance engine.

The node always runs in the worker, never inline in a webhook request
(`synchronous_safe = False`).

## Troubleshooting

**Every delivery is 403.** The URL in Twilio does not match what the app
verifies against. Check `APP_URL`, check the trailing slash, and check `https`
vs `http`. Behind a proxy, check `TRUSTED_PROXIES`.

**Deliveries stop after a while.** Too many refused signatures in a row bans the
source for `WEBHOOK_SIGNATURE_BAN_SECONDS`; fix the URL and wait it out.

**A send fails with `provider_rejected:21610`.** Twilio's own opt-out list has
this number. The contact has to text `START` to the number to come back.

**A send fails with `opted_out`.** Ours does. Same answer.

**A send fails with `no_identity`.** The contact has no phone number on this
channel. An identity is created when they text you, or by a `data_collection`
node that asks for a phone number and records the consent.

**Messages queue instead of sending.** The connection's token bucket is at one
per second. The worker drains the backlog; if there is no worker running, nothing
does (see `docs/SPEC.md` §20 on degraded mode).
