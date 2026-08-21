# OpenChat Engineering Specification

Working title: OpenChat (ships as **BrightBean Chat** in this repo). Open-source, self-hostable chat marketing automation platform. Feature target: ManyChat parity minus AI features, minus billing (every feature available to every user), minus TikTok DMs (partner-gated API, not obtainable by self-hosters).

Version 1.0 of this spec. Owner: Jan. License: AGPL-3.0.

Implementation is tracked as GitHub issues titled `[L<layer>-<workstream>] …`. Layers are sequential; workstreams inside a layer are parallel and non-blocking. See `docs/ROADMAP.md`.

---

## 1. Purpose and scope

OpenChat lets a workspace connect messaging channels (Telegram, Instagram, Facebook Messenger, WhatsApp, SMS, Email), build automation flows in a visual editor, and run them against inbound events: DMs, comments, story mentions, inbound SMS. It includes a contact CRM, drip sequences, broadcasts, and a shared live-chat inbox with human takeover.

Deployment model mirrors BrightBean Studio: each self-hoster creates their own platform developer apps and supplies credentials via env vars or per-organization Django admin. Direct first-party API calls only, no aggregator middleman.

### 1.1 Non-goals (do not build)

- AI features of any kind (no AI reply nodes, no intents, no flow generation).
- Billing, plans, feature gates, contact limits. There is exactly one tier.
- TikTok DM automation (Business Messaging API is restricted to badged TikTok partners).
- Native mobile apps. The web UI must be responsive; that is the mobile story.
- Native third-party integrations (Zapier clones, CRM connectors). The integration surface is: public API + External Request node + outbound webhooks.
- E-commerce (catalogs, payments).
- Website growth widgets (overlays, embeds). Possible later, out of scope for this spec.

---

## 2. Tech stack

Identical conventions to brightbean-studio. Where this spec is silent, copy the Studio pattern.

- Python 3.12+, Django 5.x
- PostgreSQL 16+ (the only datastore; no Redis, no message broker)
- Frontend: Django templates + HTMX + Alpine.js + Tailwind 4
- Flow builder canvas: React + React Flow (xyflow), built as a static bundle, mounted as an island on one page
- Background work: DB-backed task queue, worker via `python manage.py process_tasks` (same pattern as Studio), plus a `manage.py tick` fallback entry point
- Web server: Gunicorn (WSGI). SSE is not used in v1; inbox uses HTMX polling
- Media: local disk or S3/R2 (env-switched, same as Studio)
- Deploy targets: Docker Compose (reference), one-click Heroku / Render / Railway
- Credential encryption at rest: reuse Studio's encrypted field implementation

Repo layout (Django apps):

```
config/                 settings, urls, wsgi
apps/common/            shared utils: encryption, BaseModel, signing, scoped managers
apps/organizations/     organization
apps/workspaces/        workspace
apps/members/           org + workspace membership, invitations, RBAC (port from Studio)
apps/accounts/          user, auth, provisioning
apps/credentials/       platform app credentials
apps/channels/          adapters, channel_connection, webhook endpoints
apps/contacts/          contact, identity, tags, fields, segments, import/export
apps/flows/             flow, versions, triggers, engine, nodes
apps/messaging/         conversation, message, compliance engine, send pipeline
apps/campaigns/         sequences, broadcasts
apps/inbox/             inbox UI views
apps/queueing/          scheduled_action, worker, tick
apps/api/               public REST API, outbound webhooks
apps/analytics/         counters and stats views
```

