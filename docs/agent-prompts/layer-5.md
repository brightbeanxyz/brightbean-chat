# Layer 5 — Agent Prompts

Six workstreams, **all parallel**. Layer 4 is merged, so nothing here is blocked.

| Issue | | Owns | Permission key |
|---|---|---|---|
| [#17](https://github.com/brightbeanxyz/brightbean-chat/issues/17) | L5-A | `apps/channels/providers/instagram.py` | `manage_channels` |
| [#18](https://github.com/brightbeanxyz/brightbean-chat/issues/18) | L5-B | `apps/channels/providers/messenger.py` | `manage_channels` |
| [#19](https://github.com/brightbeanxyz/brightbean-chat/issues/19) | L5-C | `apps/channels/providers/whatsapp.py` + template management | `manage_channels` |
| [#20](https://github.com/brightbeanxyz/brightbean-chat/issues/20) | L5-D | `apps/channels/providers/sms.py` + `send_sms` node | `manage_channels` |
| [#21](https://github.com/brightbeanxyz/brightbean-chat/issues/21) | L5-E | `apps/channels/providers/email.py` + suppression | `manage_channels` |
| [#25](https://github.com/brightbeanxyz/brightbean-chat/issues/25) | L5-F | `apps/api/` — public REST v1 + outbound webhooks | `manage_api_keys` |

The **issue body is the scope**. This file records the Layer-4 seams each one plugs into, with real names. Layer 4 left most of them explicitly reserved and documented; finding them by reading source costs each agent a cycle.

---

## Ground rules

Everything in [`layer-2.md`](layer-2.md), [`layer-3.md`](layer-3.md) and [`layer-4.md`](layer-4.md) → "Ground rules" still binds: `WorkspaceScopedModel` refuses unscoped execution, `get_scoped_object_or_404`, the roles/decorators API, the nav registry, `requirements.in` → `make lock`, the `tenancy`/`other_tenancy` fixtures, the IDOR obligation, and the six CI jobs. Read those first — this file adds only what Layer 4 now exports.

### Telegram is the template, and it was written to be one

`apps/channels/providers/telegram.py` is the first real adapter and its module docstring says outright: *"A Layer-5 author copying this should be able to replace the helpers and keep the class."* Read it before writing a line. It separates the parts that are *about Telegram* (update shapes, method names, keyboard JSON, the 1024-char caption cap) from the parts that are *about being an adapter* (HTTP mechanics, timeout policy, `429` → `RateLimitError`, "never put a URL in an error message"). The second set is inherited from `apps/channels/providers/base.py` and must not be re-implemented.

Five things it establishes that every adapter inherits:

- **`Adapter` ABC** — `resolve_connection`, `verify_webhook`, `parse_events`, `send`, `send_typing`, `mark_seen`, and the optional `on_webhook_secret_rotated`. Use `base.request_json` and `providers/exceptions.py` rather than a bare `httpx` client.
- **No throttle in the adapter.** The global limit is the connection's token bucket (`apps/messaging/buckets.py`, configured by `rate_default` in `apps/channels/policy.py`). The per-recipient limit is satisfied by the shape of the system — SPEC §9.6 serialises everything a contact does behind one advisory lock. A second throttle would be a sleep held *inside* that lock. When the platform disagrees it says so with a `429` and a `retry_after`, and the send pipeline reschedules. Say this in your PR rather than adding a timer.
- **Block downgrading is `apps/channels/downgrade.py`**, shared. You declare capabilities; you do not write your own fallback ladder.
- **`Capabilities` is a frozen dataclass** and a module-level singleton. Layer-5 adapters *read* it; a mutable patch would reconfigure the whole deployment.
- **Secrets discipline (SECURITY-BASELINE §5).** Credentials live encrypted in `connection.credentials`. `base.request_json` reports the *host* of a failed call and never the path, and `apps/common/logging.py` scrubs known token shapes. If your platform's token has a recognisable shape, add it to that scrubber.

### `register_adapter` refuses duplicates — and your tests must too

`apps.channels.registry.register_adapter(platform, cls)` raises on a second registration for the same platform: contract 4 says one adapter per platform, and which one wins must not depend on import order.

This has a direct consequence for tests, and it has already cost the project a red `main` once. A test that swaps in a fake **must save and restore** the real adapter rather than registering over it and clearing the slot. Use the shared helper — `apps/channels/tests/fake_adapter.py::swapped_adapter` — and do not write a second copy of that dance. When #12 shipped the real Telegram adapter, two copies existed and only one got updated; the other took 31 tests down.

### The routing stages are a registry, not a switch

`apps/flows/triggers/hooks.py` defines contract 6's five stages in fixed order:

```
hard_optout → post_persist → resume → trigger → default_reply
```

`apps/flows/triggers/stages.py` is the worked example — its docstring says it is the file L5-D should read first.

**L5-D specifically:** a `hard_optout` hook **cannot write `identity.opted_out_at`**. Contract 3 gives that column exactly one write site, `apps/messaging/ingest.py`, and `apps/messaging/tests/test_write_sites.py` runs an AST scan that fails the build over a second one. Ingest already applies it from an `EventType.OPT_OUT` event, so the SMS adapter's job is to classify `STOP`/`UNSUBSCRIBE` as that event type in `parse_events`. The hook owns the confirmation reply and consuming the event — `stages.opt_out_event` already does the platform-agnostic half.

### The comment-trigger infrastructure is already built — for you

L4-A absorbed it deliberately so #17 and #18 would not each build it:

- `TriggerType.COMMENT` exists, and `apps/flows/triggers/types.py` already scopes it to `frozenset({Platform.INSTAGRAM, Platform.MESSENGER})`.
- `COMMENT_POST_ID_KEY = "post_id"`, `COMMENT_PARENT_ID_KEY = "parent_comment_id"`, `COMMENT_TEXT_KEY` — where a comment event carries its post, its parent and its body. `EventPayload` has `comment_id` but no post id; that is why these keys exist. An empty or absent `parent_id` means top-level.
- `apps.flows.models.HandledComment` plus `triggers/guards.py::may_claim_comment` / `record_comment` are the once-only guard, so the same comment cannot fire two flows.

Your job is to emit the right `NormalizedEvent`. Do not add a second comment model.

### Compliance is data, not branches

`apps/messaging/compliance.py::can_send` returns `Allowed | NeedsTemplate(reason) | NeedsTag(allowed_tags) | Blocked(reason)`. It reads your `PlatformPolicy` row from `apps/channels/policy.py` as **data**. Adding a platform branch inside `apps/messaging/` is the failure mode this design exists to prevent — Telegram has none, and `grep -rn telegram apps/messaging/` returning only a migration's choices list and a docstring example is the standard to match.

- **L5-C (WhatsApp):** `NeedsTemplate` is your verdict outside the 24-hour window. Template management is yours; the *decision* to require one is the policy row plus the compliance engine.
- **L5-B (Messenger):** `NeedsTag` carries `allowed_use_text`, which is Meta's own description of what those tags may be used for. Pass it through verbatim (SPEC §6.4) — the inbox already renders it.

### Every outbound HTTP call goes through the guard

`apps/common/outbound.py::guarded_request` is the SSRF-guarded client L4-E built (SECURITY-BASELINE §6). It resolves DNS **once** and pins the connection to the literal address, validates every resolved address before pinning, re-validates each redirect hop, refuses non-`http(s)` schemes and URLs carrying userinfo, caps the response body and the redirect count, and enforces a total deadline rather than a per-read timeout.

Use it for anything whose URL a *user* can influence — a webhook target in #25, a media URL you fetch. Do **not** use it for calls to a fixed platform API host you control in code; those go through `base.request_json`, which is where the retry and error-mapping policy lives. If you are unsure which applies, the test is whether a flow author can change the destination.

### Public token routes have exactly one implementation

`apps/common/signing.py` — `sign(payload, purpose=...)` / `unsign_or_404(...)`. The `purpose` is the signer salt, so a token minted for one route cannot be replayed against another; every rejection is an indistinguishable bare 404.

**L5-E:** the unsubscribe link is the canonical case and the module docstring names it. Tokens minted with `max_age=None` never expire — unsubscribe links sit in inboxes forever, and an unsubscribe link that 404s is a compliance problem, not a broken link. Keep the old payload version accepted when you change the shape.

### The IDOR suite discovers your routes whether you add them or not

`tests/idor.py::iter_tenant_routes` walks the URL conf and **raises `UnregisteredRouteKwargError`** for any route carrying a kwarg it has no resolver for. You cannot quietly escape it. Either register a resolver in `TENANT_KWARG_RESOLVERS` / `NEUTRAL_KWARG_VALUES`, or add a `WAIVED_ROUTES` entry with a written reason — there are three today, each cross-referencing the test class that stands in for the sweep.

**L5-F:** an API authenticated by a key rather than a session is exactly the case that needs a stated position here. Decide it deliberately and write it down.

### Connect flows

`apps/channels/registry.py::CONNECT_ROUTES` maps a platform to its guided-connect route; today it holds Telegram alone. Add yours. Telegram is the cheapest possible case (one BotFather token, no OAuth) — the Meta platforms need an OAuth dance and app review, so budget for it and keep the credential exchange out of the adapter module proper.

---

## Trigger — #17 (L5-A, Instagram)

````
Implement issue #17 in brightbeanxyz/brightbean-chat: [L5-A] Instagram channel — DMs, comment-to-DM, story mentions/replies, follow trigger.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-5.md — its "Ground rules" name the exact Layer-4 seams you plug into; layer-2/3/4 ground rules still bind. Also docs/SPEC.md §6.1 and §6.4, docs/ROADMAP.md contract 4, docs/SECURITY-BASELINE.md §§2, 4 and 5, and CONTRIBUTING.md. Adapter issues get a dedicated security review at PR time.

Specifics for you:
- apps/channels/providers/telegram.py is your template and says so in its own docstring. Replace the helpers, keep the class. Do not re-implement HTTP mechanics, timeout policy or 429 handling — those are in providers/base.py.
- The comment-trigger infrastructure is ALREADY BUILT and already scoped to Instagram. TriggerType.COMMENT exists; COMMENT_POST_ID_KEY, COMMENT_PARENT_ID_KEY and COMMENT_TEXT_KEY are where a comment event carries its post, parent and body; flows.models.HandledComment plus triggers/guards.may_claim_comment/record_comment are the once-only guard. Emit the right NormalizedEvent. Do NOT add a second comment model or a second guard.
- Add your PlatformPolicy row to apps/channels/policy.py (24-hour window, human-agent tag rules per SPEC §6.4) and your Capabilities row. The compliance engine consumes both as data — no Instagram branch anywhere in apps/messaging/. Telegram has none; match that.
- Import apps.common.platforms.Platform. Do not define a platform constant.
- Tests that install a fake adapter MUST use apps/channels/tests/fake_adapter.py::swapped_adapter. Registering over the real adapter hits contract 4's duplicate guard, and clearing the slot on exit breaks every later test in the process. Two copies of that logic once took 31 tests down.
- Story mentions and replies are event types, not a second ingestion path. Extend apps/channels/events.py's vocabulary if you need to, and say so in the PR — that enum is shared.
- OAuth and app review: keep the credential exchange out of the adapter module proper. Add your guided-connect route to registry.CONNECT_ROUTES.
- docs/channels/instagram.md: app setup, permissions/scopes needed, review requirements, limits.

Branch feat/l5a-instagram off main. One PR, "Closes #17", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Five siblings run in parallel in their own trees. You will contend on apps/channels/policy.py, apps/channels/capabilities.py and registry.CONNECT_ROUTES at most — additive dict/table entries, so expect one trivial rebase for whoever merges second.
````

## Trigger — #18 (L5-B, Messenger)

````
Implement issue #18 in brightbeanxyz/brightbean-chat: [L5-B] Facebook Messenger channel — DMs, message tags, m.me referrals, delivery receipts.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-5.md — its "Ground rules" name the exact Layer-4 seams; layer-2/3/4 ground rules still bind. Also docs/SPEC.md §6.1 and §6.4, docs/ROADMAP.md contract 4, docs/SECURITY-BASELINE.md §§2, 4 and 5, and CONTRIBUTING.md. Adapter issues get a dedicated security review at PR time.

Specifics for you:
- apps/channels/providers/telegram.py is your template and says so in its own docstring. Replace the helpers, keep the class.
- Message tags are the NeedsTag half of the compliance engine, which already exists. apps/messaging/compliance.py returns NeedsTag(allowed_tags) with an allowed_use_text field carrying Meta's own description of permitted use; the inbox already renders it verbatim (SPEC §6.4). Your job is the PlatformPolicy row and the send-time tag, not a new verdict type. No Messenger branch anywhere in apps/messaging/.
- The comment-trigger infrastructure is ALREADY BUILT and already scoped to Messenger — see the Instagram note in layer-5.md's ground rules. Emit events; do not add models.
- m.me referrals: the referral event type already reaches the trigger stage (apps/flows/triggers/stages.py handles referral events and deliberately never routes them to resume). Read that file before adding a path.
- Delivery receipts update message status through apps.messaging.services, never by writing model fields. ROADMAP contract 1 and the AST scan in apps/messaging/tests/test_write_sites.py enforce it.
- Import apps.common.platforms.Platform. Tests that install a fake adapter MUST use fake_adapter.swapped_adapter.
- Add your guided-connect route to registry.CONNECT_ROUTES; keep the OAuth exchange out of the adapter module.
- docs/channels/messenger.md: app setup, page subscription, permissions, review requirements, limits.

Branch feat/l5b-messenger off main. One PR, "Closes #18", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Five siblings run in parallel. Contention is limited to additive entries in apps/channels/policy.py, capabilities.py and CONNECT_ROUTES.
````

## Trigger — #19 (L5-C, WhatsApp)

````
Implement issue #19 in brightbeanxyz/brightbean-chat: [L5-C] WhatsApp channel — Cloud API, template management, template-gated sending.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-5.md — its "Ground rules" name the exact Layer-4 seams; layer-2/3/4 ground rules still bind. Also docs/SPEC.md §6.1 and §6.4, docs/ROADMAP.md contract 4, docs/SECURITY-BASELINE.md §§2, 4 and 5, and CONTRIBUTING.md. Adapter issues get a dedicated security review at PR time.

Specifics for you:
- apps/channels/providers/telegram.py is your template and says so in its own docstring. Replace the helpers, keep the class.
- Template gating already has its verdict. apps/messaging/compliance.py::can_send returns NeedsTemplate(reason) outside the window, driven by your PlatformPolicy row in apps/channels/policy.py (window_hours=24). Template *management* — sync, status, variables, approval state — is genuinely yours and is the bulk of this issue. The decision to require a template is not: it is policy data plus the existing engine. No WhatsApp branch anywhere in apps/messaging/.
- Template variables are user-authored text reaching a rendering path. SECURITY-BASELINE §3 bans SSTI: use apps/flows/rendering.py's approach, never a Django Template built from user input.
- Import apps.common.platforms.Platform. Tests that install a fake adapter MUST use fake_adapter.swapped_adapter — registering over the real adapter hits contract 4's duplicate guard.
- Media: apps/media_library/platform_limits.py::warnings_for already reads the capabilities registry for per-platform size and kind limits. Add your numbers to Capabilities rather than a second table.
- Add your guided-connect route to registry.CONNECT_ROUTES; keep the credential exchange out of the adapter module.
- docs/channels/whatsapp.md: Cloud API setup, phone number registration, template approval process, the 24-hour window, limits.

Branch feat/l5c-whatsapp off main. One PR, "Closes #19", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Five siblings run in parallel. Contention is limited to additive entries in apps/channels/policy.py, capabilities.py and CONNECT_ROUTES.
````

## Trigger — #20 (L5-D, SMS)

````
Implement issue #20 in brightbeanxyz/brightbean-chat: [L5-D] SMS channel (Twilio) — two-way messaging, STOP/HELP compliance, send_sms node.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-5.md — its "Ground rules" name the exact Layer-4 seams; layer-2/3/4 ground rules still bind. Also docs/SPEC.md §6.1, docs/ROADMAP.md contracts 4, 5 and 6, docs/SECURITY-BASELINE.md §§2, 4 and 5, and CONTRIBUTING.md. Adapter issues get a dedicated security review at PR time.

Specifics for you:
- apps/channels/providers/telegram.py is your template and says so in its own docstring. Replace the helpers, keep the class.
- READ apps/flows/triggers/stages.py FIRST. Its module docstring is addressed to you by name. The one thing it says you must know: a hard_optout hook CANNOT write identity.opted_out_at. ROADMAP contract 3 gives that column exactly one write site — apps/messaging/ingest.py — and the AST scan in apps/messaging/tests/test_write_sites.py fails the build over a second one. Ingest already applies it from an EventType.OPT_OUT event. Your job in the adapter is to classify STOP/UNSUBSCRIBE as that event type in parse_events; stages.opt_out_event already owns the platform-agnostic confirmation reply and consuming the event. Register HELP at the hard_optout stage via triggers.hooks.register_hook.
- The send_sms node registers through apps.flows.engine.registry.register_node (ROADMAP contract 5) and declares synchronous_safe as a class attribute. Do not add a second list of safe node types — apps/flows/triggers/safety.py reads the class attribute.
- Your PlatformPolicy row: no messaging window, but opt-out is absolute. Segment counting and per-segment cost belong in capabilities/limits, not in a branch inside apps/messaging/.
- Import apps.common.platforms.Platform. Tests that install a fake adapter MUST use fake_adapter.swapped_adapter.
- Twilio webhook verification is an HMAC over the full URL plus sorted POST params — constant-time compare, and mind that the URL it signs is the public one, which behind a proxy is not request.build_absolute_uri(). apps/common/net.py has is_trusted_proxy and TRUSTED_PROXIES.
- docs/channels/sms.md: Twilio setup, number provisioning, STOP/HELP regulatory requirements, per-segment cost.

Branch feat/l5d-sms off main. One PR, "Closes #20", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Five siblings run in parallel. Contention is limited to additive entries in apps/channels/policy.py, capabilities.py and CONNECT_ROUTES.
````

## Trigger — #21 (L5-E, Email)

````
Implement issue #21 in brightbeanxyz/brightbean-chat: [L5-E] Email channel — SMTP/Resend/SES outbound, unsubscribe & suppression, send_email node.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-5.md — its "Ground rules" name the exact Layer-4 seams; layer-2/3/4 ground rules still bind. Also docs/SPEC.md §6.1, docs/ROADMAP.md contracts 4 and 5, docs/SECURITY-BASELINE.md §§2, 3, 4 and 5, and CONTRIBUTING.md. Adapter issues get a dedicated security review at PR time.

Specifics for you:
- apps/channels/providers/telegram.py is your template and says so in its own docstring. Replace the helpers, keep the class. Three backends behind one adapter means the backend seam is yours to draw — keep it as narrow as apps/media_library/storage.py keeps boto3.
- The unsubscribe link is the canonical case for apps/common/signing.py and its module docstring names it. sign(payload, purpose="unsubscribe") / unsign_or_404(...). Two things that module insists on: mint with max_age=None, because unsubscribe links sit in inboxes forever and an expired one that 404s is a compliance problem rather than a broken link; and when the payload shape changes, keep accepting the old version rather than cutting over. Do not reach for django.core.signing directly and do not invent a second token format.
- Email bodies are the product's other attacker-content path. HTML you generate goes out to a mail client; SECURITY-BASELINE §3 bans SSTI, so render through apps/flows/rendering.py's approach and never build a Django Template from user input.
- Suppression list: a bounce or a complaint must survive the contact being re-imported. apps/contacts/imports.py deliberately does not fabricate identities for imported contacts — read it before deciding where suppression lives.
- The send_email node registers through apps.flows.engine.registry.register_node (contract 5) with synchronous_safe as a class attribute.
- Import apps.common.platforms.Platform. Tests that install a fake adapter MUST use fake_adapter.swapped_adapter.
- Inbound (replies, bounces, complaints) arrives at the existing webhook_email route, which is one of only three entries in the IDOR suite's WAIVED_ROUTES. Read that waiver: it holds only while the route answers the SAME status to every connection id. Keep that true.
- docs/channels/email.md: SMTP/Resend/SES setup, SPF/DKIM/DMARC, unsubscribe requirements, suppression handling.

Branch feat/l5e-email off main. One PR, "Closes #21", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Five siblings run in parallel. Contention is limited to additive entries in apps/channels/policy.py, capabilities.py and CONNECT_ROUTES.
````

## Trigger — #25 (L5-F, Public REST API + outbound webhooks)

````
Implement issue #25 in brightbeanxyz/brightbean-chat: [L5-F] Public REST API (v1) and outbound webhooks.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-5.md — its "Ground rules" name the exact Layer-4 seams; layer-2/3/4 ground rules still bind. Also docs/SPEC.md §§4, 5 and 17, docs/ROADMAP.md contracts 1 and 4, docs/SECURITY-BASELINE.md §§1, 4, 6 and 7, and CONTRIBUTING.md. This issue gets a dedicated security review at PR time — it is the first surface that authenticates without a session.

You are the only non-adapter workstream in this layer, so you share almost nothing with your five siblings.

Specifics for you:
- manage_api_keys already exists in apps/members/roles.py PERMISSION_KEYS and is already in _ADMIN_ONLY_KEYS. Gate on it; do not invent a key. NOTE: SPEC §4 describes api-key management at the ORG tier while the merged code has it as a WORKSPACE key. Ask before building either way — this issue is where that divergence stops being theoretical.
- Every write goes through the existing facades, not the ORM. ROADMAP contract 1: apps.messaging.services for sends and conversations, apps.flows.engine.start_flow for flows, apps.contacts.services for contacts. apps/messaging/tests/test_write_sites.py runs an AST scan that fails the build on a second write site. An API that writes model fields directly is the failure mode here.
- Outbound webhooks are user-supplied URLs, which makes them the exact case apps/common/outbound.py::guarded_request exists for (SECURITY-BASELINE §6). Use it. It pins DNS after validating every resolved address, re-validates each redirect hop, caps the body and enforces a total deadline. Do not hand-roll an httpx call, and do not use base.request_json — that is for fixed platform hosts.
- Sign your outbound webhook deliveries so receivers can verify them, and use apps/common/signing.py rather than a second token format.
- Rate limiting: apps/common/ratelimit.py (window_key/hit) is the Postgres fixed-window limiter. DatabaseCache.incr loses counts under concurrency, which is why that module exists — do not reach for the cache.
- Tenancy: an API key scopes to a workspace, and every queryset must go through the scoped managers. WorkspaceScopedModel raises UnscopedQueryError at execution rather than silently returning everything.
- The IDOR suite auto-discovers routes and RAISES UnregisteredRouteKwargError for any kwarg it cannot build. A key-authenticated API is exactly the case that needs a stated position: either register resolvers, or add a WAIVED_ROUTES entry with a written reason and a test class that stands in for the sweep. There are three waivers today, each cross-referenced that way — match the standard.
- Pagination, filtering and errors: keep the shape boring and documented. docs/api/v1.md with a worked example per resource.

Branch feat/l5f-api off main. One PR, "Closes #25", all six CI jobs green.

Five sibling adapter issues run in parallel in their own trees. You will contend on config/urls.py at most.
````

---

## Merge order and gate

Development is fully parallel. Merge order is free — no workstream owns a seam another needs, and all six touch `apps/channels/policy.py`, `capabilities.py` and `CONNECT_ROUTES` only additively.

**Merge one at a time, and let the post-merge run on `main` finish before the next.** Layer 4 merged four PRs inside 90 seconds; the CI concurrency group cancelled the three intermediate runs, and a collision between two of them reached `main` with no red build to show for it. Six workstreams make that more likely, not less.

**Layer 5 gate** — before opening Layer 6:

1. All six merged, six CI jobs green on `main`, with a completed run on the merge commit of each.
2. Contract 4 proven additive by five more adapters: no platform branch anywhere in `apps/messaging/`, and `grep -rn "<platform>" apps/messaging/` clean for each.
3. Every platform's compliance verdicts driven by its policy row alone — the set-wise agreement test in `apps/messaging/tests/test_compliance_setwise.py` extended to all six platforms.
4. STOP/HELP proven end to end on SMS, with `opted_out_at` still written from exactly one site.
5. Unsubscribe links minted before a payload change still resolve after it.
6. IDOR suite green and extended; the API's position on the sweep written down; security review over the merged diff; dependency audits clean.
7. **Deployment.** The Layer 4 gate asked for #28's production-infra half and it did not land — there is no `deploy/`, no production compose, no HTTPS story. Five of these six channels need a publicly reachable HTTPS webhook to be verifiable against the real platform, so this is now the binding constraint rather than a nicety. Decide before dispatch whether #28's infra half runs alongside Layer 5 or Layer 5 ships verified against fixtures only.
