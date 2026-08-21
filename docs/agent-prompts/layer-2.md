# Layer 2 — Agent Prompts

Six workstreams, **all parallel**, dispatched together once Layer 1 has merged. Each owns its own Django app, so they touch disjoint trees.

| Issue | | App | Workspace permission key |
|---|---|---|---|
| [#3](https://github.com/brightbeanxyz/brightbean-chat/issues/3) | L2-A | `apps/contacts/` | `manage_crm` |
| [#4](https://github.com/brightbeanxyz/brightbean-chat/issues/4) | L2-B | `apps/channels/` | `manage_channels` |
| [#5](https://github.com/brightbeanxyz/brightbean-chat/issues/5) | L2-C | `apps/queueing/` | — (no UI beyond admin) |
| [#6](https://github.com/brightbeanxyz/brightbean-chat/issues/6) | L2-D | `apps/flows/` | `edit_flows` |
| [#7](https://github.com/brightbeanxyz/brightbean-chat/issues/7) | L2-E | `apps/notifications/` | — (per-user, not workspace-gated) |
| [#16](https://github.com/brightbeanxyz/brightbean-chat/issues/16) | L2-F | `apps/media_library/` | see ground rule 3 |

The **issue body is the scope** — it lists deliverables and acceptance criteria. This file carries what changed *underneath* those issues since they were written against an empty repo, plus the conventions Layers 0 and 1 established. Read both.

---

## Ground rules — what Layers 0 and 1 already shipped

Every one of these binds all six agents. None of it existed when the issue bodies were written.

**1. Tenant models inherit `WorkspaceScopedModel`** (`apps/common/scoping.py`), not a plain model plus a remembered filter. Its `objects` manager hands out querysets that **refuse to execute until scoped**:

```python
Contact.objects.filter(status="active")  # raises UnscopedQueryError
Contact.objects.for_workspace(ws).filter(...)  # fine
Contact.objects.unscoped().count()  # fine, and greppable
```

The guard fires at execution, not at `.filter()`, and covers `count()`, `exists()`, `update()`, `delete()`, `aggregate()` and `iterator()` — not just iteration. `Meta.default_manager_name`/`base_manager_name` point at the plain `all_objects` so admin, cascades and reverse managers keep working. Non-tenant models inherit `apps.common.models.BaseModel` (UUIDv7 pk + `created_at`/`updated_at`).

**2. Fetch scoped objects with `get_scoped_object_or_404`** (`apps/common/shortcuts.py`). Django's own `get_object_or_404` goes through `_default_manager` — the *plain* one — so it would look right and cross tenants. Cross-workspace access answers **404, never 403**.

**3. Permissions live in `apps.members.roles`**: `PERMISSION_KEYS`, `ROLE_PERMISSIONS`, `permissions_for_role`, `OrgRole`, `WorkspaceRole`, `ORG_ROLE_LEVEL`, `WORKSPACE_ROLE_LEVEL`. Decorators in `apps.members.decorators`: `require_permission`, `require_workspace_role`, `require_org_role`. Stack them `@login_required` → `@require_*` → `@require_POST`/`@require_GET`. Type your views with `WorkspaceRequest` / `OrgRequest` / `RBACRequest` from `apps.members.requests` so mypy passes.

There is **no media permission key**. #16 either reuses an existing key or adds one to `roles.py` — a shared file, and note `_ADMIN_ONLY_KEYS` is written as a subtraction, so a new key is granted to Editor unless you also add it there. Decide, say so in the PR, and keep the edit to one line.

**4. `apps.common.platforms.Platform`** is the canonical messaging-platform enum (telegram, instagram, messenger, whatsapp, sms, email). #4 **imports** it for the adapter/policy registry — its docstring names issue #4 explicitly. Do not define a second one.

**5. Rate limiting is `apps.common.ratelimit`** — a Postgres fixed-window limiter (`window_key()`, `hit()`) built because `DatabaseCache.incr` is a non-atomic get-then-set that loses counts under exactly the concurrency an attacker generates. Client IP comes from `apps.common.net.get_client_ip`, which ignores `X-Forwarded-For` unless the peer is in `TRUSTED_PROXIES`. #4's signature-failure throttle uses both.

**6. URLs.** Workspace routes mount under `/w/<uuid:workspace_id>/`; the kwarg name `workspace_id` is `RBACMiddleware`'s resolution contract — do not rename it. Org routes sit under `/organization/`. **Stub routes already exist** in `config/urls.py` naming the issue that replaces them — `contacts/`, `flows/`, `settings/fields/`, `settings/tags/` among them. Replace your stub; do not add a second route beside it.

**7. Navigation is a registry**, not markup: `MAIN_NAV` and `SETTINGS_NAV` in `apps/common/context_processors.py`. This is **the one file all six of you will touch** — add only your own entries and expect a rebase.

**8. UI.** `templates/base.html` exposes `title`, `extra_head`, `sidebar_nav`, `page_header`, `content`, `alpine_components`, `extra_js`. HTMX responses use `trigger_response` / `toast_response` from `apps/common/htmx.py`. **Visit `/ui/` first** — it is the living style guide, static markup with no side effects.

**9. The CSS cascade trap.** Design-system classes in `theme/static_src/src/styles.css` are *unlayered*; Tailwind emits utilities inside `@layer utilities`, and unlayered rules beat every cascade layer regardless of specificity or source order. So `class="btn-outline w-auto"` does **not** give you an auto-width button — `.btn-outline { width: 100% }` wins silently. Add a modifier class to `styles.css` instead. Utilities remain correct for properties no component class sets (layout, spacing, flex).

**10. Dependencies.** New packages go in `requirements.in`, then `make lock`, then commit both compiled `.txt` files. Installs use `--require-hashes` and CI recompiles and diffs them, so a stale lockfile fails the build.

**11. Tests.** `conftest.py` provides `tenancy` (one org, one workspace, a user in each of the four roles), `other_tenancy` (a second tenant — the attacker's side of every IDOR test), `client_for(user)`, `user`, and `secret_value`, plus autouse cache isolation. **Every PR that adds an endpoint extends the IDOR suite** — see `CONTRIBUTING.md`.

**12. Gates.** Six CI jobs: ruff (incl. the `S` security rules, 120 cols), mypy over `apps/ config/ tests/`, pytest on Postgres 16, dependency audit with a lockfile-staleness diff, Docker build, gitleaks. Read `CONTRIBUTING.md` before your first commit.

### One divergence to know about, not to fix

`docs/SPEC.md` §4 says `manage_members` and `manage_api_keys` are organization-tier keys that must not appear in the workspace table. The merged Layer 1 code keeps both in `PERMISSION_KEYS` and uses `manage_members` to scope *which workspaces* an inviter may grant into, while `@require_org_role` gates access to member management itself. Neither reading affects any Layer 2 permission key. Leave it alone; it is tracked separately.

---

## Trigger — #3 (L2-A, contacts domain + condition engine)

````
Implement issue #3 in brightbeanxyz/brightbean-chat: [L2-A] Contacts domain — contacts, tags, custom fields, segments, and the condition engine.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-2.md "Ground rules" — Layers 0 and 1 have merged and every ground rule binds you. Also docs/SPEC.md §5 (contacts) and §11.4 (condition schema), docs/ROADMAP.md contracts 7 and 8, docs/SECURITY-BASELINE.md §7, and CONTRIBUTING.md.

Specifics for you:
- Contact, Tag, ContactTag, CustomField, CustomFieldValue and Segment are tenant models: inherit apps.common.scoping.WorkspaceScopedModel. Every query goes through .for_workspace(); an unscoped one raises UnscopedQueryError at execution.
- You own ROADMAP contract 8 — CONDITION_SCHEMA, evaluate(), queryset(), and the pluggable source registry. L2-D (#6) embeds your schema for the condition node and L3-B calls evaluate(); get the exported names right and do not make them depend on anything unmerged. Register tag/custom_field/system_field/segment; leave `window` (L3-A) and `sequence` (L6-A) as registrable slots that raise clearly if evaluated.
- Set-wise evaluation compiles through the ORM only, with a field/operator allowlist — no string-built SQL (SECURITY-BASELINE §7). Fuzz it with hostile keys and values.
- You own contract 7's contact events: contact.created, contact.tag_added, contact.tag_removed, contact.field_changed. Emit them from contacts/services.py with the documented payload shapes. No subscribers in this issue.
- Replace the existing `contacts/`, `settings/fields/` and `settings/tags/` stub routes in config/urls.py rather than adding routes beside them; gate on manage_crm. The end-user CRM UI is #13 — this issue needs only what the issue body lists.

Branch feat/l2a-contacts off main. One PR, "Closes #3", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Five siblings run in parallel in their own apps (#4 channels, #5 queueing, #6 flows, #7 notifications, #16 media). The only file you are likely to contend on is apps/common/context_processors.py (nav registry) and config/urls.py — add only your entries.
````

## Trigger — #4 (L2-B, channels framework)

````
Implement issue #4 in brightbeanxyz/brightbean-chat: [L2-B] Channels framework — connections, adapter interface, webhook ingestion and event log.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-2.md "Ground rules" — Layers 0 and 1 have merged and every ground rule binds you. Also docs/SPEC.md §§5, 6.1 and 7, docs/ROADMAP.md contracts 4 and 6, docs/SECURITY-BASELINE.md §§2, 4 and 7, and CONTRIBUTING.md. This is a security-critical issue: your endpoints are the deployment's internet-facing surface, so expect a dedicated security review at PR time.

Specifics for you:
- Import apps.common.platforms.Platform for the registry — do NOT define a second platform enum. Its docstring names this issue: "issue #4 builds the adapter/policy registry (ROADMAP contract 4) around the same values. One enum, imported; not two that drift."
- ChannelConnection is a tenant model (WorkspaceScopedModel); WebhookEventLog hangs off the connection. Credentials use apps.common.encryption.EncryptedJSONField — note it cannot be used in .filter(), it silently matches nothing.
- The signature-failure throttle uses apps.common.ratelimit (window_key/hit) with the client address from apps.common.net.get_client_ip, which ignores X-Forwarded-For unless the peer is a trusted proxy. Do not read the header directly.
- You own contract 4 in full: the PlatformPolicy dataclass (outside_window blocked | needs_tag(tags, allowed_use_text) | needs_template, human_agent_days, broadcast_allowed, rate_default) and the static Capabilities table, both as registry data so L2-D can emit capability warnings without importing adapter code and Layer 5 adapters never patch can_send.
- You own contract 6's first half: the dispatch seam. verify → dedup → raw-persist → dispatch(NormalizedEvent) against a pluggable registration point that is a no-op stub here. L3-A registers persistence; L4-A registers routing. Do not hard-code either.
- Connection settings UI gates on manage_channels; keep it a minimal list/connect/status page — real per-platform connect flows belong to the adapter issues.
- Webhook endpoints: body-size cap and cheap-reject before any DB work, raw-body HMAC before JSON parsing, constant-time compares, 403 only for signature failures and 200 for business-logic failures, and hostile-payload fixtures (oversized, wrong types, injection strings in every string field).

Branch feat/l2b-channels off main. One PR, "Closes #4", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Five siblings run in parallel in their own apps. The only file you are likely to contend on is apps/common/context_processors.py (nav registry) and config/urls.py.
````

## Trigger — #5 (L2-C, Postgres task queue)

````
Implement issue #5 in brightbeanxyz/brightbean-chat: [L2-C] Postgres task queue — scheduled actions, worker, tick, housekeeping, advisory locks.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-2.md "Ground rules" — Layers 0 and 1 have merged and every ground rule binds you. Also docs/SPEC.md §§5, 9.6 and 15, and CONTRIBUTING.md. SPEC §22 is absolute: no Redis, ever — Postgres is the queue, the lock manager and the rate limiter.

Specifics for you:
- ScheduledAction is a tenant model (WorkspaceScopedModel), but the worker legitimately claims across tenants: use .unscoped() there, deliberately and greppably, and say why in the PR.
- apps.common.ratelimit already demonstrates the house pattern for a Postgres-backed concurrency-safe counter under select_for_update — read it before writing the claim query. Your claim uses the SPEC §15 statement verbatim: UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING *.
- You ship three registries other layers append to: the handler registry (type → callable), the housekeeping-job registry (L2-B's event-log prune, L3-B's stale-execution expiry, L5-C's template polling all register into it — your own body covers only zombie reset), and the advisory-lock helpers contact_lock / try_contact_lock in queueing/locks.py that the whole engine depends on for its one-step-per-contact invariant.
- /internal/tick uses the shared signing/token discipline: constant-time compare against TICK_TOKEN, 404 when unset. apps/common/signing.py exists if a signed payload fits better than a bare token — read its docstring, it documents the contract for exactly these consumers.
- Multiple workers plus tick must be safe by construction. Prove it: two workers and a tick over 1k due actions, every action processed exactly once, including forced mid-batch crashes with clock manipulation for the zombie sweep.

Branch feat/l2c-queueing off main. One PR, "Closes #5", all six CI jobs green.

Five siblings run in parallel in their own apps. Land early if you can — #4's tests want a real enqueue path.
````

## Trigger — #6 (L2-D, flows core)

````
Implement issue #6 in brightbeanxyz/brightbean-chat: [L2-D] Flows core — flow/version models, graph JSON schema, validation, and the builder data API.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-2.md "Ground rules" — Layers 0 and 1 have merged and every ground rule binds you. Also docs/SPEC.md §§5, 9.1, 11 (config schemas) and 16, docs/ROADMAP.md contracts 2, 4 and 8, docs/SECURITY-BASELINE.md §7, and CONTRIBUTING.md.

Specifics for you:
- Flow and FlowVersion are tenant models (WorkspaceScopedModel). No engine in this issue — no FlowExecution, no node runtime.
- You own contract 2: the shared node-config JSON-schema module in flows/schema/, plus the generated artifact the React builder imports at build time. L3-B and L3-C both build on it, so the schema for EVERY node type in SPEC §11 ships now, including ones whose runtime lands later (external_request, send_sms, send_email).
- The condition node embeds CONDITION_SCHEMA from apps.contacts.conditions (contract 8, issue #3) — import it, do not re-declare the operator table. #3 is a parallel sibling: SPEC §11.4 writes the schema out in full, so code against the written form and swap to the import when it lands. Merge after #3.
- Capability warnings read the static Capabilities table that #4 registers (contract 4) — registry data, not adapter code, so there is no import cycle. Same parallel-sibling rule: code against the contract as written in ROADMAP.
- Validation rejects unknown config keys (mass-assignment guard) and enforces graph_json size and depth caps before validating (SECURITY-BASELINE §7).
- Replace the existing `flows/` stub route in config/urls.py; gate on edit_flows. The edit route renders a mount-div template with a placeholder — L3-C fills the island.
- GET /api/flows/<id>/ returns pick-lists that degrade to empty arrays where the owning app has not landed (sequences until L6-A) — document the stub.

Branch feat/l2d-flows off main. One PR, "Closes #6", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Five siblings run in parallel in their own apps. Merge after #3 so the condition-schema import is real rather than vendored.
````

## Trigger — #7 (L2-E, notifications)

````
Implement issue #7 in brightbeanxyz/brightbean-chat: [L2-E] Notifications — in-app and email notification engine, ported from Studio.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-2.md "Ground rules" — Layers 0 and 1 have merged and every ground rule binds you. Also CONTRIBUTING.md.

Attach brightbeanxyz/brightbean-studio with read access — apps/notifications/ there is the port source.

Specifics for you:
- Notifications are per-user, not workspace-gated, so most of your models are BaseModel rather than WorkspaceScopedModel — but anything carrying a workspace FK must be WorkspaceScopedModel. Be deliberate about which is which and say so in the PR.
- notify(workspace, event_type, *, users=None|roles=("admin",), context) resolves recipients by role through apps.members — read apps/members/roles.py and models.py for the real membership API rather than assuming Studio's shape. "Notify workspace admins" is the loop-cap consumer in L3-B.
- Email delivery goes through a queueing scheduled action when apps.queueing is available (#5 is a parallel sibling) and falls back to synchronous send in tests. Register the handler type additively; do not edit #5's module.
- The bell UI mounts in the shell: templates/base.html exposes the blocks and apps/common/context_processors.py holds the nav registry — add your entries there and expect a rebase, it is the one file every Layer 2 agent touches. Visit /ui/ first for the design-system classes, and read ground rule 9 about the cascade trap before writing markup.
- Event types this product needs: flow_loop_cap_hit, flow_execution_failed, channel_needs_reauth, outbound_webhook_disabled, inbox_reminder, member_mentioned, broadcast_finished, whatsapp_template_reviewed. Registry is additive; unknown type raises in DEBUG and logs in production.

Branch feat/l2e-notifications off main. One PR, "Closes #7", all six CI jobs green.

Five siblings run in parallel in their own apps. You have no callers yet — later layers invoke notify().
````

## Trigger — #16 (L2-F, media library)

````
Implement issue #16 in brightbeanxyz/brightbean-chat: [L2-F] Media library, ported from BrightBean Studio.

Read first: the issue body (it is the scope), then docs/agent-prompts/layer-2.md "Ground rules" — Layers 0 and 1 have merged and every ground rule binds you. Also docs/SECURITY-BASELINE.md §§6 and 9, and CONTRIBUTING.md. This is a security-critical issue (uploads): expect a dedicated security review at PR time.

Attach brightbeanxyz/brightbean-studio with read access — apps/media_library/ there is the port source. Drop its Unsplash integration and platform-variant generation; keep thumbnailing.

Specifics for you:
- Assets and folders are tenant models: inherit apps.common.scoping.WorkspaceScopedModel, and fetch through get_scoped_object_or_404.
- There is NO media permission key in apps/members/roles.py. Either reuse an existing key or add one — roles.py is a shared file, and _ADMIN_ONLY_KEYS is written as a subtraction so a new key is granted to Editor unless you add it there too. Keep the edit to one line and justify the choice in the PR.
- Signed delivery URLs use apps/common/signing.py — sign()/unsign()/unsign_or_404 with a purpose salt and versioned payloads. Read its docstring: it documents the contract for exactly these consumers, and unsign takes accept_versions so a token-format change is a rollout, not a cutover that breaks every live URL.
- Upload hardening (baseline §9): content type by sniffing not extension; SVG/HTML/unknown served with Content-Disposition: attachment and nosniff; per-file and per-workspace quotas. Test the hostile cases — HTML disguised as PNG, SVG carrying script, oversized file.
- NO server-side fetching of user-supplied URLs in this issue. The shared SSRF guard lands with #15 (L4-E) and baseline §6 forbids the call site until then. Pass URLs through to platforms untouched.
- The picker endpoint is a contract for four later consumers (#10 builder, #23 broadcasts, #24 inbox, #21 email). Document its JSON shape, and ship media_library.resolve(media_id) -> {url, mime, kind} for the send path.
- Storage is env-switched already (STORAGE_BACKEND local|s3, generic S3_* names). Note the scaffold added AWS_CLOUDFRONT_KEY_ID/KEY and a common.W001 check because S3_CUSTOM_DOMAIN silently disables URL signing — your delivery URLs are what that check exists to protect.

Branch feat/l2f-media off main. One PR, "Closes #16", all six CI jobs green. Extend the IDOR suite for every endpoint you add.

Five siblings run in parallel in their own apps.
````

---

## Merge order and gate

Development is fully parallel. Only two soft couplings fix a **merge** order:

- **#3 before #6** — #6 imports #3's `CONDITION_SCHEMA`. SPEC §11.4 writes the schema out in full, so #6 codes against the written form from the start.
- **#5 early** — #4's tests exercise a real enqueue path, and #7's email delivery registers a handler.

#4, #7 and #16 are otherwise free. All six contend only on `apps/common/context_processors.py` (nav registry) and `config/urls.py`; expect trivial rebases.

**Layer 2 gate** — before opening Layer 3:

1. All six PRs merged, six CI jobs green on `main`.
2. Security review over the layer's merged diff; #4 and #16 additionally reviewed at PR time.
3. IDOR suite green and extended by every issue that added endpoints.
4. Dependency audits clean, lockfiles not stale.
5. Contracts 2, 4, 6, 7 and 8 exist as written — Layer 3 codes against them without reading Layer 2's internals.