App packages live under `apps/` (settled in PR #35). Bare top-level names are avoided because `channels`, `calendar` and `email` would shadow a PyPI package or the standard library.

---

## 3. Architecture overview

Request path for an inbound event:

1. Platform delivers webhook to `POST /webhooks/<platform>/`.
2. Web process verifies signature, deduplicates against `webhook_event_log`, persists the raw event, and decides: run inline (first-reply budget) or enqueue. Returns 200 within 500 ms p95 in all cases.
3. Adapter normalizes the event. Trigger matcher finds a flow (or resumes a waiting execution). Engine executes nodes until it hits a pause (delay, wait-for-reply) or terminal node.
4. Outbound sends go through the compliance engine, then the adapter, with idempotency keys.
5. Anything time-based (Smart Delay, sequences, broadcasts, retries, follow-up timers) becomes a `scheduled_action` row processed by the worker.

Single invariant that everything relies on: at most one flow step executes at a time per contact. See 9.6.

---

## 4. Multi-tenancy and RBAC

Port Studio's model unchanged: Organization -> Workspace, with **two membership tiers and two permission tables** — the architecture of Studio's `apps/members`. All tenant data carries `workspace_id`. Channel connections are per workspace. Platform app credentials resolve in order: workspace-level override (if set) -> organization-level (Django admin) -> deployment env vars.

### 4.1 Organization tier

`org_membership(user, organization, org_role)`. Roles: **owner**, **admin**, **member**. Everything that spans workspaces, or governs who may enter one, lives here.

`ORG_PERMISSION_KEYS` — a set-based table, as in Studio's `BUILTIN_ORG_PERMISSIONS`:

| Key | owner | admin | member |
|---|:--:|:--:|:--:|
| `manage_members` — invite, remove, change org roles, assign workspace memberships | ✓ | ✓ | |
| `manage_workspaces` — create and archive workspaces | ✓ | ✓ | |
| `manage_platform_credentials` — org-level platform app credentials | ✓ | ✓ | |
| `manage_api_keys` — issue and revoke API keys for any workspace in the org | ✓ | ✓ | |

Owner and admin hold the same set; they differ through role-hierarchy checks rather than the table — only an owner may change an owner, the last owner cannot be removed or demoted, and nobody may grant a tier at or above their own. Resolution helper: `has_org_permission(membership, key) -> bool`.

**Member management is organization-level, not workspace-level.** An invitation carries an `org_role` plus a list of workspace assignments (`[{workspace_id, role}]`), so a single invite places a person in the organization and in the workspaces they need. Member-management routes are mounted outside the workspace URL prefix.

### 4.2 Workspace tier

`workspace_membership(user, workspace, workspace_role)`. Roles: **admin**, **editor**, **agent**, **viewer**. Everything scoped to one workspace's data lives here.

`PERMISSION_KEYS` — a dict-of-bools table, as in Studio's `BUILTIN_ROLE_PERMISSIONS`:

| Key | admin | editor | agent | viewer |
|---|:--:|:--:|:--:|:--:|
| `use_inbox` | ✓ | ✓ | ✓ | ✓ |
| `view_analytics` | ✓ | ✓ | ✓ | ✓ |
| `reply_in_inbox` | ✓ | ✓ | ✓ | |
| `edit_contact_fields` | ✓ | ✓ | ✓ | |
| `manage_crm` | ✓ | ✓ | | |
| `edit_flows` | ✓ | ✓ | | |
| `send_broadcasts` | ✓ | ✓ | | |
| `manage_channels` | ✓ | | | |
| `manage_workspace_settings` | ✓ | | | |

Resolution: `WorkspaceMembership.effective_permissions -> dict[str, bool]`. This property is the **only** protocol consumers use — the public API (§17) duck-types a `VirtualMembership` exposing just `effective_permissions`, exactly as Studio's `apps/api/auth.py` does.

An **org owner is treated as a workspace admin in every workspace of their org**; an org admin is bounded by actual workspace membership. Port that rule from Studio's `members/services.py` together with its escalation guards.

### 4.3 Enforcement

Four decorators, Studio's signatures unchanged: `require_org_role(min_role)`, `require_org_permission(key)`, `require_workspace_role(min_role)`, `require_permission(key)`. The role hierarchies live in one module and are imported wherever needed — Studio duplicates them across two files with a "must match" comment; do not repeat that. Denials raise `PermissionDenied`; cross-tenant object access returns 404, never 403.

Note the deliberate name overlap: `OrgRole.ADMIN` and `WorkspaceRole.ADMIN` are different roles at different tiers. Prose always qualifies ("org admin" / "workspace admin"); in code the enum and decorator names disambiguate.

No seat limits anywhere. Inbox features are available to any member with the workspace agent role or above.

---

## 5. Data model

All PKs are UUIDv7. All tenant tables have `workspace_id` FK with index. Timestamps `created_at`, `updated_at` everywhere (omitted below). JSON columns are `jsonb`.

### core
- `organization`, `workspace` — port from Studio.
- `org_membership`: user, organization, org_role (owner/admin/member), invited_at, accepted_at. Unique (user, organization).
- `workspace_membership`: user, workspace, workspace_role (admin/editor/agent/viewer), added_at. Unique (user, workspace).
- `invitation`: organization, email, org_role, workspace_assignments json (`[{workspace_id, role}]`), invited_by, token (unique), expires_at, accepted_at. Invitations are org-level (§4.1).

### channels
- `channel_connection`: workspace_id, platform (enum: telegram, instagram, messenger, whatsapp, sms, email), display_name, external_id (page id / IG user id / WABA phone number id / bot id / Twilio number / sending domain), credentials (encrypted json), status (active, needs_reauth, disabled), capabilities_cache json, webhook_secret. Unique (platform, external_id). Index (workspace_id, platform).

### contacts
- `contact`: workspace_id, first_name, last_name, locale, timezone, email, phone, status (active, deleted), last_interaction_at. Index (workspace_id, last_interaction_at).
- `contact_channel_identity`: contact_id, channel_connection_id, platform, platform_user_id, opt_in (bool), opt_in_at (nullable timestamptz), opt_in_source (text: message_in, data_collection, import, api, manual — consent audit per 11.8), opted_out_at, window_expires_at (nullable timestamptz), last_inbound_at, extra json (username, profile pic url). Unique (channel_connection_id, platform_user_id). Index (contact_id).
- `tag`: workspace_id, name. Unique (workspace_id, name).
- `contact_tag`: contact_id, tag_id. Unique together.
- `custom_field`: workspace_id, name, type (text, number, date, datetime, boolean). Unique (workspace_id, name).
- `custom_field_value`: contact_id, custom_field_id, value_text, value_number, value_date, value_datetime, value_bool (one populated per row based on type). Unique (contact_id, custom_field_id).
- `segment`: workspace_id, name, filter_json (same condition schema as the Condition node, section 11.4).

### flows
- `flow`: workspace_id, name, status (draft, active, archived), folder (nullable text).
- `flow_version`: flow_id, version (int, monotonic), graph_json, published (bool), created_by. A flow has at most one published version. Editing always writes a new draft version; publishing flips flags atomically.
- `trigger`: workspace_id, flow_id, channel_connection_id (nullable = all connections of matching platform), type (enum, section 10), config_json, enabled, priority (int). Index (workspace_id, type, enabled).
- `flow_execution`: flow_version_id, contact_id, workspace_id, status (running, waiting_reply, waiting_delay, completed, failed, expired), current_node_id, variables json, blocks_since_pause (int), wait_config json (what resumes it: quick-reply set, data-collection field, timer id), started_by (trigger id / broadcast id / sequence id / api), updated_at. Partial unique index: one row per (contact_id, flow_id) where status in (running, waiting_reply, waiting_delay). Index (status, updated_at).

### messaging
- `conversation`: workspace_id, contact_id, channel_connection_id, state (open, done), assignee_id (nullable), automation_paused_until (nullable), last_message_at. Unique (contact_id, channel_connection_id). Index (workspace_id, state, last_message_at).
- `message`: conversation_id, direction (in, out), source (automation, agent, api, broadcast, sequence), body json (normalized schema, section 7.2), provider_message_id, status (queued, sent, delivered, read, failed), error text, idempotency_key. Unique (conversation_id, idempotency_key) for outbound; unique (channel_connection_id via join, provider_message_id) enforced as unique (provider_message_id) scoped per connection in a composite. Index (conversation_id, created_at).
- `conversation_label`: workspace_id, name; `conversation_label_link` unique together.
- `inbox_rule`: workspace_id, condition_json, actions json (add label, assign to member, mark done), enabled, priority.
- `whatsapp_template`: channel_connection_id, name, language, category (marketing, utility, authentication), body_structure json, meta_template_id, status (draft, pending, approved, rejected), rejected_reason.

### campaigns
- `sequence`: workspace_id, name, status. `sequence_step`: sequence_id, position, delay_value, delay_unit, send_window json (days of week, hour range, contact-timezone flag), flow_id (each step fires a flow).
- `sequence_enrollment`: sequence_id, contact_id, current_step, next_run_at, status (active, completed, unsubscribed). Index (status, next_run_at).
- `broadcast`: workspace_id, channel_connection_id, name, target_filter_json, message flow_id OR whatsapp_template_id, message_tag (nullable), scheduled_at, status (draft, scheduled, sending, sent, cancelled), stats json (queued, sent, delivered, failed, skipped_window).

### queueing
- `scheduled_action`: workspace_id, contact_id (nullable for non-contact jobs), run_at, type (resume_execution, start_flow, sequence_step, broadcast_fanout, broadcast_send, send_retry, followup_timer, housekeeping), payload json, status (pending, running, done, failed, cancelled), attempts, max_attempts (default 5), last_error, idempotency_key. Unique (idempotency_key). Index (status, run_at). Index (contact_id, status).

### webhooks and API
- `webhook_event_log`: channel_connection_id, platform, provider_event_id, received_at, processed_at, status (received, processed, skipped_duplicate, failed), raw json. Unique (channel_connection_id, provider_event_id). Prune rows older than 30 days via housekeeping job.
- `api_key`: workspace_id, name, hashed_key, scopes, last_used_at.
- `outbound_webhook`: workspace_id, url, secret, events (array: contact.created, contact.tag_added, message.received, execution.completed, broadcast.finished), enabled. Delivery via scheduled_action with retries.

### analytics
- `node_stat_daily`: flow_id, node_id, date, sent, delivered, failed, clicked. Upserted counters, no per-event rows in v1 beyond `message`.

---

## 6. Channel integrations

### 6.1 Adapter interface

One module per platform in `channels/providers/`. Each implements:

```python
class Adapter:
    capabilities: Capabilities   # static per platform
    def verify_webhook(self, request, connection) -> bool
    def parse_events(self, request, connection) -> list[NormalizedEvent]
    def send(self, connection, identity, outbound: OutboundMessage) -> SendResult
    def send_typing(self, connection, identity) -> None      # no-op where unsupported
    def mark_seen(self, connection, identity) -> None        # no-op where unsupported
```

`Capabilities` (booleans plus limits): text, image, audio, video, file, card, gallery, buttons, quick_replies, url_buttons, typing_indicator, proactive_send, window_hours (int or None), tags_supported (list), max_buttons, max_quick_replies, max_text_len, broadcast_allowed, inbound (bool).

The engine renders one abstract `OutboundMessage`; the adapter downgrades unsupported blocks deterministically: gallery -> sequential image+text messages; buttons unsupported -> numbered options appended to text ("Reply 1 for ..."); card -> image + text + url in text.

### 6.2 Telegram (build first)

- Bot API, token from BotFather stored on the connection. Webhook set via `setWebhook` with `secret_token`; verify the `X-Telegram-Bot-Api-Secret-Token` header equals `channel_connection.webhook_secret`.
- Inbound: messages, callback queries (button presses), /start payloads (ref-URL equivalent: `t.me/<bot>?start=<ref>` maps to the Ref URL trigger).
- Buttons: inline keyboards (callback_data carries `node_id:button_id`). Quick replies: reply keyboards.
- No messaging window, proactive_send true (contact must have messaged the bot once; enforce opt_in on identity). Rate limits: ~1 msg/sec per chat, ~30/sec global; on HTTP 429 honor `retry_after` and reschedule.

### 6.3 Instagram

- Instagram API with Instagram Login (`graph.instagram.com`), Professional accounts. Scopes: `instagram_business_basic`, `instagram_business_manage_messages`, `instagram_business_manage_comments`.
- OAuth connect flow identical in shape to Studio's existing IG provider; store user token + refresh handling.
- Webhook fields: `messages`, `messaging_postbacks`, `comments`, `mentions`, `message_deletions` (must be handled: redact message body, keep row with status deleted).
- Signature: `X-Hub-Signature-256`, HMAC-SHA256 of raw body with app secret, constant-time compare, before JSON parsing.
- Window: 24h from last inbound user message; HUMAN_AGENT tag extends to 7 days, allowed only for agent (inbox) sends, never automation. proactive_send false. broadcast_allowed false.
- Private replies (comment-to-DM): one message per comment, within 7 days of the comment. Track per comment id.
- Docs must state: Advanced Access + App Review + Business Verification required to serve accounts other than the app owner's.

### 6.4 Facebook Messenger

- Page + `pages_messaging` via Facebook Login for Business. Webhook object `page`, fields `messages`, `messaging_postbacks`, `messaging_referrals`, `message_deliveries`, `message_reads`.
- Same signature scheme as Instagram. Window 24h. Tags: HUMAN_AGENT (7d, agent sends only), CONFIRMED_EVENT_UPDATE, POST_PURCHASE_UPDATE, ACCOUNT_UPDATE (non-promotional only; the broadcast composer must force tag selection when targeting outside-window contacts and display Meta's allowed-use text). broadcast_allowed true (with tags), sponsored/marketing-messages API out of scope v1.
- m.me ref links map to Ref URL trigger via `messaging_referrals`.

### 6.5 WhatsApp

- Cloud API direct from Meta. Connection stores WABA id, phone number id, permanent token.
- Webhook object `whatsapp_business_account`, fields `messages` (inbound + status updates). Same signature scheme. Statuses map to message.status (sent/delivered/read/failed).
- Window 24h from inbound. Outside window: approved template messages only. proactive_send true only via templates. Template CRUD against the Graph API from the `whatsapp_template` model; poll status after submit.
- Surface per-send cost hint in broadcast composer (static table per category, editable in settings; do not attempt live pricing).

### 6.6 SMS (BYO Twilio) — inbound and outbound

- Connection stores account SID, auth token, from-number (or messaging service SID).
- Outbound: Send SMS node and broadcasts. Segment-count preview in composer (GSM-7 vs UCS-2, 160/70 chars).
- Inbound: expose `POST /webhooks/sms/<connection_id>/`; validate `X-Twilio-Signature`. Inbound texts create/update contact identity (keyed on E.164 number), enter conversations, and run through the trigger matcher exactly like a DM. Keyword triggers therefore work on SMS.
- Mandatory carrier compliance handled in-core, not in flows: inbound STOP/UNSUBSCRIBE/CANCEL/QUIT/END sets opted_out_at, sends the confirmation reply, and blocks all future sends to that identity. HELP returns configurable help text. START/UNSTOP re-opts in. These are hard-coded before trigger matching.
- Docs note: US traffic requires A2P 10DLC registration on the Twilio side; OpenChat surfaces a settings checklist only.

### 6.7 Email (BYO SMTP / Resend / SES) — outbound only

- Connection stores SMTP creds or provider API key, from-address, from-name.
- Send Email node and broadcasts. Simple template: subject, HTML body (rich text editor, not a drag-drop email builder in v1), plain-text alternative auto-generated.
- Compliance in-core: every email includes List-Unsubscribe header and a hosted unsubscribe link (`/u/<signed token>`); clicking sets opted_out_at for the email identity and adds it to a suppression check applied before every send. Bounce handling: webhook endpoints for Resend/SES bounce notifications set status failed and suppress hard bounces.
- Open/click tracking: 1px pixel route + link-wrapping redirect route, increment node stats. Per-message granularity not required in v1 beyond message.status.
- Docs note: SPF/DKIM/DMARC required on the sending domain.

---

## 7. Webhook ingestion

### 7.1 Endpoint behavior

`POST /webhooks/<platform>/` (Meta platforms, one URL per platform per deployment; connection resolved from payload ids) and `POST /webhooks/sms/<connection_id>/`, `POST /webhooks/email/<provider>/<connection_id>/`.

Steps, all inside the request:

1. Verify signature. Fail -> 403, log.
2. For each event in payload: attempt insert into `webhook_event_log` with (connection, provider_event_id). Conflict -> skip (duplicate delivery).
3. Persist normalized inbound `message` row (direction in), update identity `last_inbound_at`, set `window_expires_at = now + window_hours` where applicable, update conversation.
4. Decide execution mode:
   - If a waiting execution or trigger matches AND the resulting first step is synchronous-safe (send message, action, condition, randomizer, start flow), execute inline under a total budget of 1.5 s wall clock including the outbound API call (2 s hard timeout on the HTTP client). Fire `mark_seen` + `send_typing` first where supported.
   - Budget exceeded, node is not synchronous-safe, or any error: enqueue `scheduled_action(type=resume_execution, run_at=now)` and let the worker do it.
5. Return 200. Never return 5xx for business-logic failures; only for signature failures and malformed payloads per platform requirements.

Meta verification GET (`hub.challenge`) must be supported on the same URLs.

### 7.2 Normalized schemas

`NormalizedEvent`: type (message, postback, comment, story_mention, story_reply, referral, follow, delivery_status, opt_out), connection, platform_user_id, provider_event_id, timestamp, payload (text, attachments, button id, comment id, media ids, ref string).

`OutboundMessage.body` json: `{ blocks: [ {type: text|image|audio|video|file|card|gallery, ...}, ], buttons: [...], quick_replies: [...], tag: null|HUMAN_AGENT|..., template_ref: null|id }`.

---

## 8. Compliance engine

Single chokepoint: `can_send(identity, source, outbound) -> Allowed | NeedsTemplate(reason) | NeedsTag(allowed_tags) | Blocked(reason)`. Called by the send pipeline for every outbound message, no exceptions.

Rules:

- opted_out_at set -> Blocked (all platforms).
- Platform window_hours set and `window_expires_at < now`:
  - Instagram: automation -> Blocked. Agent send -> Allowed with HUMAN_AGENT if within 7 days of last inbound, else Blocked.
  - Messenger: automation/broadcast -> NeedsTag (non-promotional tags) unless a tag already set and valid; agent -> HUMAN_AGENT rule as above.
  - WhatsApp: NeedsTemplate. Broadcast composer resolves this at targeting time; flow sends outside window fail the node with a logged error (do not silently drop).
- Rate throttling: token bucket per channel_connection, config per platform (defaults: telegram 25/sec, instagram 8/sec, messenger 40/sec, whatsapp 20/sec, sms 1/sec/number, email 10/sec). The worker respects buckets; the inline path performs a non-blocking acquire and falls back to enqueue when empty.
- Broadcast eligibility filter (section 13.2) applies the same rules set-wise before fanout and records `skipped_window` counts.

Window bookkeeping: `window_expires_at` updated on every inbound event in the webhook path. Nowhere else.

---

## 9. Flow engine

### 9.1 Graph format (`flow_version.graph_json`)

```json
{
  "schema": 1,
  "nodes": [
    { "id": "n1", "type": "send_message", "position": {"x":0,"y":0},
      "config": { ... node-type specific, section 11 ... } }
  ],
  "edges": [
    { "id": "e1", "source": "n1", "sourceHandle": "default|btn:<button_id>|qr:<id>|cond:true|cond:false|rand:<path_id>|timeout",
      "target": "n2" } ]
}
```

Entry node: the node with no incoming edges from other nodes; validation rejects graphs with zero or multiple entries. Server-side validation on save and publish: unknown node types, dangling edges, missing required config, channel-capability warnings (non-blocking), cycles allowed (caps protect at runtime).

### 9.2 Execution model

- `flow_execution` is the durable state machine: current_node_id, variables, status, wait_config.
- Node classes implement `execute(ctx) -> StepResult` where StepResult is one of: `Continue(next_handle)`, `Wait(wait_config)`, `Schedule(run_at, resume_handle)`, `End`, `Fail(error)`.
- The runner loop: load execution (locked, 9.6), dispatch current node, follow edge for the returned handle, increment blocks_since_pause, repeat. Missing edge for a handle -> End.
- blocks_since_pause resets on Wait/Schedule. If it reaches 30 -> Fail("loop cap"), notify workspace admins (in-app notification), status failed.
- Variables: json dict merged into a render context along with contact fields for `{{placeholder}}` substitution in all text (system fields: first_name, last_name, email, phone; custom fields by name; variables by key).
- Starting a flow when an active execution exists for the same contact+flow: terminate the old one (status expired) and start fresh. Different flow: the new execution supersedes; the old one is expired. Exactly one live execution per contact (any flow) keeps semantics simple and matches ManyChat behavior closely enough; revisit post-v1 if parallel executions are demanded.

### 9.3 Waiting and resumption

Wait types stored in wait_config:

- quick_reply / buttons: map of expected ids -> handles, optional followup timer (schedule followup_timer action; on fire, follow `timeout` handle), optional retry-on-unmatched (max 5, counter in wait_config).
- data_collection: field target, reply type validation (text, email, phone, number, date, url), retry limit (default 3), timeout.
- smart_delay: resumed only by its scheduled_action.

Inbound event routing per contact: (1) hard opt-out keywords (SMS) -> core handling; (2) waiting execution on that channel -> attempt resume (match button id / validate input); unmatched input with no retry -> execution keeps waiting AND the event falls through to (3) trigger matching, so keywords still work mid-flow only if nothing consumed the event; matched or retried input is consumed. (4) nothing matched -> default reply trigger if configured (rate-limited to once per contact per 24h per channel).

### 9.4 Idempotency

Outbound idempotency_key = `exec:{execution_id}:node:{node_id}:attempt_bucket` (attempt bucket = retry attempt for send_retry actions, 0 inline). Insert message row before provider call; on unique violation, skip the call. Store provider_message_id on success. Provider timeout with unknown outcome: mark status queued and schedule send_retry which first checks for the provider id where the API allows lookup, else retries with the same idempotency key (accepting rare duplicate risk on Telegram only, documented).

### 9.5 Failure policy

Node-level provider errors: 4xx permanent -> message failed, follow `default` edge onward (sending failure does not kill the flow) and increment node failure stat. 429/5xx -> schedule send_retry with backoff 30s, 2m, 10m, 1h, 6h then failed.

### 9.6 Locking

All execution (inline and worker) wraps in: `pg_advisory_xact_lock(hashtext('contact:' || contact_id))`. The worker claims jobs with `SELECT ... FOR UPDATE SKIP LOCKED` on scheduled_action, then takes the contact advisory lock before touching the execution. Inline path: `pg_try_advisory_xact_lock`; if unavailable, enqueue instead of blocking the web request.

---

## 10. Triggers

All triggers reference a flow and carry config_json. Matching runs in priority order (int, lower first), first match wins per event.

| Type | Channels | Config |
|---|---|---|
| keyword | all inbound | keywords[], mode per keyword: exact / contains / any_word (case-insensitive, trimmed) |
| comment | instagram, messenger | post scope: all / specific post ids; include_keywords[], exclude_keywords[]; top_level_only bool; public_reply: none / static text / random from list; like_comment bool; once_per_contact_per_post bool (default true) |
| story_mention | instagram | none |
| story_reply | instagram | optional keywords |
| follow | instagram | none (fires on new-follower webhook where available; degrade gracefully if the field is unavailable to the app) |
| ref_url | telegram (start payload), messenger (m.me ref), instagram (ig.me ref) | ref string exact match |
| default_reply | per channel | frequency guard fixed 24h |
| welcome | telegram (/start no payload), messenger (get_started postback) | none |
| rule | n/a (internal events) | event: tag_added / tag_removed / field_changed / sequence_subscribed / sequence_unsubscribed / contact_created; optional filters |
| api | n/a | fired via public API flow-start endpoint |

Comment trigger behavior: public reply and like are executed via the platform API, the private reply (the flow's first message) counts against the one-private-reply-per-comment rule; store handled comment ids to enforce once_per_contact_per_post and the 7-day private-reply deadline.

---

## 11. Node reference

Every node: id, type, position, config. Handles listed are the sourceHandles the builder exposes.

### 11.1 send_message
Config: blocks[] (text with placeholders; image/audio/video/file by media library id or URL; card {image, title, subtitle, url_button}; gallery = card list), buttons[] (id, label, action: url / postback-handle), quick_replies[] (id, label), followup {enabled, delay, unit} routes to `timeout`, retry_unmatched {enabled, max<=5, text}. Handles: default, btn:<id>..., qr:<id>..., timeout. Behavior: renders per capability flags; if buttons/QRs present -> Wait, else Continue(default).

### 11.2 action
Config: actions[] executed in order: add_tag(tag), remove_tag, set_field(field, value|placeholder), clear_field, subscribe_sequence(sequence), unsubscribe_sequence, open_conversation, close_conversation, assign_conversation(member), notify_members(member_ids, via: email, text template). Handles: default. Always Continue.

### 11.3 start_flow
Config: flow_id. Behavior: terminates current execution (completed), starts target flow's published version immediately under the same lock. Handle: none (terminal in-graph).

### 11.4 condition
Config: `{ match: all|any, rules: [ {source: tag|custom_field|system_field|segment|sequence|window, key, op, value} ] }`. Ops by type: text is/is_not/contains/has_value/no_value; number =, !=, >, <, >=, <=; date/datetime before/after/on, relative offsets (days ago / days from now); bool is; tag has/has_not; sequence subscribed/not; window inside/outside. Handles: cond:true, cond:false.

### 11.5 smart_delay
Config: mode duration {value, unit: minutes|hours|days} or date {field or fixed datetime}; continue_window {enabled, days[], from, to, use_contact_timezone}. Behavior: Schedule(run_at adjusted into the next allowed window). Handle: default.

### 11.6 randomizer
Config: paths[] {id, weight percent}, sticky bool (default true). Sticky: first pass stores path in variables under `rand:<node_id>` and reuses it. Handles: rand:<id>.

### 11.7 external_request
Config: method, url (placeholders allowed), headers[], body (json template), timeout_s (max 10), response_mappings[] {json_path, target custom_field or variable}, fallback_handle_on_error bool. Handles: default, error. Runs in worker only (never inline). SSRF guard: deny requests resolving to private/loopback/link-local ranges and the deployment's own host; configurable allowlist env `EXTERNAL_REQUEST_ALLOW_PRIVATE=false`.

### 11.8 data_collection (block inside send_message or standalone node; implement standalone)
Config: question text, reply_type (text, email, phone, number, date, url), target (custom_field or system email/phone), retry {max 3, invalid_text}, timeout {delay, handle}. On valid input: save; if reply_type email/phone also update contact.email/phone and create/refresh the corresponding email/SMS identity with opt_in true recorded with timestamp + source (consent audit). Handles: default, timeout.

### 11.9 send_sms
Config: text, media_url optional. Requires an SMS connection and contact phone identity; missing -> follow error handle. Handles: default, error. Compliance engine applies (opt-out suppression).

### 11.10 send_email
Config: subject, html_body, from override optional. Requires email connection + contact email identity, suppression applied. Handles: default, error.

### 11.11 note
Config: text. Ignored at runtime, builder-only annotation.

---

## 12. Sequences

Enrollment via action node, rule trigger, or manually from contact view. Each step waits its delay from the previous step's send, adjusted to the step's send window, then starts the step's flow (which typically contains a single send_message). Unsubscribe stops future steps (mid-flow executions complete). Worker query: `sequence_enrollment where status=active and next_run_at <= now` batched via SKIP LOCKED.

---

## 13. Broadcasts

### 13.1 Composer
Channel connection, target = segment or ad-hoc condition filter, content = inline mini-flow (send_message [+ buttons]) or WhatsApp template with variable mappings, schedule now/later, Messenger tag selector when needed.

### 13.2 Fanout
On send: `scheduled_action(type=broadcast_fanout)` resolves the audience with the condition engine, applies compliance filtering per identity (window, opt-out, template requirement), records skipped counts, and inserts one `broadcast_send` action per contact in batches of 500. Sends drain through the per-connection token bucket. Broadcast page shows live counters from stats json (updated in batches). Cancellation flips remaining pending actions to cancelled.

Instagram never appears in the broadcast channel selector.

---

## 14. Inbox

- Conversation list: filter by state, channel, assignee, label; sorted by last_message_at. HTMX polling every 3 s on list and open thread (ETag/304 to keep payloads near-zero when unchanged).
- Thread view: full message history, send box (goes through compliance engine as source=agent), internal notes (stored as message with source=agent, direction out, flag internal, never sent).
- Agent send sets `conversation.automation_paused_until = now + 30 min` (constant, ws-configurable later). Trigger matcher and execution resumption skip paused conversations except opt-out handling; waiting executions do not consume events while paused (they resume eligibility after the pause lapses).
- Manual controls: pause/resume automation toggle, assign, labels, state open/done, reminders (`scheduled_action` -> in-app notification), scheduled replies (compose now, send at time via queue).
- Inbox rules engine: on inbound message, evaluate `inbox_rule` conditions (channel, keyword, contact tag/field) and apply actions. Runs in the webhook path after persistence, before trigger matching.

---

## 15. Task queue and worker

- Worker loop (process_tasks): every 1 s, claim up to 50 due rows: `UPDATE scheduled_action SET status='running', attempts=attempts+1 WHERE id IN (SELECT id FROM scheduled_action WHERE status='pending' AND run_at <= now() ORDER BY run_at LIMIT 50 FOR UPDATE SKIP LOCKED) RETURNING *`. Process each; on exception, status pending with backoff (30s, 2m, 10m, 1h, 6h), after max_attempts -> failed.
- Multiple worker processes are safe by construction (SKIP LOCKED + contact advisory locks).
- Zombie recovery: housekeeping job resets rows in status running with updated_at older than 10 min back to pending.
- `manage.py tick`: single drain pass (claim until empty or 55 s elapsed), for cron-based hosts. `/internal/tick?token=` HTTP wrapper (constant-time token compare, env `TICK_TOKEN`) for external pingers. Behavior identical to one worker cycle; safe to run concurrently with a worker.
- Housekeeping (hourly, via self-rescheduling action): prune webhook_event_log > 30 d, expire stale waiting executions (waiting > 30 d -> expired), poll WhatsApp template statuses, reset zombies.

---

## 16. Flow builder UI

- Route `/w/<workspace>/flows/<id>/edit` serves a Django template with a mount div; the React bundle (React 18 + @xyflow/react) loads from static files. Everything else in the app stays HTMX.
- Data API (session-authenticated, workspace-scoped, CSRF via X-CSRFToken header):
  - `GET  /api/flows/<id>/` -> latest draft version graph + metadata + tags/fields/sequences/flows lists for config panels + capability warnings
  - `PUT  /api/flows/<id>/` -> save draft (server validation; response includes validation warnings)
  - `POST /api/flows/<id>/publish/` -> validate strictly, bump published version
  - `GET  /api/flows/<id>/stats/` -> per-node counters for the stats overlay
- Node config panels are React components mirroring section 11 config schemas exactly; keep a single shared JSON-schema module used by both server validation and the client.
- Autosave drafts (debounced 2 s). Publish is explicit.
- Preview: a "test on Telegram" action that links the editor's user to a test conversation and runs the draft version against it (Telegram only in v1; cheapest real-channel test loop).

---

## 17. Public API and outbound webhooks

Bearer auth with `api_key` (`Authorization: Bearer oc_...`), workspace-scoped, JSON.

- `GET/POST /api/v1/contacts`, `GET/PATCH /api/v1/contacts/<id>`, `POST /api/v1/contacts/<id>/tags`, `DELETE .../tags/<tag>`, `PUT /api/v1/contacts/<id>/fields/<field>`
- `POST /api/v1/contacts/<id>/flows/<flow_id>/start` (fires api trigger; respects locks and compliance)
- `POST /api/v1/messages` {contact_id, connection_id, body} (source=api, compliance applies)
- `GET /api/v1/flows`, `GET /api/v1/tags`, `GET /api/v1/fields`
- Rate limit 10 req/s per key (in-Postgres sliding window is fine at this scale).

Outbound webhooks: HMAC-SHA256 signature header `X-OpenChat-Signature` over raw body with the endpoint secret; retries via queue (same backoff), auto-disable after 100 consecutive failures with admin notification.

---

## 18. Analytics (v1 scope)

Per-node counters (sent, delivered, failed, clicked) via `node_stat_daily` upserts from the send pipeline and click-redirect route (`/c/<signed payload>` wraps URL buttons and email links). Flow detail page renders totals and per-node overlay in the builder. Broadcast stats from broadcast.stats. Nothing else in v1: no funnels, no pixel, no UTM builder.

---

## 19. Security

- Credentials and tokens: encrypted at rest (Studio implementation). Never logged.
- Webhook signature verification mandatory on every endpoint; raw-body HMAC before parsing; constant-time compares.
- SSRF guard on external_request and on any URL-fetch (media by URL) as in 11.7.
- Placeholders render with auto-escaping in email HTML context; plain text elsewhere.
- Message deletion webhooks (Instagram): redact stored body.
- GDPR: contact delete = hard delete of contact, identities, field values, messages bodies (keep anonymized counters); export = JSON dump endpoint on contact view.
- Opt-out is enforced in the compliance engine, not in flows, so it cannot be bypassed.

---

## 20. Deployment

- `docker-compose.prod.yml`: app (gunicorn, 4 workers 2 threads), worker (process_tasks), postgres, caddy (auto-HTTPS), one-shot migrate service. Same shape as Studio.
- Heroku: web + worker dynos, Basic or higher (sleeping dynos break webhooks); no worker -> Scheduler every 10 min + external pinger on `/internal/tick` for minute-granularity, documented as degraded mode.
- Render: paid web service + background worker, or web + cron/tick.
- Railway: web service + worker service from the same image (different start command).
- Health: `/healthz` (DB check). Readiness for webhooks requires public HTTPS; Caddy or platform TLS.
- Env vars: everything from Studio's base set plus `PLATFORM_*` app credentials per platform, `TICK_TOKEN`, `EXTERNAL_REQUEST_ALLOW_PRIVATE`, `DEFAULT_SEND_RATE_OVERRIDES` (json, optional).

---

## 21. Build phases and acceptance criteria

### Phase 1
Scope: core + tenancy port, data model, channel adapter framework, Telegram end-to-end, Instagram (DMs, keywords, comment-to-DM, default reply, welcome), compliance engine (window tracking + IG rules), flow engine with nodes send_message, action, condition, smart_delay, randomizer, start_flow, data_collection, note; queue + worker + tick; builder with those nodes; contacts/tags/fields/segments; basic inbox (list, thread, reply, assign, takeover pause); Docker deploy.

Accept when: webhook ack p95 < 500 ms and first automated reply p95 < 2 s on a 2 vCPU box; zero duplicate sends across 1k forced worker retries; concurrent webhooks for one contact never interleave steps (test with 50 parallel events); IG private-reply constraints enforced in tests; loop flow halts at 30 blocks with admin notification.

### Phase 2
Scope: Messenger, WhatsApp (incl. template management), SMS in+out with STOP/HELP handling, Email out with unsubscribe/suppression, sequences, broadcasts with eligibility filter and live counters, inbox rules/labels/reminders/scheduled replies, rule triggers, ref URL/QR, external_request node, send_sms/send_email nodes, outbound webhooks.

Accept when: 10k-contact broadcast respects token buckets and skips out-of-window identities with correct counts; WhatsApp template submit->approved->send round-trips against a real WABA; STOP suppresses within one inbound event; unsubscribe link suppresses email within one click.

### Phase 3
Scope: public API, analytics counters + click tracking, template flows (imports/exports of flow JSON for sharing), story mention/reply + follow triggers, media library polish, PaaS one-click buttons.

Accept when: a Make/Zapier-style scenario (inbound webhook -> API contact update -> flow start) works with only public API + outbound webhooks; flow export/import round-trips including triggers.

---

## 22. Decisions log (questions pre-answered)

- One live execution per contact across all flows; new start supersedes. Revisit only with user demand.
- Randomizer sticky by default, per-node toggle.
- No Redis ever in v1; Postgres is the queue, the lock manager, and the rate limiter.
- SSE deferred; HTMX polling with 304s is the inbox transport.
- WhatsApp costs are the self-hoster's Meta bill; OpenChat only warns, never meters.
- Human agent 7-day tag is available only to inbox sends, never automation, hard-coded.
- TikTok, website widgets, e-commerce, AI, billing: out of scope, do not stub.
- Naming: "OpenChat" is a working title; this repo ships as **BrightBean Chat** — use BrightBean Chat in user-facing UI and docs, keep internal naming grep-friendly.
