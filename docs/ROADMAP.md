# BrightBean Chat — Build Roadmap

Implementation of `docs/SPEC.md`, decomposed into **layers** (sequential) and **workstreams** (parallel inside a layer, non-blocking, touching disjoint Django apps/files wherever possible). Every GitHub issue is titled `[L<layer>-<workstream>] …`.

Rule of thumb: an issue may depend on anything in **lower** layers (assume it merged), must not depend on code from a **same-layer** sibling except through the interface contracts written down below, and must never depend on a higher layer.

## Layer map

| Issue | Depends on | Delivers |
|---|---|---|
| **L1-A** Foundation | — | Django scaffold, settings, CI, Docker dev, tenancy (org/workspace/membership), RBAC (Admin/Editor/Agent/Viewer), auth, encrypted fields, Tailwind theme + base UI shell ported from BrightBean Studio |
| **L2-A** Contacts domain | L1 | `contacts` app: contact, tag, custom fields, segments, condition/filter engine (spec §11.4 schema) |
| **L2-B** Channels framework | L1 | `channels` app: channel_connection, Adapter interface, Capabilities, NormalizedEvent/OutboundMessage, block downgrading, webhook endpoints + signature framework + event log/dedup, connection settings UI |
| **L2-C** Task queue | L1 | `queueing` app: scheduled_action, worker, tick, backoff, zombie recovery, housekeeping, advisory-lock helpers |
| **L2-D** Flows core | L1 | `flows` app (no engine): flow/flow_version models, graph JSON schema for all node types (spec §11), server-side validation, versioning/publish, flow list UI, builder data API (§16) |
| **L2-E** Notifications | L1 | in-app + email notification engine ported from Studio |
| **L3-A** Messaging spine | L2 | contact_channel_identity, conversation, message models; inbound persistence pipeline; window bookkeeping; compliance engine `can_send`; send pipeline with idempotency; Postgres token buckets |
| **L3-B** Flow engine runtime | L2 | executions, runner loop, StepResult, locking, wait/resume semantics, retry/failure policy, loop cap; nodes: send_message, action, condition, smart_delay, randomizer, start_flow, data_collection, note |
| **L3-C** Flow builder UI | L2 | React Flow canvas island, node palette + config panels from the shared schema, autosave, publish |
| **L4-A** Triggers & routing | L3 | trigger model + matcher, inbound routing order (§9.3/§10), inline-vs-enqueue decision + budget (§7.1), keyword/ref_url/welcome/default_reply/api types |
| **L4-B** Telegram | L3 | Telegram adapter end-to-end, connection UI, "test on Telegram" preview |
| **L4-C** Contacts CRM UI | L3 | contact list/detail, tag/field editors, segment builder UI, CSV import/export |
| **L4-D** Inbox v1 | L3 | conversation list + thread, agent reply, assignment, open/done, automation pause, HTMX polling with 304s |
| **L4-E** External Request node | L3 | external_request node, SSRF guard |
| **L4-F** Media library | L1 | media library app ported from Studio (org/ws scoped, folders, S3/local) |
| **L5-A** Instagram | L4 | IG adapter (Instagram Login), DMs, postbacks, deletions, HUMAN_AGENT rules, private replies, comment/story_mention/story_reply/follow triggers |
| **L5-B** Messenger | L4 | Messenger adapter, message tags, m.me referrals, delivery/read receipts, FB comment trigger |
| **L5-C** WhatsApp | L4 | Cloud API adapter, whatsapp_template CRUD + status polling, NeedsTemplate path |
| **L5-D** SMS (Twilio) | L4 | SMS adapter in+out, STOP/HELP core handling, send_sms node |
| **L5-E** Email | L4 | Email adapter (SMTP/Resend/SES), unsubscribe + suppression, bounces, send_email node, open/click routes |
| **L6-A** Sequences + rule triggers | L5 | sequences models/worker/UI, internal event bus, rule trigger type |
| **L6-B** Broadcasts | L5 | composer, eligibility filter, fanout, live counters, cancellation |
| **L6-C** Inbox v2 | L5 | labels, inbox rules engine, reminders, scheduled replies, internal notes |
| **L6-D** Public API + webhooks | L5 | django-ninja API v1, api_key auth + rate limit, outbound webhooks with HMAC + retries |
| **L7-A** Analytics | L6 | node_stat_daily, click tracking, builder stats overlay, broadcast stats page |
| **L7-B** Flow export/import | L6 | flow JSON export/import incl. triggers, template sharing |
| **L7-C** Deployment & docs | L6 | docker-compose.prod, Heroku/Render/Railway, healthz, self-hosting + per-platform setup docs |
| **L7-D** GDPR & security | L6 | contact hard delete + export, redaction audit, security pass |
| **L7-E** Acceptance suite | L6 | cross-system tests for spec §21 criteria (idempotency, interleaving, budgets, loop cap, broadcast counts) |

## Same-layer interface contracts

Workstreams in the same layer code against these signatures without importing each other's unmerged code; integration is verified by the next layer (and L7-E).

1. **Send pipeline (L3-A provides, L3-B consumes):**
   `messaging.services.send_outbound(*, workspace, contact, connection, outbound: OutboundMessage, source: str, idempotency_key: str) -> Message` — applies compliance (`can_send`), inserts the message row first (idempotent), calls the adapter, returns the `Message` with status. Raises nothing for compliance denials; returns the message with `status=failed` + machine-readable error code. L3-B's send_message node calls only this.
2. **Graph schema (L2-D provides, L3-B/L3-C consume):** the shared node-config JSON-schema module in `flows/schema/` is the single source of truth; server validation and React config panels both generate from it. Changing it after L2-D requires touching both consumers.
3. **Routing hooks (L3-A provides fields, L4-A consumes):** `conversation.automation_paused_until`, `identity.window_expires_at`, `identity.opted_out_at` are written only by L3-A code paths; the trigger matcher only reads them.
4. **Adapter registry (L2-B provides, L4-B/L5-* consume):** each platform adds one module in `channels/providers/` and registers `platform -> Adapter` plus a compliance policy entry (window_hours, tags, rate default). Additive only — parallel channel workstreams never edit shared files beyond a one-line registry entry.
5. **Node registry (L3-B provides, L4-E/L5-D/L5-E consume):** new node types register `type -> NodeClass` and add their config schema to the L2-D schema module. Additive only.

## Conventions for every issue

- Frontend follows BrightBean Studio: Django templates + HTMX + Alpine.js + Tailwind 4. Reuse Studio's `theme/static_src` token architecture, `templates/base.html` shell, `templates/layouts/`, `templates/components/`, and `apps/common/htmx.py` helpers. The only React is the flow builder island.
- Tests accompany every issue (pytest, same conventions as Studio's `conftest.py` + per-app `tests/`).
- No AI features, no billing, no TikTok — do not stub them.
- Product name in UI/docs: **BrightBean Chat**.
