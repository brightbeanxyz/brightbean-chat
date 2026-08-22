# Layer 4 — Agent Prompts

Five workstreams, **all parallel**. Layer 3 is merged, so nothing here is blocked.

| Issue | | Owns | Permission key |
|---|---|---|---|
| [#11](https://github.com/brightbeanxyz/brightbean-chat/issues/11) | L4-A | `apps/flows/triggers*` + the routing tail | `edit_flows` |
| [#12](https://github.com/brightbeanxyz/brightbean-chat/issues/12) | L4-B | `apps/channels/providers/telegram.py` | `manage_channels` |
| [#13](https://github.com/brightbeanxyz/brightbean-chat/issues/13) | L4-C | `apps/contacts/` views + templates | `manage_crm` |
| [#14](https://github.com/brightbeanxyz/brightbean-chat/issues/14) | L4-D | `apps/inbox/` | `use_inbox` / `reply_in_inbox` |
| [#15](https://github.com/brightbeanxyz/brightbean-chat/issues/15) | L4-E | `external_request` node + the shared SSRF guard | `edit_flows` |

The **issue body is the scope**. This file records the Layer-3 seams each one plugs into, with real names — Layer 3 left several of them explicitly reserved and documented for Layer 4, and finding them by reading source costs each agent a cycle.

---

## Ground rules

Everything in [`layer-2.md`](layer-2.md) → "Ground rules" and [`layer-3.md`](layer-3.md) → "Ground rules" still binds: `WorkspaceScopedModel` refuses unscoped execution, `get_scoped_object_or_404`, the roles/decorators API and typed request classes, the nav registry, `requirements.in` → `make lock`, the `tenancy`/`other_tenancy` fixtures, the IDOR obligation, the unlayered-CSS cascade trap, and the six CI jobs. Read those first — this file adds only what Layer 3 now exports.

### The routing seam is already reserved (#11's, specifically)

`apps/messaging/ingest.py` registers **two** processors into contract 6's dispatch and names them:

```python
PERSISTENCE_PROCESSOR = "persistence"
ROUTING_PROCESSOR = "routing"
```

`register_processors()` registers persistence, then registers `route_events` — a documented **no-op** — under `ROUTING_PROCESSOR`. Its docstring says what #11 does: register your own callable under the same name. Re-registering a name **replaces in place rather than appending**, so the real router inherits the slot *after* persistence, and neither `apps.messaging` nor `apps.channels` changes.

Two things Layer 3 already reasoned about, so #11 does not have to rediscover them:

- `register_processors()` guards with `if ROUTING_PROCESSOR not in registered_processors()`, because `ready()` runs in `INSTALLED_APPS` order — without the guard, an app listed before messaging would have its real router silently replaced by the no-op, and routing would stop with nothing raising.
- `persist_events` deliberately takes **no contact advisory lock**: it appends rows the database arbitrates through unique constraints, and a blocking lock there would spend SPEC §7.1's 1.5 s inline budget waiting on a worker. **The routing stage takes the lock, in its own transaction** — a transaction-scoped lock cannot span two processors.

### `apps/messaging/` — the facade and compliance (#8)

- `messaging/services.py` `__all__`: `send_outbound`, `send_as_agent`, `send_via_api`, `upsert_contact_identity`, `open_conversation`, `close_conversation`, `assign_conversation`, `pause_automation`, `AGENT_AUTOMATION_PAUSE` (30 minutes, SPEC §14). **`send_as_agent` already applies the automation pause** — #14 does not set that field itself.
- `messaging/compliance.py` — `can_send(...)` returning `Allowed` / `Blocked` / `NeedsTemplate` / `NeedsTag`, plus `annotate_eligibility(...)` and `eligible(...)` for set-wise use, `HUMAN_AGENT_TAG`, `DECISION_FIELD`. Decisions are data derived from `channels.policy`; no caller branches per platform.
- `messaging/identities.py` — `resolve_identity(connection, platform_user_id, *, occurred_at=None) -> IdentityResolution`, `record_consent(...)`, `normalized_address_for(platform, id)`, `ADDRESS_PLATFORMS`, `bounded_key`/`bounded_address` size guards.
- `messaging/ingest.py` — also exports the parse limits already enforced on inbound content: `MAX_TEXT_CHARS`, `MAX_ATTACHMENTS`, `MAX_ATTACHMENT_URL_CHARS`, `MAX_EVENT_ID_CHARS`, and the event-class sets `THREAD_EVENTS`, `ACTIVITY_EVENTS`, `CONTACT_ONLY_EVENTS`, `IDENTITY_EVENTS`, `RECEIPT_STATUSES`.

### `apps/flows/engine/` — the runtime (#9)

`apps.flows.engine.__all__` covers: `start_flow`, `resume_execution`, `advance`, `attempt_resume`, `Consumed`, `NotConsumed`, `ResumeOutcome`, `Continue`, `Wait`, `Schedule`, `StartNext`, `End`, `Fail`, `StepResult`, `Graph`, `NodeContext`, `LOOP_CAP`, `register_node`, `register_verb`, `node_class_for`, `registered_node_types`, `registered_verbs`, `EngineError`, `FlowNotRunnableError`, `UnknownNodeTypeError`, `DuplicateNodeTypeError`, `DuplicateVerbError`.

Two functions in `engine/registry.py` exist for Layer 4 by name:

- **`synchronous_safe(node_type) -> bool`** — its docstring says "Read by L4-A's inline-vs-enqueue budget." Each node class carries the answer as a class attribute, so there is no second list to disagree with it, and an unregistered type answers `False`.
- **`types_without_runtime()`** — node types the schema describes but nothing can execute. **Pinned by a test**, so a type joining or leaving that set is a deliberate act. `external_request` is in it today; #15 removes it and updates the pin.

Registered node runtimes: `action`, `condition`, `data_collection`, `note`, `randomizer`, `send_message`, `smart_delay`, `start_flow`.

Other flows modules Layer 4 touches: `flows/handlers.py` (`handle_start_flow`, `handle_resume_execution`, `handle_followup_timer` — already registered queue handlers), `flows/messaging.py` (a facade shim with `available()` and `FacadeUnavailableError`, so flows never hard-imports messaging), `flows/rendering.py` (the SSTI-safe renderer: `RenderContext`, `context_for`, `PLACEHOLDER_PATTERN`, `SYSTEM_FIELDS`, `MAX_RENDERED_CHARS`, `MAX_VALUE_CHARS`).

### Existing UI surfaces

- **Flows**: `apps/flows/urls.py` has `flows/`, `create/`, `<id>/edit/`, `rename`, `duplicate`, `archive`, `restore`, plus the builder API at `api/flows/schema/`, `api/flows/<id>/`, `api/flows/<id>/publish/`, `api/flows/<id>/stats/`. The React island lives in `frontend/builder/` (Vite, `vite.config.mts`, vitest).
- **Contacts**: `apps/contacts/` already ships `contact_list`, and full tag/field CRUD at `settings/tags/*` and `settings/fields/*` with HTMX row partials. **The `contacts/` stub route is already gone** — #13 extends this app, it does not replace a stub.
- **Remaining stubs** in `config/urls.py`: `inbox/` (#14 — replace it), plus `sequences/`, `broadcasts/`, `organization/api-keys/` and `accounts/preferences/` for later layers.
- `apps/channels/providers/` holds only `base.py` and `exceptions.py` — **no adapter exists yet**; #12 ships the first.

---

## Trigger — #11 (L4-A, triggers and inbound routing)

````
Implement issue #11 in brightbeanxyz/brightbean-chat: [L4-A] Trigger system and inbound event routing.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-4.md — its "Ground rules" name the exact Layer-3 seams you plug into; layer-2.md and layer-3.md ground rules still bind. Also docs/SPEC.md §§5, 7.1, 9.3 and 10, docs/ROADMAP.md contract 6, and CONTRIBUTING.md.

You merge FIRST in this layer — #12 proves your work end-to-end, and #14 depends on your pause semantics.

Specifics for you:
- The routing seam is already reserved for you. apps/messaging/ingest.py defines ROUTING_PROCESSOR = "routing" and registers a documented no-op route_events under it. Register your ordered hook registry under the SAME name: re-registering replaces in place, so your router inherits the slot after persistence, and nothing in apps.messaging or apps.channels changes. Keep messaging's `if ROUTING_PROCESSOR not in registered_processors()` guard working — ready() runs in INSTALLED_APPS order and without it a no-op can silently replace your router.
- Take the contact advisory lock in YOUR stage, in your own transaction. persist_events deliberately does not take it (it only appends rows the DB arbitrates, and blocking there would burn SPEC §7.1's 1.5s budget), and a transaction-scoped lock cannot span two processors. queueing.locks.contact_lock raises LockOutsideTransactionError if you are not inside one.
- Named stages, fixed order: hard_optout → post_persist → resume → trigger → default_reply. L5-D registers STOP/HELP at hard_optout and L6-C registers inbox rules at post_persist, so the registry is the deliverable, not an internal detail.
- The inline-vs-enqueue budget reads apps.flows.engine.registry.synchronous_safe(node_type) — its docstring names you. Do not build a second list of safe node types; the answer is a class attribute on each node.
- Resume calls apps.flows.engine.attempt_resume(execution, event) and honours Consumed / NotConsumed. Starting a flow is engine.start_flow(...).
- Conversation pause: read conversation.automation_paused_until, never write it. messaging.services.send_as_agent already applies AGENT_AUTOMATION_PAUSE.
- Absorb the platform-agnostic comment-trigger infrastructure (trigger type, §10 config schema, handled_comment model) so #17 and #18 do not each build it.
- Trigger CRUD UI is yours (HTMX, on the flow page), including ref_url links plus a server-generated QR code from a local library — no external service. Extend the builder data API (apps/flows/api.py) with triggers, and recompute capability warnings from trigger channel bindings.

Branch feat/l4a-triggers off main. One PR, "Closes #11", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Four siblings run in parallel in their own trees. You will contend on apps/common/context_processors.py (nav) and config/urls.py at most.
````

## Trigger — #12 (L4-B, Telegram adapter)

````
Implement issue #12 in brightbeanxyz/brightbean-chat: [L4-B] Telegram adapter end-to-end plus the "test on Telegram" flow preview.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-4.md — its "Ground rules" name the Layer-3 seams; layer-2.md and layer-3.md ground rules still bind. Also docs/SPEC.md §6.2 and §16, docs/ROADMAP.md contract 4, docs/SECURITY-BASELINE.md §§2 and 4, and CONTRIBUTING.md. Adapter issues get a dedicated security review at PR time.

You are the FIRST adapter — apps/channels/providers/ currently holds only base.py and exceptions.py. What you build is the shape Layer 5's five adapters copy, so keep the platform-specific parts obviously separable from the parts that generalise.

Specifics for you:
- Subclass the Adapter ABC in apps/channels/providers/base.py: resolve_connection, verify_webhook, parse_events, send, send_typing, mark_seen. Use its request_json helper and the exceptions module (RateLimitError carries retry_after) rather than a bare httpx client.
- Register with apps.channels.registry.register_adapter(platform, cls) and add your PlatformPolicy row to apps.channels.policy.POLICIES: window_hours=None, proactive_send=True (gated on identity opt_in), broadcast_allowed=True, rate default 25/s. The compliance engine consumes that row as data — do not add a Telegram branch anywhere in apps/messaging.
- Import apps.common.platforms.Platform. Do not define a platform constant.
- Inbound content is already size-guarded downstream: apps/messaging/ingest.py enforces MAX_TEXT_CHARS, MAX_ATTACHMENTS, MAX_ATTACHMENT_URL_CHARS. Parse defensively anyway and ship hostile fixtures (oversized, wrong types, injection strings in every string field).
- verify_webhook is a constant-time compare of X-Telegram-Bot-Api-Secret-Token against connection.webhook_secret.
- "Test on Telegram": the deep-link token uses apps/common/signing.py (sign/unsign_or_404 with a purpose salt and expiry — a tampered or expired token must give a generic 404). Run the DRAFT version via engine.start_flow(..., flow_version=<draft>); the engine already accepts an explicit version, so do not patch the runner. Flag preview executions so they stay out of stats.
- 429 → parse retry_after → raise RateLimitError so the send pipeline reschedules. Per-chat 1 msg/s is already satisfied by sequential-per-contact execution; say so in the PR rather than adding a second throttle.
- docs/channels/telegram.md: BotFather setup, public-HTTPS webhook requirement, limits.

Branch feat/l4b-telegram off main. One PR, "Closes #12", all six CI jobs green. Merge after #11 so the end-to-end test (start → button press → resume) exercises the real router.

Four siblings run in parallel in their own trees.
````

## Trigger — #13 (L4-C, contacts CRM UI)

````
Implement issue #13 in brightbeanxyz/brightbean-chat: [L4-C] Contacts CRM UI — list, detail, segment builder, CSV import/export.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-4.md — its "Ground rules" name what already exists; layer-2.md and layer-3.md ground rules still bind. Also docs/SPEC.md §5 and §11.4, and CONTRIBUTING.md.

Specifics for you:
- apps/contacts/ is NOT a blank slate and the `contacts/` stub route is already gone. It ships contact_list plus full tag and custom-field CRUD at settings/tags/* and settings/fields/* with HTMX row partials. Read apps/contacts/views.py and urls.py before writing: you are extending these, and duplicating a tag editor beside the existing one is the failure mode here.
- The filter bar compiles through apps.contacts.conditions — CONDITION_SCHEMA, queryset(), validate(), OPS_BY_TYPE, OPS_BY_SOURCE, SOURCE_NAMES, SYSTEM_FIELDS. Build the UI from those exports so a source or operator added later shows up without a second table to update. Set-wise listing uses queryset(), never a Python loop over contacts.
- Segments round-trip: save the filter, reload it, and the rules must be identical. Test that.
- Contact detail shows channel identities (platform, handle, opt-in, window status with expiry, opted_out_at) — those live on apps/messaging models now; read them, and mutate only through apps.messaging.services (upsert_contact_identity, record_consent in apps/messaging/identities.py).
- "Start a flow" from a contact calls apps.flows.engine.start_flow(...); "stop automation" expires the live execution through the engine, not by writing model fields.
- CSV import runs as a queueing action (apps.queueing.registry.register_handler + schedule), with a dry-run preview and a per-row error report. Imported contacts are NOT channel-reachable until they message in — do not fabricate identities, and say so in the consent copy.
- Bulk actions and role gating: manage_crm for writes, and the sequence subscribe/unsubscribe controls stay disabled until L6-A registers those verbs.

Branch feat/l4c-crm-ui off main. One PR, "Closes #13", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Four siblings run in parallel in their own trees.
````

## Trigger — #14 (L4-D, inbox v1)

````
Implement issue #14 in brightbeanxyz/brightbean-chat: [L4-D] Inbox v1 — conversation list, thread view, agent reply, assignment, automation pause.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-4.md — its "Ground rules" name the facade you must use; layer-2.md and layer-3.md ground rules still bind. Also docs/SPEC.md §14, docs/SECURITY-BASELINE.md §2, and CONTRIBUTING.md.

This is the primary attacker-content → team-browser path in the product: everything in a thread arrived from a stranger.

Specifics for you:
- Replace the `inbox/` stub in config/urls.py; it names your issue. New app apps/inbox/.
- Mutate messaging state ONLY through apps.messaging.services: send_as_agent, open_conversation, close_conversation, assign_conversation, pause_automation. Note send_as_agent ALREADY applies AGENT_AUTOMATION_PAUSE (30 min, exported as a constant) — do not set automation_paused_until yourself, and do not re-implement the 30 minutes as a literal.
- Compliance denial is a rendered explanation, not a silent failure. can_send returns Allowed / Blocked / NeedsTemplate / NeedsTag from apps/messaging/compliance.py, derived from channels.policy — surface the reason, and keep platform logic out of the template.
- XSS: message text, usernames, profile fields and media URLs are attacker-controlled. Escaped rendering only, never mark_safe, and validate media URLs are http(s) before they become src/href. Ship a dedicated hostile-content suite (script tags in text, javascript: URLs, HTML in usernames) and keep CSP nonces on any inline script.
- Internal notes are messages with internal=True that never reach the send pipeline — assert no adapter call in a test.
- HTMX polling every 3s on list and thread with ETag/304 so unchanged polls carry no body. This is the reference implementation SPEC §14 mandates instead of SSE; get the version hash cheap (max updated_at + count) and include labels/indicators later layers add.
- Roles: use_inbox to read, reply_in_inbox to send, Agent+ to assign. Viewer is read-only.

Branch feat/l4d-inbox off main. One PR, "Closes #14", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Four siblings run in parallel in their own trees. #11 owns routing and the pause semantics you read — do not write routing code.
````

## Trigger — #15 (L4-E, External Request node + SSRF guard)

````
Implement issue #15 in brightbeanxyz/brightbean-chat: [L4-E] External Request node with SSRF guard.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-4.md — its "Ground rules" name the engine registry you plug into; layer-2.md and layer-3.md ground rules still bind. Also docs/SPEC.md §11.7, docs/SECURITY-BASELINE.md §6, and CONTRIBUTING.md. Security-critical: dedicated security review at PR time.

You ship the SHARED SSRF guard, not a node-private helper. SECURITY-BASELINE §6 makes it mandatory for every server-initiated request to a user-supplied URL — this node, outbound webhook deliveries (#25), media fetch-by-URL, provider callbacks — and forbids those call sites until it exists. Several later issues are waiting on it, so its API is the deliverable.

Specifics for you:
- guarded_request(method, url, **kwargs): resolve the hostname first; deny private, loopback, link-local and multicast ranges (IPv4 and IPv6, including IPv4-mapped); deny the deployment's own host/port; PIN the resolved IP for the actual connection so DNS cannot rebind between check and request; re-validate on redirects (cap 3); http/https only; 1 MB streaming cap. EXTERNAL_REQUEST_ALLOW_PRIVATE=false toggles the private-range rule for on-prem.
- Note apps/common/net.py already exists and is about something else — trusted-proxy client-IP resolution. Do not overload it; the guard is its own module, and say in its docstring that it is the one mandatory path.
- Ship a test helper that asserts a given callable routes through the guard, so #25 and later call sites can prove it rather than claim it.
- Register the node with apps.flows.engine.register_node. The config SCHEMA already exists in flows/schema — you add runtime against it, you do not redefine it. external_request is currently in engine.registry.types_without_runtime(), which is PINNED BY A TEST: removing it from that set is part of your diff and is meant to be deliberate.
- synchronous_safe = False. SPEC §11.7 says worker only, never inline — the class attribute is what L4-A's budget reads.
- URL placeholders render through apps/flows/rendering.py and substituted values must be URL-encoded. Response values mapped into variables are UNTRUSTED downstream, exactly like contact input — the renderer escapes them in HTML contexts, and your PR should test that path.
- Never log resolved header values; they carry secrets. The global scrubber exists but do not rely on it alone.
- Timeout clamp ≤10s; error routes to the `error` handle without retry storms.

Branch feat/l4e-external-request off main. One PR, "Closes #15", all six CI jobs green.

Four siblings run in parallel in their own trees.
````

---

## Merge order and gate

Development is fully parallel. Merge order has one hard rule and one soft one:

- **#11 merges first.** It owns the routing tail that #12's end-to-end test exercises and the pause semantics #14 reads.
- **#12 merges after #11**, so its `/start` → trigger → button-press → resume test runs against the real router rather than a stub.
- #13, #14 and #15 are free.

**Layer 4 gate** — before opening Layer 5:

1. All five merged, six CI jobs green on `main`.
2. The phase-1 acceptance subset from #30 lands here (SPEC §21): webhook ack p95 < 500 ms, first automated reply p95 < 2 s, zero duplicate sends across 1k forced worker retries, 50 parallel webhooks for one contact never interleaving steps, loop cap halting at 30 blocks with an admin notification.
3. #28's production-infra half lands here too — a real HTTPS deployment is what makes Layer 5's webhook work testable.
4. Contract 4 (adapter registry + policy rows) proven by a real adapter, so Layer 5's five channels are additive.
5. IDOR suite green and extended; security review over the merged diff; dependency audits clean.
