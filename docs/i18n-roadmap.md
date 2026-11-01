# i18n rollout — roadmap

## Context

The backbone shipped first: `LocaleMiddleware`, `LANGUAGES` (`en-us`, `ru`,
`ky`), a per-user `User.language` preference, a `gettext_lazy` pattern proven
against this codebase's frozen dataclasses (`NavItem`, `NotificationEvent`)
and mypy, and a translator workflow (`make i18n-extract` / `make
i18n-compile`). Alongside it, a pilot slice proved the pattern on real,
live pages: the app shell (`base.html`), the whole sidebar/settings
navigation, login/signup, Profile + Preferences, and the notification/invite
email templates — 12 of 156 Django templates, plus five Python modules
(`apps/notifications/events.py` and four callers). Verified end-to-end:
`ru`/`ky` render correctly through a real browser and inside the built
container image.

What's left is inventoried, not guessed:

| Surface | Size | Status |
|---|---|---|
| Django templates | 156 total, **12 done** | 144 remain, concentrated in 5 apps |
| Python strings (`ValidationError`, `messages.*`) | ~96 call sites across 13 apps | 0 done outside the pilot's 5 files |
| Public REST API errors (`apps/api/errors.py` + routers) | ~27 hardcoded strings | Undecided — see Phase 3 |
| React flow-builder (`frontend/builder`) | 57 files, ~80+ strings, zero i18n lib | Untouched, own workstream |
| `flow-templates/*.json` seed content | 43 files | Content, not code — separate track |

**54% of the remaining template work (84/144) sits in 5 apps**: channels (22),
flows (17), inbox (16), contacts (16), broadcasts (13). Python-string debt is
just as lopsided: channels alone carries 37 of the ~96 call sites.

The goal: turn "infra exists, pilot proven" into "the product is usable
end-to-end in ru/ky" without another giant diff — every phase is the *same
proven recipe* applied to one coherent, shippable slice at a time, in the
order that pays off fastest.

## Principles

1. **Same recipe every time, no new mechanism.** `{% load i18n %}` +
   `{% translate %}`/`{% blocktranslate %}` in templates, `gettext_lazy` for
   module-level Python copy, `gettext`/`%`-formatting for f-strings built at
   request time (the pattern `apps/members/services.py`'s invite subject
   already uses). No new library, no new abstraction until Phase 4.
2. **One app = one PR = one shippable unit.** Never a cross-app batch. Each
   phase is sized to review and merge on its own, and each one leaves the app
   fully translated — no half-migrated template directories.
3. **Order by (traffic × visibility), not by file count.** Channels first —
   the largest single Python-string debt *and* the first thing a new
   non-English operator touches after signup. Inbox second — the
   highest-frequency screen for anyone actually running support.
4. **Every phase ends with translations actually filled in**, not just
   extracted. `make i18n-extract` → fill `msgstr` for the new msgids → `make
   i18n-compile` → verify in a browser, every time.
5. **The regression guard grows with the coverage, not ahead of it.**
   `tests/test_i18n_coverage.py` deliberately only checks tag presence on a
   fixed allowlist today — a stronger check would be noise while most of
   `templates/` is untouched. Upgrade point: Phase 2's completion (see
   Cross-cutting).
6. **Decide scope before touching the API, don't sweep it by default.** The
   public REST API's error `message` field is machine-consumed; translating
   it risks breaking any client that string-matches on it. Recommendation in
   Phase 3: don't translate it, keep `error.code` as the stable field.
7. **Content isn't code.** `flow-templates/*.json`'s seed copy is authoring
   work, not a code change — listed once at the end so it isn't forgotten,
   not scheduled as an engineering phase.

## Roadmap

| Phase | Scope | Templates | Python strings | Depends on |
|---|---|---|---|---|
| 0 | Infra + pilot | 12 | 5 files | done |
| 1 | Settings area: organizations, members, workspaces, billing, API-keys/webhooks HTML | ~24 | ~39 | Phase 0 |
| 2a | Channels | 22 | 37 | Phase 0 |
| 2b | Inbox | 16 | 0 | Phase 0 |
| 2c | Contacts | 16 | 5 | Phase 0 |
| 2d | Flows (Django wrapper pages only, not the React builder) | 17 | 5 | Phase 0 |
| 2e | Broadcasts | 13 | 0 | Phase 0 |
| 2f | Small remainder: campaigns, analytics, media_library, remaining partials/layouts/root pages | ~26 | ~11 | Phase 0 |
| 3 | Public REST API — decision, then optional work | — | ~27 | Phase 0 |
| 4 | React flow-builder (`frontend/builder`) — independent track, can run in parallel with Phase 2 | 57 files | — | Phase 0 only |
| — | `flow-templates/*.json` content translation | 43 files | — | Content track, not scheduled here |

### Phase 1 — finish Settings

Why first: Preferences/Profile are already done, so a user who opens Settings
today gets a jarring half-English page the moment they click any other tab.
Smallest, most self-contained slice — a warm-up rep of the pattern before the
big apps.

- Templates: `templates/organizations/*` (3), `templates/members/*` minus
  the 2 pilot email files (6), `templates/workspaces/*` (2), `templates/api/*`
  (6, the API-keys/webhooks settings pages — **not** the REST API itself, see
  Phase 3), plus the billing templates under the org settings tree.
