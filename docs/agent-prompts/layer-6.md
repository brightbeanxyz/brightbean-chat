# Layer 6 — Agent Prompts

Three workstreams, **all parallel**. Layer 5 is merged, so nothing here is blocked.

| Issue | | Owns | Permission key |
|---|---|---|---|
| [#22](https://github.com/brightbeanxyz/brightbean-chat/issues/22) | L6-A | `apps/campaigns/` — sequences + rule triggers | `edit_flows` |
| [#23](https://github.com/brightbeanxyz/brightbean-chat/issues/23) | L6-B | broadcasts — composer, fanout, counters | `send_broadcasts` |
| [#24](https://github.com/brightbeanxyz/brightbean-chat/issues/24) | L6-C | `apps/inbox/` v2 — labels, rules, reminders | `use_inbox` / `reply_in_inbox` |

The **issue body is the scope**. This file records the seams Layers 3–5 reserved for these three, with real names. Several are named-and-empty on purpose: the stage exists, the picklist resolver exists, the condition source is declared, the action-verb schemas ship. Finding them by reading source costs each agent a cycle, and *not* finding them means building a duplicate.

---

## Ground rules

Everything in [`layer-2.md`](layer-2.md) → [`layer-5.md`](layer-5.md) → "Ground rules" still binds: `WorkspaceScopedModel` refuses unscoped execution, `get_scoped_object_or_404`, the roles/decorators API, the nav registry, `requirements.in` → `make lock`, the `tenancy`/`other_tenancy` fixtures, the IDOR obligation, and the six CI jobs. Read those first — this file adds only what Layers 4 and 5 now export.

### Everything you need is already named. Look before you build.

This is the layer where the reserved seams pay off, and the failure mode is building a second one beside the reserved one. A non-exhaustive list of things that already exist:

| You need | It is already at | State |
|---|---|---|
| A `sequences` picklist for the builder | `apps/flows/picklists.py::_sequences` | Resolves `campaigns.Sequence` via `installed_model`; returns `[]` until the app exists |
| `subscribe_sequence` / `unsubscribe_sequence` config schemas | `apps/flows/schema/nodes.py` | Schemas ship; **runtime** is L6-A's |
| A `sequence` condition source | `apps/contacts/conditions.py` | Declared with a `None` handler and the note `"issue #22, L6-A"` |
| The `post_persist` routing stage | `apps/flows/triggers/hooks.py` | Empty and named, and in `RUNS_WHILE_PAUSED` |
| `reminder` / `scheduled_reply` / `sequence_step` / `broadcast_fanout` / `broadcast_send` action types | `apps/queueing/models.py::ActionType` | Constants spelled; the registry is the authority, the enum is open |
| `broadcast.finished` as a subscribable webhook event | `apps/api/events.py::SUBSCRIBABLE_EVENTS` | Offered; **no emitter until L6-B**, and subscribing today is a no-op |
| Approved-WhatsApp-template selector | `apps/channels/whatsapp_templates.py::approved_templates_for` / `variable_schema` | Written for "L6-B's broadcast composer and the flow builder's template picker" |
| SMS segment counting | `apps/channels/segments.py` | Pure, no Django/DB/clock; the composer multiplies by its own price |
| Eligibility filtering | `apps/messaging/compliance.py::eligible` / `annotate_eligibility` | Docstring says "Exported for L6-B" |
| Email suppression | `apps/channels/suppression.py::is_suppressed` | Workspace-scoped address check |
| Media picker payload | `apps/media_library/picker.py` | Names "the broadcast composer (#23)" as a caller |

If you find yourself writing a second table of approved templates, a second segment counter or a second eligibility filter, stop and go find the first one.

### L6-A: the app is called `campaigns`, and that is not a preference

`apps/flows/picklists.py::_sequences` calls `installed_model("campaigns", "apps.campaigns", "Sequence")` and returns `[]` while that resolves to nothing. Ship `apps/campaigns/` with a `Sequence` model exposing `name` and `.objects.for_workspace(...)`, and the builder's dropdown fills itself with no edit to `apps/flows/`. A different app label leaves that resolver permanently empty and nothing will fail loudly.

Three registrations, all additive, none of which touch the module that declares them:

- **Action verbs** — `apps.flows.engine.registry.register_verb("subscribe_sequence", handler)`. The *schemas* already ship in `apps/flows/schema/nodes.py`; `apps/flows/engine/nodes/action.py` says an unregistered verb "logs a warning and moves on", so today the node is a documented no-op. You supply the runtime, not the schema.
- **Condition source** — `apps.contacts.conditions.register_source(...)` for `sequence`. It is already declared with a `None` handler; you fill it. Set-wise, through `queryset()`, never a Python loop.
- **Events** — contract 7 says you emit `sequence.subscribed` / `sequence.unsubscribed`. The convention is a per-app `EVENT_CATALOG: dict[str, Signal]` module (see `apps/contacts/events.py`, `apps/messaging/events.py`); `apps/api/events.py::discover_catalog` unions them automatically. Payloads carry workspace id, contact id and event-specific ids **only** — never message bodies.

**Rule triggers consume the event catalog, not the inbound pipeline.** ROADMAP contract 6 is explicit: "L6-A's rule triggers consume the event catalog (contract 7), not this pipeline." A rule that fires on `contact.tag_added` connects to that Django signal. Do not register a routing hook for it.

Sequence steps schedule through `apps.queueing.registry.schedule(ActionType.SEQUENCE_STEP, ...)` with `workspace=` required and keyword-only — `schedule_system` is for deployment-level rows and is not what you want. Give every step an `idempotency_key`; the queue returns the existing row rather than a duplicate.

### L6-B: the composer is HTMX, and a broadcast is a single-node graph

ROADMAP line 43: **"HTMX composer (single-node `graph_json` — no React embed)"**. The React island is the flow builder's; a broadcast is one message, and reusing the canvas here would be the expensive wrong call. Reuse the *config panels'* data shapes, not the canvas.

Fanout is the one genuinely hard part and the pieces are all placed:

- **Eligibility** — `apps.messaging.compliance.eligible(identities, ...)` returns just the identities a send would be allowed to, set-wise. `annotate_eligibility` gives you the per-identity verdict when you need to show *why* someone was excluded. Do not re-derive window state.
- **Targeting** — `apps.contacts.conditions.queryset(workspace, filter_json)`. Same engine as segments, so a broadcast audience and a segment agree by construction.
- **Rate** — `apps.messaging.buckets` (`rate_for`, `capacity_for`, `try_acquire`). The bucket is per connection and already tuned per platform in `apps/channels/policy.py`. A broadcast must not bypass it, and must not add a second throttle beside it.
- **Batching** — `ActionType.BROADCAST_FANOUT` splits, `BROADCAST_SEND` delivers. Both are already spelled in `apps/queueing/models.py`.
- **Sends** — through `apps.messaging.services`, never the ORM. `apps/messaging/tests/test_write_sites.py` runs an AST scan that fails the build on a second write site.

**Cancellation has to be real.** A broadcast cancelled mid-fanout must stop scheduling *and* skip already-scheduled sends that have not run. Decide where that check lives and test it against a partially-drained queue.

**You own `broadcast.finished`.** `apps/api/events.py` already offers it to webhook subscribers and `apps/api/models.py` notes nothing emits it yet. Emit it through your app's `EVENT_CATALOG` and L5-F's delivery picks it up with no edit.

Per-platform composer affordances read from the registries: `approved_templates_for` + `variable_schema` for WhatsApp, `apps/channels/segments.py` for SMS length, `apps/channels/capabilities.py` for what blocks a platform renders. No platform branch in `apps/messaging/`, and no `{% if platform == "whatsapp" %}` in a template.

### L6-C: `post_persist` is your stage — and the ROADMAP had it misnamed

The stage is **`post_persist`**. ROADMAP line 44 said "pre_trigger hook", which was wrong and is corrected in this PR — there is no `pre_trigger` stage and there never was. `apps/flows/triggers/hooks.py` defines the five: `hard_optout → post_persist → resume → trigger → default_reply`.

Two properties of that stage that were designed for you:

- It ships **empty and named**, and `apps/flows/triggers/stages.py`'s docstring says that is the point: your rules are a registration, not an edit to routing code. That file is the worked example — read it before writing a hook.
- It is in `RUNS_WHILE_PAUSED`, deliberately. `hooks.py` explains why: "inbox rules are inbox features — labels, assignment — and a paused conversation is exactly when an agent wants them." So your rules keep running while automation is paused. Do not add a pause check.
- A hook that does nothing may simply return; `hooks.py` line 127 names "L6-C applying a label" as the case.

`apps/flows/triggers/pipeline.py` notes the router does **not** hold the contact lock across your stage, on purpose — "holding a lock across L6-C's inbox rules would put a state machine inside it". If a rule of yours needs the lock, take it yourself, in your own transaction, via `apps.queueing.locks.contact_lock`.

Reminders and scheduled replies schedule through the queue: `ActionType.REMINDER` and `ActionType.SCHEDULED_REPLY` are already spelled in `apps/queueing/models.py`, and that enum is deliberately open so you register handlers without an `AlterField` in `apps/queueing`.

`apps/inbox/` today is deliberately small — `ConversationRead` is the only model and `mark_read` the only service. `apps/inbox/rendering.py` returns view models and **never HTML**, with an AST scan that keeps `mark_safe` out of the package, and URLs pass `apps.common.validators.is_renderable_url` before becoming an `href` or `src`. Labels and rule descriptions are user-authored text on that same attacker-content → team-browser path (SECURITY-BASELINE §2). Extend the rendering layer; do not route around it.

### Merge one at a time

Layer 4 merged four PRs inside 90 seconds, the CI concurrency group cancelled the intermediate runs, and a collision between two of them reached `main` with no red build to show for it. Layer 5 merged one at a time and every run completed green. Keep doing that: merge, let the post-merge run on `main` finish, then merge the next.

---

## Trigger — #22 (L6-A, sequences + rule triggers)

````
Implement issue #22 in brightbeanxyz/brightbean-chat: [L6-A] Sequences (drip campaigns) and rule triggers on internal events.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-6.md — its "Ground rules" name the exact seams Layers 3-5 reserved for you, several of which are named-and-empty so that your work is a registration rather than an edit. layer-2/3/4/5 ground rules still bind. Also docs/SPEC.md §§11.2, 11.4 and 12, docs/ROADMAP.md contracts 5, 7 and 8, and CONTRIBUTING.md.

Specifics for you:
- THE APP IS CALLED `campaigns`, and that is a contract, not a preference. apps/flows/picklists.py::_sequences calls installed_model("campaigns", "apps.campaigns", "Sequence") and returns [] while it resolves to nothing. Ship apps/campaigns/ with a Sequence model exposing `name` and .objects.for_workspace(...), and the builder's dropdown fills itself with no edit to apps/flows/. A different app label leaves that resolver permanently empty and nothing fails loudly.
- The subscribe_sequence / unsubscribe_sequence SCHEMAS already ship in apps/flows/schema/nodes.py. You register the RUNTIME via apps.flows.engine.registry.register_verb. Read apps/flows/engine/nodes/action.py first: an unregistered verb currently logs a warning and moves on, which is the documented no-op you are replacing. Do not add a second schema.
- The `sequence` condition source is already DECLARED in apps/contacts/conditions.py with a None handler and the note "issue #22, L6-A". Fill it via register_source. Set-wise through queryset(), never a Python loop over contacts.
- Rule triggers consume the EVENT CATALOG (contract 7), NOT the inbound routing pipeline. ROADMAP contract 6 says so explicitly. A rule firing on contact.tag_added connects to that Django signal in apps/contacts/events.py. Do NOT register a routing hook for it.
- You emit sequence.subscribed / sequence.unsubscribed. The convention is a per-app EVENT_CATALOG: dict[str, Signal] module — see apps/contacts/events.py and apps/messaging/events.py — which apps/api/events.py::discover_catalog unions automatically, so L5-F's outbound webhooks pick you up with no edit. Payloads carry workspace id, contact id and event-specific ids ONLY. Never message bodies.
- Sequence steps schedule through apps.queueing.registry.schedule(ActionType.SEQUENCE_STEP, ...). `workspace=` is required and keyword-only; schedule_system is for deployment-level rows and is not what you want. Give every step an idempotency_key — the queue returns the existing row rather than a duplicate.
- Sends go through apps.messaging.services. apps/messaging/tests/test_write_sites.py runs an AST scan that fails the build on a second write site.
- apps/contacts/views.py currently ships a disabled subscribe/unsubscribe pair with the body "Subscribing contacts to a sequence arrives with issue #22 (L6-A)." Enable it; do not build a third endpoint beside it.

Branch feat/l6a-sequences off main. One PR, "Closes #22", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Two siblings run in parallel in their own trees. You will contend on config/urls.py and the nav registry at most.
````

## Trigger — #23 (L6-B, broadcasts)

````
Implement issue #23 in brightbeanxyz/brightbean-chat: [L6-B] Broadcasts — composer, eligibility filtering, batched fanout, live counters.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-6.md — its "Ground rules" carry a table of the seams already built for you by name, and the failure mode in this layer is building a second one beside the reserved one. layer-2/3/4/5 ground rules still bind. Also docs/SPEC.md §§6.3, 6.4 and 13, docs/ROADMAP.md contracts 1, 4 and 8, and CONTRIBUTING.md.

You are on the critical path (ROADMAP line 13: ... → L5 wave → L6-B → L7-E).

Specifics for you:
- The composer is HTMX with a single-node graph_json. ROADMAP line 43 says "no React embed" — the React island belongs to the flow builder, and a broadcast is one message. Reuse the config panels' DATA SHAPES, not the canvas.
- Eligibility is apps.messaging.compliance.eligible(...) — its docstring says "Exported for L6-B". Use annotate_eligibility when you need to show WHY someone was excluded. Do not re-derive window state or opt-out status.
- Targeting is apps.contacts.conditions.queryset(workspace, filter_json) — the same engine segments use, so an audience and a segment agree by construction.
- Rate limiting is apps.messaging.buckets (rate_for / capacity_for / try_acquire), per connection, already tuned per platform in apps/channels/policy.py. A broadcast must not bypass it and must not add a second throttle beside it.
- Fanout uses ActionType.BROADCAST_FANOUT to split and BROADCAST_SEND to deliver; both are already spelled in apps/queueing/models.py. Sends go through apps.messaging.services — apps/messaging/tests/test_write_sites.py fails the build on a second write site.
- CANCELLATION MUST BE REAL: a broadcast cancelled mid-fanout stops scheduling AND skips already-scheduled sends that have not run. Test it against a partially-drained queue.
- YOU OWN broadcast.finished. apps/api/events.py already lists it in SUBSCRIBABLE_EVENTS and apps/api/models.py notes nothing emits it yet — subscribing today is a deliberate no-op. Emit it through your app's EVENT_CATALOG and L5-F's delivery picks it up with no edit.
- Per-platform composer affordances read from the registries, never a branch: apps/channels/whatsapp_templates.py::approved_templates_for + variable_schema (both written for your composer by name), apps/channels/segments.py for SMS length (pure — you multiply by your own price), apps/channels/suppression.py::is_suppressed for email, apps/channels/capabilities.py for renderable blocks, apps/media_library/picker.py for media. No platform branch in apps/messaging/ and no {% if platform == "..." %} in a template.
- Live counters: decide the polling story deliberately. apps/common/polling.py already does conditional GETs with a (max(updated_at), count) version token, and templates/inbox/list.html documents the htmx-swaps-on-304 trap and its fix. Reuse both.

Branch feat/l6b-broadcasts off main. One PR, "Closes #23", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Two siblings run in parallel in their own trees. You will contend on config/urls.py and the nav registry at most.
````

## Trigger — #24 (L6-C, inbox v2)

````
Implement issue #24 in brightbeanxyz/brightbean-chat: [L6-C] Inbox v2 — labels, inbox rules engine, reminders, scheduled replies.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-6.md — its "Ground rules" name the exact seam Layer 4 reserved for you and two properties of it that were designed around your use case. layer-2/3/4/5 ground rules still bind. Also docs/SPEC.md §14, docs/ROADMAP.md contract 6, docs/SECURITY-BASELINE.md §2, and CONTRIBUTING.md.

Specifics for you:
- YOUR STAGE IS `post_persist`. If you saw "pre_trigger" in an older ROADMAP, that was an error and has been corrected — no such stage exists. apps/flows/triggers/hooks.py defines the five: hard_optout → post_persist → resume → trigger → default_reply.
- READ apps/flows/triggers/stages.py FIRST. Its docstring addresses you by name and it is the worked example for writing a hook. post_persist ships EMPTY AND NAMED on purpose, so your rules are a registration rather than an edit to routing code.
- post_persist is in RUNS_WHILE_PAUSED, deliberately. hooks.py explains: inbox rules are inbox features — labels, assignment — and a paused conversation is exactly when an agent wants them. Your rules keep running while automation is paused. Do NOT add a pause check.
- The router does NOT hold the contact lock across your stage, on purpose — apps/flows/triggers/pipeline.py says holding one across your rules "would put a state machine inside it". If a rule needs the lock, take it yourself in your own transaction via apps.queueing.locks.contact_lock, which raises LockOutsideTransactionError if you are not in one.
- Reminders and scheduled replies schedule through the queue. ActionType.REMINDER and ActionType.SCHEDULED_REPLY are already spelled in apps/queueing/models.py, and that enum is deliberately NOT attached to the column as choices= so you register handlers without an AlterField migration in apps/queueing.
- apps/inbox/ is deliberately small today: ConversationRead is the only model, mark_read the only service. EXTEND it.
- Rendering is the security surface and it is enforced. apps/inbox/rendering.py returns view models and never HTML; an AST scan keeps mark_safe out of the package; URLs pass apps.common.validators.is_renderable_url before becoming an href or src; anything unshowable becomes a visible tombstone. Labels and rule descriptions are user-authored text on the same attacker-content → team-browser path (SECURITY-BASELINE §2). Extend that layer; do not route around it.
- Writes to conversations go through apps.messaging.services (open/close/assign_conversation, pause_automation) — ROADMAP contract 1, enforced by the AST scan in apps/messaging/tests/test_write_sites.py.
- Roles: use_inbox reads, reply_in_inbox writes. Do not invent a permission key; PERMISSION_KEYS in apps/members/roles.py is the whole vocabulary.

Branch feat/l6c-inbox-v2 off main. One PR, "Closes #24", all six CI jobs green. Extend the IDOR suite for every endpoint you add — tests/idor.py auto-discovers routes and RAISES UnregisteredRouteKwargError for any kwarg it cannot build, so you cannot quietly escape it.

Two siblings run in parallel in their own trees. You will contend on config/urls.py and the nav registry at most.
````

---

## Merge order and gate

Development is fully parallel. Merge order is free — no workstream owns a seam another needs. **Merge one at a time and let the post-merge run on `main` finish before the next**, as Layer 5 did.

**Layer 6 gate** — before opening Layer 7:

1. All three merged, six CI jobs green on `main`, with a completed run on each merge commit.
2. `apps/flows/picklists.py::_sequences` returns real rows with no edit to `apps/flows/` — proof the `campaigns` app label is right and `installed_model` did its job.
3. The `subscribe_sequence` verb runs from a flow, and the `sequence` condition source filters set-wise in one query.
4. `sequence.subscribed`, `sequence.unsubscribed` and `broadcast.finished` all reach an L5-F outbound webhook subscriber with no edit to `apps/api/`.
5. A broadcast cancelled mid-fanout leaves no send behind, proven against a partially-drained queue.
6. Eligibility, template selection, segment counting and suppression each have exactly one implementation, still — `grep` for a second one.
7. Inbox rules fire while automation is paused, and no rule writes a conversation field outside `apps.messaging.services`.
8. IDOR suite green and extended; security review over the merged diff; dependency audits clean.
9. **Deployment (#28) is still outstanding** and has now been carried past two gates. Layer 7 opens with L7-C, so this is the last point at which it can stop being deferred.
