# Inbound media

How a picture a contact sent reaches a team member's screen.

Implementation: `apps/channels/media.py` (the shared path),
`Adapter.media_source` in `apps/channels/providers/base.py` (the per-platform
half), `apps.inbox.views.media` (the route).

## Two kinds of inbound media

`EventPayload` has two fields for it, and the line between them is **who can
fetch it** — decided by the adapter, at parse time.

| Field | Meaning | Stored as | Rendered as |
|---|---|---|---|
| `attachments` | Addresses that resolve for anyone, with no credential of ours | `{"type": "file", "url": …}` | A link the reader's own browser follows |
| `media_ids` | Identifiers that need *this connection's* credentials | `{"type": "media", "media_id": …, "media_kind": …}` | An `<img>` or a labelled link, at our own route |

Nothing fetches an `attachments` URL server-side. There is no reason to: the
reader can already reach it, and fetching a stranger's URL from inside the
deployment is what SECURITY-BASELINE §6's guard exists to contain.

A `media_ids` entry is the opposite case, and both shipped channels are in it:

* **Telegram** hands out a `file_id`. It becomes a URL only after a `getFile`
  call, and the URL it becomes expires in an hour and carries the bot token in
  its path.
* **Twilio** hands out a `MediaUrl` addressing a REST resource under the
  account. With authenticated media enabled it answers 401 to anyone without the
  Account SID and Auth Token — which is every browser rendering a thread. With
  it disabled the URL answers to *anyone at all*, which would be a contact's
  picture messages behind a link we handed out. Neither is an attachment.

If a platform's media URL needs a signature, a session or an account credential,
it is `media_ids`, whatever it looks like.

## The path

```
Message.body block  →  inbox:media route  →  Adapter.media_source  →  guarded_request  →  sniff  →  response
   {"type": "media",     workspace-scoped,     "where, and with        SSRF guard,        magic     §9 rules
    "media_id": …}       session required       what headers"          size cap           bytes
```

1. **Ingest** writes the identifier into the message body. `apps.messaging.ingest`
   never writes a URL for one, and never fetches anything.
2. **Rendering** (`apps.inbox.rendering`) turns the block into a link to
   `inbox:media`, addressing the message row and the block's position. The
   identifier itself never reaches the page.
3. **The route** reads the identifier back out of the stored row and calls
   `apps.channels.media.fetch_media`.
4. **The adapter** says where to fetch and with what headers, doing whatever
   platform call that takes.
5. **The shared fetch** makes one `guarded_request`, sniffs the bytes, and
   serves them.

## The time budget

This path runs on a **web worker**, and that is the constraint everything else
bends around. `Procfile` and `Dockerfile` both start
`gunicorn --workers 2 --threads 2` with no `--timeout`, so a deployment has four
concurrent request slots and gunicorn's default 30-second worker timeout. A
request that outlives that budget is not slow — it is a SIGKILL that takes every
other request on that worker with it.

| Step | Budget | Constant |
|---|---|---|
| Resolve (the adapter's own call, e.g. `getFile`) | 5 s | `MEDIA_RESOLVE_TIMEOUT` |
| Download (`guarded_request`) | 10 s | `MEDIA_DOWNLOAD_TIMEOUT` |
| **Worst case** | **15 s** | half of gunicorn's 30 s |

Adapters import `MEDIA_RESOLVE_TIMEOUT` rather than choosing a timeout, so the
sum stays true when a second platform lands, and
`test_media.py::TestTheTimeBudget` asserts the arithmetic. `BACKGROUND_TIMEOUT`
(30 s) is emphatically *not* the right budget here: it is for work nobody is
waiting on, and on its own it exceeds the worker timeout.

`INBOUND_MEDIA_MAX_BYTES` (default 16 MB) is the matching size bound, and it is
its own setting rather than a hard-coded argument for a reason worth stating:
passing `max_bytes=` to the guard *overrides*
`EXTERNAL_REQUEST_MAX_RESPONSE_BYTES`, so a number written at the call site
would be a cap the operator could neither see nor lower. It is an allocation
bound, not a bandwidth one — the guard buffers what it reads, so this times the
number of concurrent readers is memory the web process will hold.

## Five decisions worth knowing about

**The fetch goes through the SSRF guard, always.** SECURITY-BASELINE §6 names
"media fetch-by-URL" explicitly. The sibling helper,
`providers.base.request_json`, is for URLs an adapter builds from constants and
stored ids — a Twilio `MediaUrl` arrives in a webhook body, and a Telegram file
path arrives in an API response, so neither is that case. §6 settles the
remainder: "a call site that cannot tell which of the two it is wants the
guard." The platform *API* call an adapter makes to resolve an id (Telegram's
`getFile`) is the other case and keeps using `request_json`.