- Python: the `messages.*`/`ValidationError` calls in
  `apps/organizations/views.py`, `apps/members/views.py`,
  `apps/workspaces/views.py`, `apps/billing/views.py`,
  `apps/api/views_keys.py`, `apps/api/views_webhooks.py`. Several are
  `str(exc)` passthroughs — fix at the model/service layer that raises, not
  the view that catches, so every caller of that validation gets the fix (the
  same root-cause call Phase 0 made in `apps/notifications/engine.py`).

### Phase 2 — the core product (5 sequential app-sized PRs)

Same recipe, repeated once per app, in this order: **channels → inbox →
contacts → flows → broadcasts → (campaigns/analytics/media_library bundle)**.
Each PR:
1. `{% load i18n %}` + wrap every template in the app directory.
2. Wrap the app's `messages.*`/`ValidationError` strings (channels has by far
   the most: `forms.py`, `forms_whatsapp.py`, `views_telegram.py`,
   `views_email.py`'s hardcoded `JsonResponse` bodies).
3. `make i18n-extract`, fill `ru`/`ky` `msgstr`, `make i18n-compile`.
4. Extend `tests/test_i18n_coverage.py`'s `_I18N_TEMPLATES`/`_I18N_MODULES`
   lists with the new files.

Flows (2d) is Django-template-only — the flow *builder page itself*
(`templates/flows/edit.html` and friends: wrapper chrome, breadcrumbs,
publish button) is in scope; the React island it mounts is Phase 4, entirely
separate code.

### Phase 3 — public REST API (decision point)

**Recommendation: do not translate `apps/api/errors.py`'s error envelope or
the ~20 `ApiError(...)` call sites in `apps/api/routers/*`.** A machine
client of `/api/v1/` should get a stable, English `message` it can log or
show a developer, and `error.code` is already the field meant for
programmatic branching — the same convention Stripe/GitHub/Twilio use.
Translating `message` is pure downside: it breaks any integration that
string-matches on it today, for a field no human end-user is meant to read
directly. If this changes later (e.g. a hosted-support surface echoing API
errors into a workspace admin's UI), that's a routing/content-negotiation
feature to design separately, not a `gettext_lazy` sweep.

Action if approved: no code change — a one-line note in `docs/api/` stating
`message` is English-only by design, so a future contributor doesn't
"fix" it by half-translating the router.

### Phase 4 — React flow-builder (independent track)

Not a Django task, not blocked on Phase 2, and doesn't block it either — can
be staffed and run in parallel by whoever owns `frontend/builder`.

- Add `react-i18next` + `i18next-browser-languagedetector` (the one new
  dependency in this whole rollout — nothing in-repo covers client-side
  string interpolation with pluralization).
- Sync the active language from Django into the island: `templates/flows/edit.html`
  already passes data via attributes on `#flow-builder`
  (`frontend/builder/src/env.ts`'s `readEnv()`) — add
  `data-locale="{{ LANGUAGE_CODE }}"` there, read it in `i18next`'s init
  instead of re-detecting the browser language independently.
- Extract the ~80+ strings across the 57 non-test files into a JSON
  namespace, replace with `useTranslation()`; add
  `eslint-plugin-i18next` (or similar) afterward as the TS-side equivalent of
  `tests/test_i18n_coverage.py`'s regression guard.

### Content track (not scheduled, listed so it isn't lost)

`flow-templates/*.json` (43 files, seeded default flow copy) and any future
admin-authored flow content are a translation/authoring job, not a code
change — a parallel `*.json` per locale, or accept these ship English-only
indefinitely since a workspace admin edits and owns this copy immediately
after import anyway. `apps/flows/fixtures.py` is test-only (no non-test
import anywhere) — explicitly out of scope, not user-facing.

## Cross-cutting: the regression guard's upgrade point

`tests/test_i18n_coverage.py` currently just asserts `{% load i18n %}`
presence on a fixed allowlist — deliberately weak, because most of
`templates/` was still untouched. Once Phase 2 lands, coverage crosses
roughly half of `templates/` (12 + ~24 + 84 = 120/156), at which point the
noise-floor argument stops holding. Upgrade then:

- Replace the presence check with a diff-based one: `make i18n-extract` in
  CI, fail if it produces a msgid from a file **not** already in the .po
  catalog's `#:` references *and* not on a small, explicit allowlist of
  known-unmigrated files (Phase 4/content-track leftovers). Catches "new
  hardcoded string added to an already-migrated file" — the actual
  regression that matters — without policing files nobody has touched yet.

## Verification (same shape every phase)

1. `make i18n-extract` — new msgids appear, nothing existing disappears
   (`git diff locale/` additive-only for a template-only phase).
2. Fill `ru`/`ky` `msgstr` for the phase's new strings; `msgfmt --check-format`
   on both — catches a mismatched `%(name)s`/`{name}` placeholder before it
   ships.
3. `make i18n-compile`, then a real browser check: log in, set language to
   `ru` (Preferences page), walk the app's screens, confirm no English
   survives outside proper nouns/brand name.
4. `make test` — `tests/test_i18n_coverage.py` (extended with the phase's
   files) plus the app's own existing suite must stay green; `make lint` /
   `make typecheck` (watch for the `gettext_lazy`-vs-`str` mypy gap Phase 0
   hit — reuse `CopyText`/`NavLabel`-style aliases rather than rediscovering
   the fix).
5. Phase 4 specifically: `npm run typecheck:js` and `npm run test:js`
   (Vitest) after wiring `react-i18next`, plus a manual pass with the Django
   shell set to `ru` to confirm the island's language matches the page
   around it.
