# Layer 3 — Agent Prompts

Three workstreams. Two of them ship as **two sequential PRs by the same agent**.

| Issue | | App | PRs |
|---|---|---|---|
| [#8](https://github.com/brightbeanxyz/brightbean-chat/issues/8) | L3-A | `apps/messaging/` | 2 (models+ingest, then compliance+send) |
| [#9](https://github.com/brightbeanxyz/brightbean-chat/issues/9) | L3-B | `apps/flows/` (runtime) | 2 (runner+simple nodes, then wait/resume) |
| [#10](https://github.com/brightbeanxyz/brightbean-chat/issues/10) | L3-C | `frontend/builder/` + `apps/flows/` templates | 1 |

The **issue body is the scope**. This file carries the Layer-2 APIs each one builds on, with real exported names.

## ⚠️ The Layer 2 gate is not met yet

Four of six Layer 2 issues are merged (#5 queueing, #6 flows, #7 notifications, #16 media). **Two are still open: [#44](https://github.com/brightbeanxyz/brightbean-chat/pull/44) (contacts, #3) and [#47](https://github.com/brightbeanxyz/brightbean-chat/pull/47) (channels, #4).** That gates Layer 3 unevenly:

- **#10 is unblocked now.** Everything it consumes — the flows data API, the schema artifact, the media picker — is merged.
- **#9 needs #44** for `contacts.conditions.evaluate()`.
- **#8 needs both**: channels for the adapter registry, policy table and dispatch seam; contacts for the identity model's base and the `window` condition source.

Dispatch #10 whenever you like. Hold #8 and #9 until their dependencies land, or accept that they will code against the contracts as written here and integrate on merge.

---

## Ground rules

Everything in [`layer-2.md`](layer-2.md) → "Ground rules" still binds: `WorkspaceScopedModel` refuses to execute unscoped, `get_scoped_object_or_404`, the roles/decorators API, `apps.common.platforms.Platform`, the nav registry, the `requirements.in` → `make lock` discipline, the `tenancy`/`other_tenancy` fixtures, the IDOR obligation, the unlayered-CSS cascade trap, and the six CI jobs. Read it first — this file only adds what Layer 2 itself now exports.

### Reaching an app that has not landed

`apps/flows/compat.py` is the house pattern, and #6 wrote it for exactly this situation:

```python
installed_model(app_label, app_module, model_name)  # -> model class, or None
```

It answers "not yet" rather than raising, and starts answering for real the moment the app is installed, with no edit at the call site. Use it instead of `try: import ... except ImportError`.

The same issue also vendored a fallback capability table: `apps/flows/capabilities.py` exposes `CAPABILITIES_ARE_VENDORED`, `CAPABILITIES`, `capabilities_for(platform)` and `connected_platforms(workspace)`, importing from `apps.channels` when present and falling back to `_VENDORED` when not. **Do not add a second vendored copy of anything** — extend this one, or wait for #47.

### `apps/flows/` — schema, services, data API (#6, merged)

- `flows/schema/nodes.py` — **contract 2 and contract 5 both live here.** `NodeSpec`, `NODE_TYPES`, `SHARED_DEFS`, `ACTION_VERBS`, `register_node_type(spec)`, `register_action_verb(verb, def_name, schema)`, `register_defs(**defs)`, `node_spec(type)`, `handles_for_node(spec, config)`. The **schema for every node type already exists**, including ones whose runtime lands later. L3-B adds runtime against these specs; it does not redefine them.
- `flows/schema/validation.py` — `validate_graph(graph, *, platforms=(), known_size=None) -> ValidationResult`, `Issue`.
- `flows/schema/export.py` — `json_schema()`, `serialize()`, artifact at `static/flows/flow-schema.json` (`ARTIFACT_RELATIVE_PATH`). L3-C consumes the artifact; it does not hand-maintain a node list.
- `flows/services.py` — `create_flow`, `rename_flow`, `set_folder`, `archive_flow`, `latest_version`, `published_version`, `validate_for_workspace`, `PublishResult`, `FlowValidationError`.
- `flows/api.py`, `flows/picklists.py` — the builder data API and its pick-lists.
- `flows/models.py` — `Flow`, `FlowVersion` (both `WorkspaceScopedModel`), `FlowStatus`.

### `apps/queueing/` — the scheduling substrate (#5, merged)

- `queueing/registry.py` — `register_handler(action_type, *, replace=False)`, `get_handler`, `registered_types()`, `schedule(...)`, `schedule_system(...)`, `IdempotencyKeyConflictError`, `DuplicateHandlerError`, `UnknownActionTypeError`.
- `queueing/locks.py` — `contact_lock(contact)`, `try_contact_lock(contact)`, `advisory_lock(key)`, `try_advisory_lock(key)`, `contact_lock_key(contact)`, and **`LockOutsideTransactionError`**: an advisory lock taken outside a transaction is not a lock, so the helpers refuse. The one-step-per-contact invariant (SPEC §9.6) rests entirely on this.
- `queueing/worker.py` — `BACKOFF_SCHEDULE = (30, 120, 600, 3600, 21600)`, already SPEC §9.5's ladder. Reuse it; do not restate the numbers.
- `queueing/models.py` — `ScheduledAction` (`WorkspaceScopedModel`), `ActionType`, `ActionStatus`, `TERMINAL_STATUSES`, `DEFAULT_MAX_ATTEMPTS = 5`, `coerce_contact_id`.
- `queueing/housekeeping.py` — `register_housekeeping_job(name, *, replace=False)`, `OPTIONAL_JOB_PATHS`. Check whether your job is already listed there before adding it.

### `apps/notifications/` (#7, merged)

`notifications.engine.notify(...)` plus `events.register_event`, `get_event`, `NotificationEvent`, `REGISTRY`. Event copy is registered data — check `events.py` for an existing key before inventing one, and register yours if it is missing.

### `apps/media_library/` (#16, merged)

- `media_library.resolution.resolve(media_id, *, workspace) -> {"url", "mime", "kind"}`, `MediaNotFoundError`. **The `workspace` kwarg is required** — resolution is scoped, and that is deliberate.
- `media_library.picker.picker_payload(...)`, `serialize_asset(asset, *, platform="")`, `serialize_folder`. `platform_limits.py` holds per-platform media ceilings.

### `apps/contacts/` (#3, **PR #44 open**)

`apps.contacts.conditions.__all__` is settled: `CONDITION_SCHEMA`, `evaluate`, `evaluate_many`, `queryset`, `validate`, `register_source`, `sources`, `ConditionSource`, `CompiledFilter`, `SOURCE_NAMES`, `SYSTEM_FIELDS`, `OPS_BY_TYPE`, `OPS_BY_SOURCE`, `ConditionError`, `ConditionValidationError`, `SourceNotEvaluableError`, `SourceContractError`.

`SOURCE_NAMES` already includes `window` and `sequence` as registrable slots that raise `SourceNotEvaluableError` until their owner registers them. Models: `Contact`, `Tag`, `ContactTag`, `CustomField`, `CustomFieldValue`, `Segment`, `ContactStatus`, `CustomFieldType`, and **`ContactScopedModel`** — a base for models scoped by contact rather than only by workspace.

### `apps/channels/` (#4, **PR #47 open**)

- `channels/registry.py` — `register_adapter`, `adapter_for`, `entry_for`, `RegistryEntry`, `registered_platforms`, `has_adapter`, `AdapterNotRegisteredError`.
- `channels/policy.py` — **contract 4 as data**: `PlatformPolicy`, `POLICIES`, `policy_for(platform)`, `NeedsTag`, `OutsideWindow`. Messenger's allowed-use text is already in the table.
- `channels/events.py` — `NormalizedEvent`, `EventType`, `EventPayload`, `OutboundMessage`, `TextBlock`, `MediaBlock`, `Card`, `CardBlock`, `GalleryBlock`, `Button`, `QuickReply`.
- `channels/ingest.py` — **contract 6's seam**: `register_processor(processor, *, name)`, `unregister_processor`, `registered_processors()`, `process_events(connection, events)`, `synthetic_event_id`.
- `channels/providers/base.py` — the `Adapter` ABC (`resolve_connection`, `verify_webhook`, `parse_events`, `send`, `send_typing`, `mark_seen`), `request_json`, and `SendResult`. Also `downgrade.py`, `capabilities.py`, `security.py`, and models `ChannelConnection`, `WebhookEventLog`, `ConnectionStatus`.

---

## Trigger — #8 (L3-A, messaging spine)

````
Implement issue #8 in brightbeanxyz/brightbean-chat: [L3-A] Messaging spine — channel identities, conversations, messages, compliance engine, send pipeline.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-3.md — its "Ground rules" section names the real Layer-2 APIs you build on, and layer-2.md's ground rules still bind. Also docs/SPEC.md §§5, 7.1, 8, 9.4, 9.5, docs/ROADMAP.md contracts 1, 3, 4, 6, 7 and 8, docs/SECURITY-BASELINE.md §§2 and 5, and CONTRIBUTING.md.

Ship as TWO sequential PRs: PR1 = models + inbound persistence + window bookkeeping; PR2 = compliance engine + token buckets + send pipeline + the service facade.

Specifics for you:
- You own ROADMAP contract 1, the messaging service facade — send_outbound(), upsert_contact_identity(), open/close/assign_conversation(), pause_automation(). L3-B and L4-D mutate messaging state ONLY through it, so the signatures in the contract are load-bearing. The agent-send +30min automation pause lives inside send_outbound(source="agent"), not in the caller.
- Register your persistence processor with apps.channels.ingest.register_processor(processor, name=...). That is contract 6's seam and it already exists — do not edit the webhook views to call you directly. L4-A registers the routing tail after you.
- Compliance consumes apps.channels.policy.policy_for(platform) as DATA — PlatformPolicy carries outside_window (blocked | NeedsTag(tags, allowed_use_text) | needs_template), human_agent_days, broadcast_allowed and the rate default. can_send() must contain no per-platform branches; Layer 5 adapters add policy rows, never patch you.
- Register the `window` condition source via apps.contacts.conditions.register_source. SOURCE_NAMES already lists it as a slot that raises SourceNotEvaluableError until you fill it.
- ContactChannelIdentity: SPEC §5 files it under contacts, but apps/contacts is issue #3's app. Put it in apps/messaging/ and inherit apps.contacts.models.ContactScopedModel (a cross-app abstract base is fine and that base exists for exactly this shape). Say which you chose and why in the PR. It needs the consent-audit fields opt_in_at and opt_in_source.
- Register the send_retry handler with apps.queueing.registry.register_handler, and reuse apps.queueing.worker.BACKOFF_SCHEDULE rather than restating 30s/2m/10m/1h/6h.
- Token buckets are per-connection and Postgres-backed (SPEC §22: no Redis). apps.common.ratelimit is a FIXED-WINDOW limiter, which is not a token bucket — read its docstring for the house pattern (a row plus select_for_update, because DatabaseCache.incr loses counts under concurrency), then build the bucket alongside it rather than bending the window into one. The inline path needs a non-blocking try_acquire.
- Window bookkeeping (window_expires_at) is written in the ingest path and nowhere else. Make that a single greppable write site and test it.
- Emit message.received per contract 7's event catalog.

Branch feat/l3a-messaging off main. Two PRs, the second closing #8, all six CI jobs green on each. Extend the IDOR suite for every endpoint you add.

Blocked until PR #44 (contacts) and PR #47 (channels) merge. L3-B (#9) and L3-C (#10) run in parallel with you in their own trees; L3-B calls your facade by signature and fakes it in tests, so do not let its needs pull scope into your PRs.
````

## Trigger — #9 (L3-B, flow engine runtime)

````
Implement issue #9 in brightbeanxyz/brightbean-chat: [L3-B] Flow engine runtime — executions, runner, waits, and the core node set.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-3.md — its "Ground rules" section names the real Layer-2 APIs you build on, and layer-2.md's ground rules still bind. Also docs/SPEC.md §§5, 9.2–9.6, 11.1–11.6, 11.8, 11.11, docs/ROADMAP.md contracts 1, 5 and 8, docs/SECURITY-BASELINE.md §3, and CONTRIBUTING.md.

Ship as TWO sequential PRs: PR1 = execution model, runner, locking, failure policy, loop cap, registries, the shared renderer, and the simple nodes (action, condition, randomizer, start_flow, note); PR2 = wait/resume plus send_message, smart_delay and data_collection.

Specifics for you:
- The node SCHEMAS already exist. #6 shipped flows/schema/nodes.py with NodeSpec and every node type registered. You add RUNTIME against those specs via register_node_type / register_action_verb (contract 5) — you do not redefine config schemas, and a mismatch between your runtime and the shipped spec is a bug in your PR.
- Sequence action verbs (subscribe_sequence / unsubscribe_sequence) are L6-A's to register. Leave them out; an unknown verb must fail validation at publish and log at runtime, not silently pass.
- The condition node calls apps.contacts.conditions.evaluate() — do not re-implement the operator table. That app is PR #44, still open: SPEC §11.4 and the exported names in layer-3.md are the contract, so code against them and integrate on merge.
- Locking: apps.queueing.locks.contact_lock(contact), and it raises LockOutsideTransactionError if you are not inside a transaction — an advisory lock outside one is not a lock. The whole one-step-per-contact invariant is this call.
- Register start_flow, resume_execution and followup_timer handlers with apps.queueing.registry.register_handler. Reuse apps.queueing.worker.BACKOFF_SCHEDULE. Register stale-execution expiry with register_housekeeping_job — check OPTIONAL_JOB_PATHS first, it may already name your job.
- Loop cap at 30 blocks calls apps.notifications.engine.notify(...). Check apps/notifications/events.py for an existing event key and register yours if missing; the copy is registered data, not an f-string at the call site.
- You own the SSTI-safe renderer. SECURITY-BASELINE §3 names its path: flows/rendering.py. Plain token substitution, never template-engine evaluation of user content; HTML-escaping mode for the email node; External Request response values are untrusted like contact input.
- Media blocks resolve through apps.media_library.resolution.resolve(media_id, workspace=ws) — the workspace kwarg is required.
- Sending goes through L3-A's facade (contract 1) and nothing else. It is a parallel sibling: call send_outbound() by signature and fake it in tests.
- start_flow() accepts an explicit flow_version so #12 can run a draft for its "test on Telegram" preview without patching the runner.

Branch feat/l3b-flow-engine off main. Two PRs, the second closing #9, all six CI jobs green on each.

Blocked until PR #44 (contacts) merges. L3-A (#8) and L3-C (#10) run in parallel in their own trees.
````

## Trigger — #10 (L3-C, flow builder UI)

````
Implement issue #10 in brightbeanxyz/brightbean-chat: [L3-C] Flow builder UI — React Flow canvas island with schema-driven config panels.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-3.md — its "Ground rules" section names the real Layer-2 APIs you build on, and layer-2.md's ground rules still bind (especially the unlayered-CSS cascade trap and the CSP nonce requirement). Also docs/SPEC.md §16 and §9.1, and CONTRIBUTING.md.

You are NOT blocked: everything you consume is already merged.

Specifics for you:
- The schema artifact is real and generated: apps/flows/schema/export.py writes static/flows/flow-schema.json (ARTIFACT_RELATIVE_PATH). Consume it at build time. Do not hand-maintain a node list in the frontend — every node type, its config schema and its handles come from there, so a node added in a later layer needs no bespoke canvas code.
- The data API is apps/flows/api.py with pick-lists in apps/flows/picklists.py. Read what it actually returns before designing panels; pick-lists degrade to empty arrays for apps that have not landed.
- Capability warnings come from apps/flows/capabilities.py — capabilities_for(platform) and connected_platforms(workspace). Note CAPABILITIES_ARE_VENDORED: #6 vendored the table while #4 was in flight, and it flips to the real channels import when PR #47 merges. Render whatever the API returns; do not branch on the flag or add a second vendored copy.
- Media picker: apps/media_library/picker.py (picker_payload, serialize_asset with its platform kwarg) and urls.py. The picker is already built — wire to it rather than inventing an endpoint.
- CI already has a "Dependency audit + frontend build" job and a root package.json with npm audit wired. Add your Vite build into the existing frontend-build step; do not create a parallel toolchain. Python deps go through requirements.in + make lock; npm deps must keep npm audit --audit-level=low green.
- The Dockerfile runs collectstatic AFTER dropping to USER app, so your bundle must be built and copied --chown=app:app before that step, and every {% static %} reference must resolve or the image build fails.
- Every inline script carries nonce="{{ request.csp_nonce }}". CSP is django-csp 4.0 in config/settings/base.py (CSP_POLICY["DIRECTIVES"], csp.constants) and it already has no CDN origins — keep the bundle self-contained. apps/common/tests/test_csp.py asserts unsafe-eval stays scoped to script-src; if your bundle lets you drop it, change that test deliberately and say so.
- Visit /ui/ first — it is the living style guide.

Branch feat/l3c-builder off main. One PR, "Closes #10", all six CI jobs green. Extend the IDOR suite for any endpoint you add.

L3-A (#8) and L3-B (#9) run in parallel in their own trees and are blocked on Layer 2 PRs; you are not.
````

---

## Merge order and gate

- **#10** is free and can land first.
- **#9** merges after PR #44 (contacts).
- **#8** merges after PR #44 and PR #47, and its two PRs are strictly ordered.
- Within #8 and #9, PR1 → PR2 by the same agent.

**Layer 3 gate** — before opening Layer 4:

1. All three merged (five PRs total), six CI jobs green on `main`.
2. Contract 1 (facade), contract 5 (node/verb registries) and contract 6 (processor seam) exist as written — Layer 4 codes against them without reading Layer 3 internals.
3. The one-step-per-contact invariant proven: 50 parallel resume attempts for one contact never interleave steps.
4. Loop cap halts at 30 blocks with an admin notification.
5. IDOR suite green and extended; security review over the merged diff; dependency audits clean.