**The content type is sniffed, never believed.** SECURITY-BASELINE §9's rules
are about anything this deployment serves from its own origin, and inbound media
is the more hostile of the two sources — an upload came from a team member, this
came from a stranger. `apps.media_library.mimes` does the sniffing, the same
allowlist the upload path uses. Only safe image types are served `inline`;
everything else is `Content-Disposition: attachment`, and every response carries
`X-Content-Type-Options: nosniff`. Bytes the sniffer does not recognise are
served as `application/octet-stream` attachments rather than hidden: a thread
with a hole in it is worse for the reader than a download link.

**There is no signed token.** The obvious precedent is
`apps/media_library/delivery.py`, whose `/m/<token>/` route mints a signed,
unguessable, never-expiring token — because, in its own words, "a platform
fetching an image has no session and no workspace." The reader here is the
opposite: a team member's browser, on our own origin, with a session and a
workspace. So `inbox:media` is an ordinary authenticated, workspace-scoped view
and SECURITY-BASELINE §4, which governs *unauthenticated* token routes, does not
reach it. A membership check is a stronger control than a replayable URL, and
reading the identifier out of the row is a stronger provenance claim than "we
signed this once" — it is what stops the route from becoming a way to ask a
connection's credentials for an arbitrary identifier.

**Nothing is cached server-side, so the route is conditional instead.** Every
fetch that happens is a live platform call holding one of four request slots.
The bytes behind a given `(message, block index)` never change — an inbound row
is not rewritten — so `inbox:media` tags each response with an ETag over the row,
the position and the identifier, and answers a revalidation with **304 before
resolving anything at all**. With `Cache-Control: private, max-age=3600` and a
URL that is stable across renders, a reader scrolling back through a thread pays
for each attachment once rather than once per render.

`apps.common.polling.conditional` is deliberately not reused for this: it forces
`Cache-Control: no-store`, which is right for a JavaScript-driven poller and
wrong for an `<img>` that should sit in the browser cache. The route composes
the same two lower-level helpers (`version_etag`, `if_none_match`) under its own
policy. Resolving into the media library instead is still the better answer for
a busy deployment, and is deliberately not this change.

**The platform's word picks the tag; the bytes decide everything else.** A
`media_ids` entry can be accompanied by `media_kinds` — what the platform called
it (`image`/`audio`/`video`/`file`). That claim decides one thing: whether the
inbox bets on an `<img>` or renders a labelled link. Without it every voice note
and PDF showed a broken-image icon. It is not in tension with "sniff, do not
trust": the `Content-Type` and the disposition on the bytes are still decided by
reading them, so a platform that lies gets a wrong-looking tag, never an inline
render of something that should have been an attachment. The two tuples are
aligned by the adapter, and consumers must tolerate them being out of step —
`apps.messaging.ingest` treats a missing entry as unknown rather than raising,
because a rendering nuisance must not become a lost message.

## Adding it to a new adapter

Override one method. The default returns `None`, so a platform that never fills
`media_ids` writes nothing.

```python
def media_source(self, connection, media_id):
    return MediaSource(
        url=media_id,  # or whatever resolving takes
        headers=(("Authorization", _basic_auth(connection)),),
    )
```

Fill `EventPayload.media_kinds` alongside `media_ids` while you are there —
positionally aligned, one entry per id — so the inbox can pick a tag. Twilio's
`MediaContentType0` is the field for it. Classify from the *payload*, not from
the field name: Telegram's `sticker` key carries WebP, WebM and TGS depending on
`is_video`/`is_animated`, and calling all three "image" is how a broken-image
icon gets into a thread.

Four rules:

* Return a `MediaSource`, **not** the bytes. An adapter that fetches media itself
  has stepped outside SECURITY-BASELINE §6's single call site.
* Put credentials in `headers`, never in the URL's userinfo — the guard refuses
  that outright, and the guard drops `Authorization` when a redirect crosses an
  origin, which is what stops a platform redirecting media to a CDN from walking
  the credential off-site.
* Return `None` rather than raising for an ordinary platform refusal. An
  identifier the platform has since expired is a 404 in a months-old thread, not
  an incident. (`fetch_media` catches anything you raise anyway and turns it
  into the same 404 — the endpoint's contract is that it never 500s — but a
  raise you meant to handle is a stack trace in someone's logs.)
* Use `MEDIA_RESOLVE_TIMEOUT` for any HTTP call you make here. See the time
  budget above for why picking your own number breaks something.
