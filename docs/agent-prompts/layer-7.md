# Layer 7 — Agent Prompts

Five workstreams, **all parallel**. Layer 6 is merged, so nothing here is blocked.

| Issue | | Owns | Permission key |
|---|---|---|---|
| [#26](https://github.com/brightbeanxyz/brightbean-chat/issues/26) | L7-A | `apps/analytics/`, `/c/` and `/o/` routes | `view_analytics` |
| [#27](https://github.com/brightbeanxyz/brightbean-chat/issues/27) | L7-B | flow export/import + templates | `edit_flows` |
| [#28](https://github.com/brightbeanxyz/brightbean-chat/issues/28) | L7-C | `deploy/`, prod compose, self-hosting docs | — (infra) |
| [#29](https://github.com/brightbeanxyz/brightbean-chat/issues/29) | L7-D | GDPR erasure/export, hardening pass | `manage_workspace_settings` |
| [#30](https://github.com/brightbeanxyz/brightbean-chat/issues/30) | L7-E | `tests/acceptance/` — SPEC §21 | — (tests) |

This is the last layer. The **issue body is the scope**, and after this the gate is the product rather than the next layer.

---

## Ground rules

Everything in [`layer-2.md`](layer-2.md) → [`layer-6.md`](layer-6.md) → "Ground rules" still binds. Read those first — this file adds only what Layer 6 now exports, plus the three things that are different about a final layer.

### What is different about Layer 7

1. **Two of these five are not features.** L7-C ships infrastructure and L7-E ships tests. Neither adds a model, and both are judged on whether someone *else's* work is now provable — a self-hoster can run this, and SPEC §21 is verified rather than asserted.
2. **L7-C unblocks the other four's acceptance criteria.** SPEC §21 phase 2 wants a WhatsApp template to "round-trip against a real WABA" and phase 3 wants a Make/Zapier-style scenario through the public API — neither is testable without a publicly reachable HTTPS deployment. #28's infra half was named in the Layer 4 *and* Layer 6 gates and has not landed. **Merge L7-C first.**
3. **L7-D audits everyone.** Its "baseline-traceability verification" is a review of the whole merged codebase against SECURITY-BASELINE, not of its own diff. Expect it to open issues rather than fix everything inline.

### `apps/analytics/` is the app label, and `flow_stats` is waiting for you

`apps/flows/api.py::flow_stats` already serves the real shape and says so:

```python
"""Per-node counters for the stats overlay — zeros until L7-A (issue #26).

The shape is the real one (``node_stat_daily`` in SPEC §5 counts sent,
delivered, failed and clicked), so L3-C can build the overlay against it now.
``available`` is false while there is nothing behind it; L7-A flips it.
"""
```

L3-C's builder overlay is already written against that payload. Fill it; do not change its shape without saying why.

SPEC §2 names `apps/analytics/ counters and stats views`. Layer 6 established that an app label in SPEC is a contract when something resolves through it (`campaigns.Sequence` was), and a collision risk when two same-layer workstreams share one. Nothing else in Layer 7 builds a Django app, so `analytics` is yours alone.

### The two public routes are reserved for you by name

`apps/common/signing.py`'s module docstring lists `/c/` click-tracking redirects and `/o/` open pixels as **#26's**, and `apps/channels/urls_public.py` says the same. Use `sign(payload, purpose=...)` / `unsign_or_404(...)` — one implementation for every public token route, one place to audit. A click redirect is also an open redirect if you let it be: the target must come out of the *signed payload*, never off the query string.

### Preview and test executions must stay out of the counters

Three places already promise this and will be wrong if you ignore them:

- `apps/flows/engine/runner.py` — `preview` marks an execution as a test run "which keeps it out of L7-A's counters".
- `apps/flows/models.py` — the same field, so "a few test sends" do not move real numbers.
- `apps/broadcasts/handlers.py` — skipped sends are "kept out of the analytics counters".

`apps/flows/engine/sending.py` also warns that a send routed around the facade would be invisible to your counters. Counters read what the facade wrote; they do not instrument a second path.

### Broadcast stats already exist — read them, do not recount

`apps/broadcasts/models.py` has a `stats` JSONField that L6-B updates in batches, and SPEC §13.2 asks for live counters to come from it. The broadcast stats *page* is yours; the numbers are already being kept. `node_stat_daily` is for flow nodes.

### The graph schema is versioned, and that is what makes export/import safe

`apps/flows/schema/export.py::json_schema()` emits a `SCHEMA_VERSION` constant from `apps/flows/schema/envelope.py`, and the artifact ships at `static/flows/flow-schema.json` with sorted keys and no timestamp — deliberately reproducible. An exported flow that does not carry its schema version is an import that cannot be validated later.

**Triggers are part of the round-trip.** ROADMAP line 46 and SPEC §21 phase 3 both say "incl. triggers", and triggers live in `apps/flows/triggers/` with their own config schema — a graph alone is not a flow. Ids that mean something in *this* workspace (a tag, a sequence, a channel connection) cannot be exported as raw ids: decide the mapping and write it down.

### Deletion is already a tombstone, and #29 owns turning it into a delete

`apps/contacts/services.py` is explicit: a soft-deleted contact becomes `status=deleted`, keeps its tag and field rows, and "issue #29 owns hard delete and GDPR export". Messaging rows are re-pointed rather than orphaned, through `apps.messaging.merge`, reached via `installed_model` so contacts still imports without messaging. Erasure has to cross the same boundaries in the same direction.

Encrypted columns (`EncryptedJSONField`, `EncryptedTextField` in `apps/credentials/models.py` and `apps/channels/models.py`) hold credentials, not contact PII. Do not conflate "erase a contact" with "rotate a workspace's secrets".

### The acceptance criteria already have partial tests — consolidate, do not duplicate

SPEC §21 splits into three phases and several criteria already have homes:

| Criterion | Already tested at |
|---|---|
| Webhook ack p95 < 500 ms | `apps/flows/tests/test_routing_inline.py::TestAckLatency` |
| Zero duplicate sends, 1k forced retries | `apps/messaging/tests/test_services.py::TestIdempotency` |
| 50 parallel events never interleave | `apps/flows/tests/test_locking.py` |
| Loop cap halts at 30 + admin notification | `apps/flows/tests/test_loop_cap.py`, `apps/notifications/tests/test_queue.py` |
| Exactly-once queue processing | `apps/queueing/tests/test_concurrency.py` |
| 10k-contact broadcast reconciles | `apps/broadcasts/tests/test_acceptance.py` |

What is **not** covered: first automated reply p95 < 2 s, the phase-3 Make/Zapier-style scenario end to end, and anything needing a real deployment. `tests/` already holds cross-cutting harnesses (`idor.py`, `ssrf.py`, `support.py`) — that is the pattern to follow.

### Merge one at a time

Layer 4 merged four PRs inside 90 seconds, CI cancelled the intermediate runs, and a collision reached `main` with no red build. Layers 5 and 6 merged one at a time and every run completed green. Keep doing that.

---

## Trigger — #28 (L7-C, deployment) — **merge this one first**

````
Implement issue #28 in brightbeanxyz/brightbean-chat: [L7-C] Deployment — Docker Compose, Heroku/Render/Railway one-click, self-hosting docs.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-7.md — its "Ground rules" explain why you merge first. layer-2 through layer-6 ground rules still bind. Also docs/SPEC.md §§2, 20 and 22, all of docs/SECURITY-BASELINE.md, and CONTRIBUTING.md.

YOU MERGE FIRST IN THIS LAYER, and you have been deferred twice. The Layer 4 and Layer 6 gates both asked for your infra half and neither got it. SPEC §21 phase 2 wants a WhatsApp template round-trip "against a real WABA" and phase 3 wants a Make/Zapier-style scenario through the public API — neither is testable without a publicly reachable HTTPS deployment, so four other workstreams' acceptance criteria are waiting on you.

Specifics for you:
- What exists: Dockerfile, docker-compose.yml, docker-compose.override.yml, Makefile, and config/settings/production.py which already sets SECURE_SSL_REDIRECT, HSTS with preload and subdomains, SECURE_CONTENT_TYPE_NOSNIFF, SECURE_REFERRER_POLICY, SECURE_PROXY_SSL_HEADER, and exempts /healthz from the SSL redirect because probes hit it over plain HTTP inside the network. What does NOT exist: any deploy/ directory, any production compose file, any TLS story, any self-hosting guide in docs/.
- Secure by default is the whole point. A self-hoster who follows your README must not end up with a deployment the community can be attacked through — that sentence is why SECURITY-BASELINE exists. No default secrets, no DEBUG, no permissive ALLOWED_HOSTS, no exposed Postgres port. If a value MUST be set before first boot, fail the boot with a clear message rather than defaulting.
- The Dockerfile runs collectstatic after `USER app` under production settings with CompressedManifestStaticFilesStorage; the CI build job asserts the container runs as non-root, that /healthz answers, and that the root page renders with no .env. Do not break those.
- No Redis (SPEC §22): Postgres is the queue, the lock manager and the rate limiter. A deployment guide that adds Redis is wrong.
- Background work: there is a worker and a tick. Document how they run under each target, and what happens if only the web process is up.
- PaaS one-click for Heroku/Render/Railway — app.json / render.yaml / railway.json equivalents, each with the same secure defaults and each actually deployable, not decorative.
- docs/deployment.md (or docs/self-hosting.md): first boot, secrets, TLS termination, backups, upgrades, the hardening checklist, and how to verify the deployment is healthy. Cross-reference SECURITY-BASELINE rather than restating it.
- Note in your PR which SPEC §21 criteria become testable once this lands, and how a maintainer runs them.

Branch feat/l7c-deployment off main. One PR, "Closes #28", all six CI jobs green.

Four siblings run in parallel in their own trees. Merge yours first so their acceptance work has somewhere to run.
````

## Trigger — #26 (L7-A, analytics)

````
Implement issue #26 in brightbeanxyz/brightbean-chat: [L7-A] Analytics — node counters, click tracking, builder stats overlay, broadcast stats.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-7.md — its "Ground rules" name the stub you are filling and three places that already promise preview sends stay out of your numbers. layer-2 through layer-6 ground rules still bind. Also docs/SPEC.md §5 (node_stat_daily), §19 and §21 phase 3, docs/ROADMAP.md contract 7, docs/SECURITY-BASELINE.md §4, and CONTRIBUTING.md.

Specifics for you:
- YOUR APP IS apps/analytics/ — SPEC §2 names it, and no sibling in this layer builds a Django app, so it is yours alone.
- apps/flows/api.py::flow_stats ALREADY SERVES YOUR SHAPE and its docstring says "available is false while there is nothing behind it; L7-A flips it". L3-C's builder overlay is written against that payload. Fill it; do not change the shape without saying why in the PR.
- The /c/ click-redirect and /o/ open-pixel routes are reserved for you BY NAME in apps/common/signing.py's docstring and apps/channels/urls_public.py. Use sign()/unsign_or_404() with a purpose salt — do not invent a second token format. A click redirect is an open redirect if you let it be: the destination comes out of the SIGNED PAYLOAD, never off the query string.
- PREVIEW AND TEST EXECUTIONS MUST NOT COUNT. Three places already promise this: apps/flows/engine/runner.py (preview "keeps it out of L7-A's counters"), apps/flows/models.py (so a few test sends do not move real numbers), apps/broadcasts/handlers.py (skipped sends "kept out of the analytics counters"). Honour all three.
- Counters read what the messaging facade wrote. apps/flows/engine/sending.py warns that a send routed around the facade would be invisible to you — do not instrument a second path to compensate, and do not add a write site (apps/messaging/tests/test_write_sites.py fails the build on one).
- Broadcast stats already exist: apps/broadcasts/models.py has a stats JSONField L6-B updates in batches, and SPEC §13.2 wants live counters from it. The stats PAGE is yours; the numbers are already kept. node_stat_daily is for flow nodes.
- Upserted counters, no per-event rows in v1 beyond message (SPEC §5). Nothing else: no funnels, no UTM builder (SPEC §19).
- Gate on view_analytics, which already exists in apps/members/roles.py PERMISSION_KEYS and is Agent+.

Branch feat/l7a-analytics off main. One PR, "Closes #26", all six CI jobs green. Extend the IDOR suite for every endpoint you add — and state a position on /c/ and /o/, which are public by design, the way tests/idor.py's three existing WAIVED_ROUTES entries do.

Four siblings run in parallel. #28 merges first.
````

## Trigger — #27 (L7-B, flow export/import)

````
Implement issue #27 in brightbeanxyz/brightbean-chat: [L7-B] Flow export/import and shareable flow templates.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-7.md — its "Ground rules" explain why the schema version is what makes this safe and why triggers are part of the round-trip. layer-2 through layer-6 ground rules still bind. Also docs/SPEC.md §16 and §21 phase 3, docs/ROADMAP.md contracts 2 and 5, docs/SECURITY-BASELINE.md §§3 and 7, and CONTRIBUTING.md.

Specifics for you:
- apps/flows/schema/export.py::json_schema() already emits SCHEMA_VERSION from apps/flows/schema/envelope.py, and the artifact ships at static/flows/flow-schema.json with sorted keys and no timestamp — deliberately reproducible. An export that does not carry its schema version is an import that cannot be validated later.
- TRIGGERS ARE PART OF THE ROUND-TRIP. ROADMAP line 46 and SPEC §21 phase 3 both say "incl. triggers". They live in apps/flows/triggers/ with their own config schema; a graph alone is not a flow, and the acceptance criterion is an explicit round-trip including them.
- Workspace-local ids are the hard part. A tag, a sequence, a channel connection and a media asset all mean something only in the workspace that owns them. Decide the mapping — export by name and re-resolve, export a manifest of what must be supplied, or refuse — and write the decision in the PR and in the docs. Silently importing a dangling id is the failure mode.
- AN IMPORT IS UNTRUSTED INPUT. A shared template comes from a stranger. Validate against the schema before anything touches the ORM (apps/flows/schema/validate_graph is the existing entry point), enforce MAX_NODES/MAX_EDGES from envelope.py, and remember SECURITY-BASELINE §3: message bodies carry {{placeholders}} rendered by apps/flows/rendering.py, never by a Django Template built from imported text. An imported flow must not be able to reach an SSTI.
- An imported external_request node points at a URL the importer did not choose. apps/common/outbound.py::guarded_request already refuses private addresses at send time — say in the PR whether an import surfaces that URL to the user before it can run.
- Imports create a FlowVersion (apps/flows/models.py: monotonic per flow, at most one published). Import as an unpublished draft; publishing stays a human action.

Branch feat/l7b-flow-portability off main. One PR, "Closes #27", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Four siblings run in parallel. #28 merges first.
````

## Trigger — #29 (L7-D, GDPR and security hardening)

````
Implement issue #29 in brightbeanxyz/brightbean-chat: [L7-D] GDPR contact delete/export and security hardening pass.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-7.md — its "Ground rules" name where deletion already stops today. layer-2 through layer-6 ground rules still bind. Also docs/SPEC.md §§5, 11.8 and 15, ALL of docs/SECURITY-BASELINE.md, and CONTRIBUTING.md.

YOU AUDIT EVERYONE. The "baseline-traceability verification" half of this issue is a review of the whole merged codebase against SECURITY-BASELINE, not of your own diff. Expect to open issues for what you find rather than fixing everything inline — a hardening PR that also rewrites four other apps is unreviewable. Say in the PR what you found, what you fixed, and what you filed.

Specifics for you:
- Deletion already stops half way, deliberately, and the code says your name. apps/contacts/services.py: a soft-deleted contact becomes status=deleted, keeps its tag and field rows as a tombstone, and "issue #29 owns hard delete and GDPR export". apps/contacts/models.py says the same, and adds that erasure "needs identities and message bodies to mean anything". apps/channels/models.py notes a hard delete takes a different path from a soft one.
- Cross-app deletion goes the way merges already go: apps.messaging.merge re-points messaging rows rather than orphaning them, and apps/contacts reaches it through installed_model so contacts still imports in a deployment without messaging. Erasure must cross the same boundaries in the same direction — do not add a direct import from contacts to messaging.
- Export includes consent records (SPEC §11.8) — opt_in_at, opt_in_source, opted_out_at live on the messaging identity models. An export that omits consent is the part a regulator asks about.
- Encrypted columns hold CREDENTIALS, not contact PII: EncryptedJSONField/EncryptedTextField in apps/credentials/models.py and apps/channels/models.py. Do not conflate erasing a contact with rotating a workspace's secrets.
- Writes go through the existing facades — apps.messaging.services, apps.contacts.services. apps/messaging/tests/test_write_sites.py runs an AST scan that fails the build on a second write site, and an erasure routine is exactly the code that would be tempted to reach past it.
- The pen-test runbook is a deliverable, not a formality: what to point a scanner at, which routes are public by design (tests/idor.py's WAIVED_ROUTES documents three, each cross-referencing the test class that stands in for the sweep), and what a finding should look like when someone reports one. Include a SECURITY.md disclosure policy if the repo has none.
- Erasure is irreversible and destructive. It needs confirmation, an audit record of who ran it and when, and a test that proves it removed what it claims and nothing else.

Branch feat/l7d-gdpr-hardening off main. One PR, "Closes #29", all six CI jobs green. This issue gets a dedicated security review at PR time.

Four siblings run in parallel. #28 merges first.
````

## Trigger — #30 (L7-E, acceptance and performance suite)

````
Implement issue #30 in brightbeanxyz/brightbean-chat: [L7-E] Cross-system acceptance and performance test suite (SPEC §21 criteria).

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-7.md — its "Ground rules" carry a table of which §21 criteria ALREADY have tests and where, so you consolidate rather than duplicate. layer-2 through layer-6 ground rules still bind. Also docs/SPEC.md §21 in full, docs/ROADMAP.md (you are the end of the critical path, line 13), and CONTRIBUTING.md.

You are the last workstream on the critical path and the issue that decides whether "it works" is an assertion or a fact.

Specifics for you:
- CONSOLIDATE, DO NOT DUPLICATE. Already covered: webhook ack p95 < 500 ms (apps/flows/tests/test_routing_inline.py::TestAckLatency), zero duplicate sends across 1k forced retries (apps/messaging/tests/test_services.py::TestIdempotency), 50 parallel events never interleaving (apps/flows/tests/test_locking.py), loop cap at 30 plus admin notification (apps/flows/tests/test_loop_cap.py + apps/notifications/tests/test_queue.py), exactly-once queue processing (apps/queueing/tests/test_concurrency.py), 10k-contact broadcast reconciliation (apps/broadcasts/tests/test_acceptance.py). A second copy of any of these is worse than none — it drifts.
- NOT COVERED, and yours: first automated reply p95 < 2 s on a 2 vCPU box; the phase-3 Make/Zapier-style scenario end to end (inbound webhook → API contact update → flow start, using ONLY the public API and outbound webhooks); flow export/import round-trip including triggers (coordinate with #27 on which side owns it); STOP suppressing within one inbound event; unsubscribe suppressing email within one click.
- tests/ already holds the cross-cutting harnesses — idor.py, ssrf.py, support.py, and the fixtures directory. Follow that pattern; a shared harness other suites import beats a monolith.
- Performance assertions on CI runners are how you get a flaky suite. This repo has been bitten three times by tests asserting on a clock-derived value (PRs #46, #49, #62). Decide deliberately how a p95 assertion behaves on a noisy runner — a generous ceiling that still catches a regression, a separate opt-in marker, or measured-and-reported rather than asserted — and write the reasoning down. A suite that goes red at random gets ignored, which is worse than not having it.
- Some §21 criteria need a real deployment (WhatsApp template round-trip against a real WABA) and some need real platform credentials. #28 lands the deployment. Say clearly which criteria are verified in CI, which need a manual run against a live deployment, and how a maintainer performs the second kind.
- Report the phase-1/2/3 criteria as a checklist with its current state, so the gate is readable rather than inferred.

Branch feat/l7e-acceptance off main. One PR, "Closes #30", all six CI jobs green.

Four siblings run in parallel. #28 merges first, and its deployment is what makes several of your criteria runnable.
````

---

## Merge order and gate

**#28 merges first.** Everything else is free, and merges one at a time with the post-merge run on `main` allowed to finish.

**Layer 7 gate** — this is the v1 gate, not a handoff to Layer 8:

1. All five merged, six CI jobs green on `main`, with a completed run on each merge commit.
2. `flow_stats` reports `available: true` with real per-node counters, and preview executions are absent from them.
3. `/c/` and `/o/` refuse a tampered token with the same bare 404 every other public token route gives, and a click redirect cannot be pointed off-payload.
4. A flow exports, imports into a different workspace, and round-trips **including triggers**, with workspace-local ids handled by a documented rule rather than silently.
5. A self-hoster following `docs/` alone reaches a working HTTPS deployment with no default secret, no `DEBUG`, and Postgres not exposed.
6. Contact erasure removes identities, messages and consent records across app boundaries through the facades, is audit-logged, and is proven by a test to remove what it claims and nothing else.
7. SPEC §21's phase-1, phase-2 and phase-3 criteria each have a stated verification: a CI test, or a documented manual run against a live deployment.
8. SECURITY-BASELINE traceability verified across the merged codebase, with findings either fixed or filed.
9. IDOR suite green; dependency audits clean; security review over the merged diff.
