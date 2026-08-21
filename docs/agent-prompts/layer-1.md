# Layer 1 — Agent Prompts

Two workstreams, **fully parallel** — one agent each, dispatched at the same time once Layer 0 ([#2](https://github.com/brightbeanxyz/brightbean-chat/issues/2)) has merged. They own disjoint trees (#31 owns `apps/`, #32 owns `theme/` and `templates/`) and share only `config/settings/base.py` and `config/urls.py`, so expect one trivial rebase for whoever merges second.

Written against the real [BrightBean Studio](https://github.com/brightbeanxyz/brightbean-studio) source, so each names the exact files to port **and** the places where copying Studio verbatim would carry over a known defect (opt-in tenant scoping, inverted credential resolution, duplicated role hierarchies, two competing toast systems, a vestigial Tailwind config). Attach `brightbeanxyz/brightbean-studio` (read access) to both sessions.

Convention: one file per layer in this directory.

---

## Prompt 1 — Issue #31, L1-A

````
You are implementing GitHub issue #31 in the repo `brightbeanxyz/brightbean-chat`: `[L1-A] Tenancy, RBAC, auth, and platform credentials (port from Studio)`.

Read first (all on `main`): `docs/SPEC.md` §4 (roles) and §5 (`core` models), `docs/ROADMAP.md` (execution model, conventions), `docs/SECURITY-BASELINE.md` (§§1 and 8 are yours), and issue #31. Issue #2 (scaffold) has merged — build on it.

CONTEXT: BrightBean Chat is an open-source, self-hostable ManyChat clone. You are delivering multi-tenancy, roles, authentication, and the platform-credential store — the substrate every feature layer above depends on. Issue #32 (theme + base UI shell) is being implemented **in parallel by another agent**: it owns `theme/` and `templates/base.html`/`layouts/`/`components/`. You own apps and their templates. Your auth pages may be plain/unstyled; whichever of you merges second reconciles the styling in a small follow-up. Do not edit `theme/` or `templates/base.html`.

REFERENCE REPO: `brightbeanxyz/brightbean-studio`. Port from these, adapting as instructed below:
- `apps/organizations/models.py` (Organization: UUID pk, name, logo_url, default_timezone, billing_email, the deletion-workflow trio; `hard_delete`)
- `apps/workspaces/models.py` (Workspace: organization FK, name, icon, description, timezone, primary/secondary color with `validate_hex_color`, is_archived; `effective_timezone` property). Drop Studio's `approval_workflow_mode` and posting-defaults fields — they are social-publishing concepts. Add nothing chat-specific yet.
- `apps/members/` — models (OrgMembership, WorkspaceMembership, Invitation), `decorators.py`, `middleware.py` (RBACMiddleware), `services.py` (invitation lifecycle), plus its templates/views for member management.
- `apps/accounts/` — custom User (`AbstractBaseUser + PermissionsMixin`, `USERNAME_FIELD="email"`, UUID pk, `last_workspace_id` as a plain UUIDField not a FK), `adapters.py` (SocialAccountAdapter for Google), `middleware.py` (`AuthRateLimitMiddleware`), `signals.py` (`provision_organization_and_workspace` on `post_save` + the allauth `user_signed_up` receiver handling the pending-invite path), `views_signup.py` (invite-prefill signup).
- `apps/credentials/` — the encrypted per-org credential store: `models.py` (PlatformCredential with `EncryptedJSONField`, `REQUIRED_CREDENTIAL_KEYS` any-of alias groups, `derive_is_configured`, the `save()` invariant, `masked_credentials`), `forms.py` (the admin form that overrides the EncryptedJSONField with a real `forms.JSONField` — **without this, a no-edit admin save silently corrupts the data**), `admin.py` (all `has_*_permission` hooks return `is_superuser`, because opening the page decrypts secrets).
- allauth settings block from `config/settings/base.py` (`ACCOUNT_LOGIN_METHODS = {"email"}`, `ACCOUNT_SIGNUP_FIELDS`, adapters, Google provider wired from env vars `GOOGLE_AUTH_CLIENT_ID`/`_SECRET` rather than a DB SocialApp, 14-day sliding sessions, BCryptSHA256 hasher), and `templates/account/` + `templates/allauth/elements/`.
- Root `conftest.py` fixture style (plain `Model.objects.create`, no factory_boy).

ADAPT — TWO TIERS, TWO TABLES, exactly as Studio splits them (SPEC §4 is authoritative; read it before writing any of this). Member management is ORG-level, not workspace-level. Define in `apps/members/models.py`:

  # --- organization tier: spans workspaces, governs who may enter one ---
  class OrgRole(TextChoices): OWNER="owner"; ADMIN="admin"; MEMBER="member"
  hierarchy {owner: 3, admin: 2, member: 1}
  ORG_PERMISSION_KEYS = (   # (key, label) pairs, like Studio
      ("manage_members",              "Invite, remove, change org roles, assign workspace memberships"),
      ("manage_workspaces",           "Create and archive workspaces"),
      ("manage_platform_credentials", "Org-level platform app credentials"),
      ("manage_api_keys",             "Issue and revoke API keys for any workspace in the org"),
  )
  BUILTIN_ORG_PERMISSIONS = {   # SETS, not dicts — Studio's shape
      OWNER:  {all four}, ADMIN: {all four}, MEMBER: set(),
  }
  def has_org_permission(membership, key) -> bool   # membership may be None -> False

  # --- workspace tier: one workspace's data ---
  class WorkspaceRole(TextChoices): ADMIN="admin"; EDITOR="editor"; AGENT="agent"; VIEWER="viewer"
  hierarchy {admin: 4, editor: 3, agent: 2, viewer: 1}
  PERMISSION_KEYS = ["use_inbox", "view_analytics", "reply_in_inbox", "edit_contact_fields",
                     "manage_crm", "edit_flows", "send_broadcasts", "manage_channels",
                     "manage_workspace_settings"]
  BUILTIN_ROLE_PERMISSIONS = {   # dict[role, dict[key, bool]], every key listed explicitly
      admin:  all True,
      editor: all True except manage_channels, manage_workspace_settings,
      agent:  {use_inbox, view_analytics, reply_in_inbox, edit_contact_fields},
      viewer: {use_inbox, view_analytics},
  }

Note what moved: `manage_members` and `manage_api_keys` are ORG permissions and must NOT appear in the workspace table. Owner and admin hold identical org permission sets — they differ through hierarchy checks (only an owner may change an owner; the last owner cannot be removed or demoted; nobody may grant a tier at or above their own), not through the table.

Keep Studio's `WorkspaceMembership.effective_permissions` property as the single resolution point and sole protocol — the public API (issue #25) will duck-type a `VirtualMembership` against it, exactly as Studio's `apps/api/auth.py` does. Ship all four decorators with Studio's signatures: `require_org_role(min_role)`, `require_org_permission(key)`, `require_workspace_role(min_role)`, `require_permission(key)`, and the `@login_required` → `@require_*` → `@require_POST` stacking convention. Define each hierarchy ONCE and import it (Studio duplicates them across `decorators.py` and `services.py` with a "must match" comment — deviation 6 below).

Invitations are org-level: `Invitation(organization, email, org_role, workspace_assignments=[{workspace_id, role}], invited_by, token, expires_at, accepted_at)`. One invite places a person in the org and in the workspaces they need. Mount the member-management routes OUTSIDE the `/w/<workspace_id>/` prefix (Studio uses a flat `/members/`), and put "Team Members" under the Organization section of the settings layout, not the workspace section.

Port Studio's cross-tier rule: an **org owner is treated as a workspace admin in every workspace of their org**, while an org admin is bounded by actual workspace membership. Studio's `members/services.py` escalation guards go with it.

Name collision, on purpose: `OrgRole.ADMIN` and `WorkspaceRole.ADMIN` are different roles at different tiers. Qualify in prose and docstrings ("org admin" / "workspace admin"); the enum and decorator names disambiguate in code.

DELIBERATE DEVIATIONS FROM STUDIO — these are the traps; do not copy them:
1. **URL prefix.** `docs/SPEC.md` §16 specifies `/w/<workspace>/...`. Studio uses `workspace/<uuid:workspace_id>/` (singular) for scoped apps but `workspaces/<uuid>/settings/` (plural) for management — an inconsistency. Use `/w/<uuid:workspace_id>/` uniformly for workspace-scoped routes, and keep the **kwarg name `workspace_id`** because RBACMiddleware's whole resolution contract is `view_kwargs.get("workspace_id")`. No slugs.
2. **Tenant scoping is enforced, not opt-in.** Studio's `OrgScopedManager`/`WorkspaceScopedManager` only add a `.for_workspace(id)` helper; nothing overrides `get_queryset()`, so a view that forgets `.for_workspace(...)` leaks across tenants and relies purely on the middleware. Ship a **workspace-scoped base manager/queryset** (in `apps/common/`) that every tenant model must use, document the convention in CONTRIBUTING, and make cross-workspace object access return **404, never 403** (no existence oracle). This is SECURITY-BASELINE §1 and later layers depend on it.
3. **The IDOR fuzz helper is a deliverable, not a nice-to-have.** Build a reusable pytest utility that walks registered URL patterns and hits them as an authenticated member of a *different* workspace, asserting 404. Wire it over your own views now; every later PR that adds endpoints extends it. Prove it works by pointing it at a deliberately-broken view in a test.
4. **Credential resolution direction is INVERTED vs Studio.** Studio's `resolve_platform_credentials` is env-dominant with an org fallback. `docs/SPEC.md` §4 requires **workspace-level override → organization-level (Django admin) → deployment env vars**. Implement that order, add the workspace-level override table + UI (Admin role, encrypted values, in workspace settings), and unit-test all three levels including partial/incomplete credential sets. Do not port Studio's direction.
5. **Drop `CustomRole`.** It exists in Studio with no UI and is ignored by `require_workspace_role` despite its docstring. Four built-in roles only.
6. **Single source of truth for role hierarchies.** Studio duplicates the level maps in `decorators.py` and `services.py` with a "must match" comment. Define once, import in both.
7. **Auth hardening.** Port `AuthRateLimitMiddleware` (per-IP, POST-only, on login/signup/password-reset) but **do not trust `X-Forwarded-For` unconditionally** as Studio does — gate it behind a `TRUSTED_PROXIES` setting, defaulting to not trusting the header. Keep responses enumeration-safe. **Set `ACCOUNT_EMAIL_VERIFICATION = "optional"`** — this is decided, not open: Studio ships `"none"`, but ours sends a verification email on signup while never blocking access on it, so a self-hoster with no SMTP configured is never locked out of their own instance. Keep the console email backend in development so local signup stays frictionless, and document the SMTP requirement in `.env.example`.
8. **Provisioning + tests.** Port the signup auto-provisioning signal, but note Studio's test suite copy-pastes a `_make_user()` teardown helper in two modules because every `create_user` auto-creates an Org+Workspace and `RBACMiddleware` resolves org with `.first()`. Promote that to a shared conftest fixture (or make provisioning opt-in for tests) instead of duplicating it.
9. Drop Client-role/portal concepts entirely — no client portal, no approval workflows.

ALSO NOTE (carry forward, don't fix now): RBACMiddleware assumes one org per user (`.first()`); keep the assumption but document it in the middleware docstring. The archived-workspace asymmetry in Studio (the `last_workspace_id` fallback filters `is_archived=False` but URL resolution does not) is a bug — fix it in your port.

CONSTRAINTS:
- Branch `feat/l1a-tenancy-rbac` off `main`; one PR; `Closes #31`.
- Do not touch `theme/`, `templates/base.html`, `templates/layouts/`, `templates/components/` — issue #32 owns them.
- Tests required: BOTH permission tables (a workspace viewer cannot POST anywhere; a workspace agent is blocked from flow routes; a workspace admin who is only an org *member* cannot reach member management, API keys or workspace creation; an org owner is treated as workspace admin in a workspace they hold no membership in), credential resolution across all three levels, invitation escalation rules (cannot invite at or above your own org tier, cannot remove or demote the last org owner, cannot remove yourself), auth rate limiting, and the IDOR helper.
- Follow `docs/SECURITY-BASELINE.md`.

DEFINITION OF DONE: signup → org+workspace provisioning → an org-level invitation carrying an org role plus workspace assignments → workspace switcher all work; both permission tables enforce exactly what SPEC §4 specifies, with member management and API keys reachable only through the org tier; credentials resolve workspace → org → env and are stored encrypted; the workspace-scoped manager and IDOR fuzz helper exist, are documented, and are proven by tests. In the PR body, list the Studio files ported and confirm each of the 9 deviations above.
````

---

## Prompt 2 — Issue #32, L1-B

````
You are implementing GitHub issue #32 in the repo `brightbeanxyz/brightbean-chat`: `[L1-B] Theme, base UI shell, and reusable components (port from Studio)`.

Read first (all on `main`): `docs/SPEC.md` §2, `docs/ROADMAP.md` (conventions — every later UI issue builds on what you ship), `docs/SECURITY-BASELINE.md` (§8, CSP nonces), and issue #32. Issue #2 (scaffold) has merged — build on it.

CONTEXT: BrightBean Chat is an open-source, self-hostable ManyChat clone whose UI must look and behave like BrightBean Studio (Django templates + HTMX + Alpine + Tailwind 4; the only React in the product is a flow-builder island in a much later layer). You are shipping the design system and app shell that ~15 later issues render into. Issue #31 (tenancy/auth) is being implemented **in parallel by another agent**: it owns `apps/*` and auth templates. You own `theme/` and `templates/` base/layouts/components/partials. Do not edit `apps/` beyond adding `apps/common/htmx.py`, `context_processors.py`, and `templatetags/`. Whichever of you merges second reconciles the auth-page styling in a small follow-up.

REFERENCE REPO: `brightbeanxyz/brightbean-studio`. Port:
- `theme/` app: `apps.py` + `static_src/{package.json, src/styles.css}`. Studio's npm scripts are `tailwindcss -i ./src/styles.css -o ../static/css/dist/styles.css [--watch|--minify]` with `@tailwindcss/cli` ^4; output lands in `theme/static/css/dist/` (gitignored build artifact) and is served because `theme` is in LOCAL_APPS. Add `theme` to INSTALLED_APPS and wire the build into CI and the Dockerfile.
- `theme/static_src/src/styles.css` — the whole ~800-line design system. Copy wholesale. Its contract is a three-layer token architecture in one `:root`: **Layer 1 brand tokens** (`--brand-50…900` orange ramp with `--brand-500:#F97316`; `--brand-green-*` used as the selection-control accent; three font stacks), **Layer 2 semantic tokens** (`--primary*`, warm-stone `--neutral-*`, `--surface-0/page/1/2/3`, `--border*`, `--text-primary/secondary/tertiary/ghost/inverse`, status quads, six accent pairs, `--shadow-xs…xl` + `--shadow-primary`, radii, `--ease-out`/`--ease-spring`/`--dur-*`), **Layer 3 component classes** (`.sidebar-nav-item`, `.btn-brand`/`.btn-outline`/`.btn-link`, ~18 CSP-safe hover utilities that exist specifically because inline handlers are forbidden, `.focus-ring`/`.input-focus`/`.form-input-styled`, `.alert-*`, `.auth-card`/`.auth-bg`, the flatpickr theme override block, and `.bb-checkbox`/`.bb-select`/`.bb-toggle`). Rebranding means editing only the ~14 Layer-1 values. Note these tokens are plain CSS custom properties, not Tailwind theme entries — templates consume them via `style="…var(--token)"` or the component classes, so `bg-primary` does not exist. Keep it that way.
- `templates/base.html` — the shell. Port as a unit: the pre-paint `<script nonce>` reading `localStorage.sidebarCollapsed` and stamping `html.sidebar-is-collapsed`, the head `<style>` block with `[x-cloak]` + `.sidebar-initial` rules that mirror every collapsed state, and the Alpine `x-data`/`x-effect`/`x-init` handoff on the shell wrapper and `<aside>`. **These three layers only work together** — porting one without the others produces a flash-of-expanded-sidebar. Keep the authenticated-shell / `{% block auth_content %}` split, the blocks (`html_style`, `title`, `extra_head`, `sidebar_nav`, `page_header`, `content`, `auth_content`, `extra_js`), the mobile off-canvas behavior, and the `htmx:configRequest` listener that injects `X-CSRFToken`.
- `templates/layouts/settings.html` + `workspace_settings.html` — Studio expresses "settings section" by overriding `{% block sidebar_nav %}` wholesale (no tab bars, no two-column). Keep that approach; rewrite the nav items for chat (Account: Profile, Preferences · Organization: General, Workspaces, Team Members, API Keys · Workspace: General, Channels, Fields, Tags).
- `templates/components/ui_select.html` + the `{% ui_select %}` inclusion tag in `apps/common/templatetags/common_extras.py` (keyword-only signature; normalizes dicts/tuples/strings/model instances to `{value,label,icon}`; `model` is an Alpine expression string interpolated into `x-text`/`:class`/`@click`; the panel is `position: fixed` and anchored via `getBoundingClientRect()` so toolbar overflow can't clip it). Also port the `json_attr` filter (json.dumps + HTML-escape, safe inside `x-data`).
- `apps/common/htmx.py` — `trigger_response(triggers, status=204)` and `toast_response(*, tone, title, body="", events=None)` where tone ∈ success|info|warn|error. 27 lines; port verbatim.
- `apps/common/context_processors.py` — Studio's `sidebar_context` shape (returns `{}` for anonymous users; function-local model imports to dodge app-loading cycles). Rewrite its queries for chat: workspaces list, `can_create_workspace`, connected channel connections, unread inbox count. Where a later-layer model doesn't exist yet, return an empty list and leave a `# TODO(L<n>-<x>)` marker.
- `templates/partials/_platform_icon.html`.

DELIBERATE DEVIATIONS FROM STUDIO — do not copy these:
1. **Do not port `theme/static_src/tailwind.config.js`.** It is vestigial: Tailwind 4 is CSS-first (`@import "tailwindcss"` + `@source` directives in styles.css), the v4 CLI never reads it, and its `brand.primary` indigo `#4f46e5` directly contradicts the orange tokens in styles.css. Also drop the `django-tailwind` dependency, the `"tailwind"` INSTALLED_APPS entry and `TAILWIND_APP_NAME` — Studio carries all three but never invokes `manage.py tailwind`; the npm scripts are the real build.
2. **Unify the toasts.** Studio has two independent systems: an Alpine red error box in `base.html` fed by `htmx:responseError`, and a separate vanilla-JS bottom-center host in `templates/approvals/partials/_toasts.html` that must be manually included once per page, outside htmx-swapped regions. Merge them into **one** toast host in `base.html` listening for both `showToast` (from `HX-Trigger`) and htmx errors, so `toast_response(...)` works on every page with no per-page include. Keep the XSS-safe rendering (`textContent`, and `DOMParser` for parsing error bodies — never `innerHTML`), the `data-no-error-toast` / `data-inline-error="#selector"` opt-outs, and the idempotent-init guard.
3. **One platform-icon partial, six platforms.** Studio has two divergent copies (`templates/partials/_platform_icon.html` with hard-coded brand hex and a `sm`/`md` size API and an `{% else %}` fallback; `templates/social_accounts/partials/_platform_icon.html` with `currentColor`, Tailwind numeral sizes, no fallback, and a base64 raster for one platform). Ship exactly one, covering **telegram, instagram, messenger, whatsapp, sms, email**, with the named `sm`(16px)/`md`(20px) API, a generic fallback glyph for unknown keys, and `currentColor` + a wrapper class so callers can tint per context (chat channels appear in many surfaces — brand-hex-only doesn't work).
4. **One active-state convention.** Studio computes sidebar active state inline per link against `request.resolver_match` at three different granularities (`url_name`, `app_name`, both) while the settings layouts use a view-supplied `settings_active` string — two conventions, 13 call sites. Standardize: build the nav as a data structure in the context processor with an `active` flag computed once, and render it in a loop. This is the highest-value cleanup in the port and every later UI issue benefits.
5. **Fold in the orphaned CSS.** `.cal-filter-select` / `.cal-filter-icon` — the trigger styles for `ui_select` — live inline in Studio's `templates/calendar/calendar.html`, not in styles.css, so the component is broken outside that page. Move them into styles.css (renamed generically, e.g. `.bb-filter-select`).
6. **Vendor the JS.** Studio vendors htmx and Alpine into `static/js/` but pulls flatpickr, chart.js and sortable from `cdn.jsdelivr.net` (requiring CDN entries in the CSP). Vendor **all** of them — a self-hosted product should not depend on a third-party CDN at runtime, and it lets you tighten `CSP_SCRIPT_SRC`/`STYLE_SRC` to `'self'` (plus `'unsafe-eval'` for Alpine and `'unsafe-inline'` for styles). Preserve the load ordering: htmx before the inline Alpine component definitions, Alpine (deferred) after them.
7. **Nav for this product**, not Studio's: Dashboard, Contacts, Flows, Inbox, Sequences, Broadcasts, Settings. Later issues replace the "coming soon" stubs. Do not port Studio's composer/calendar/idea-modal/notification-bell/chart bootstrapping from `base.html` — that is ~700 lines of app-specific JS you don't need. Keep the shell lean; a notification bell arrives with issue #7.
8. Rebrand all strings/logo slots to **BrightBean Chat**. No dark mode (Studio is light-only by design; don't invent one).

CONSTRAINTS:
- Branch `feat/l1b-theme-shell` off `main`; one PR; `Closes #32`.
- Every inline `<script>` carries `nonce="{{ request.csp_nonce }}"` (SECURITY-BASELINE §8). No inline event handlers — that is what the CSP-safe hover utility classes are for.
- No hardcoded colors outside the Layer-1 token block.
- The Tailwind build must be deterministic in CI and run before `collectstatic` in the Docker image.
- Coordinate lightly with #31: if the auth templates it adds are unstyled when you merge, restyle them in your PR; if you merge first, leave `{% block auth_content %}` ready and say so in the PR.

DEFINITION OF DONE: the app renders in the Studio visual style (shell, sidebar collapse persisting across reloads with no flash, toasts firing from a `toast_response(...)` view on any page, `{% ui_select %}` working outside any one page, settings layouts); CSP nonces present on every inline script and no CDN origins remain in the policy; a demo/smoke view exercises toast + ui_select + platform icons. In the PR body, list the Studio files ported and confirm each of the 8 deviations above.
````

---

## Coordination and verification

**Shared-file contention between #31 and #32** is limited to `config/settings/base.py` (INSTALLED_APPS, middleware, context processors) and `config/urls.py`. Both prompts state the ownership split; expect one trivial rebase.

**Layer-1 gate — verify before opening Layer 2:**

1. `docker compose up` from a clean clone → signup → org+workspace auto-provisioned → land on a styled dashboard shell.
2. Invite a member with each org role and each workspace role; confirm a workspace viewer cannot POST, a workspace agent is refused on flow routes, and an org member — whatever their workspace role — is refused on member management, API keys and workspace creation.
3. Set a platform credential at env, org and workspace level in turn; confirm resolution order workspace → org → env, and that stored values are encrypted at rest and absent from logs.
4. Run the IDOR fuzz suite; add a deliberately unscoped view and confirm it fails.
5. Toggle the sidebar, hard-reload, confirm no flash; trigger a `toast_response` view and confirm the toast renders without a per-page include.
6. `make lint typecheck test` plus `pip-audit`/`npm audit` all clean; production settings refuse to boot with secrets unset.
7. Security review over the layer's merged diff; dependency audits clean (`docs/SECURITY-BASELINE.md` §11).
