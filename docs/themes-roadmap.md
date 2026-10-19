# CSS themes — roadmap

## Context

The goal is a real, swappable theme system: an operator or a user picks a
**theme** (palette, fonts, radii — "BrightBean", "Slate", …) and, independently,
a **mode** (light / dark / system). *System* is not a third palette: it picks
light or dark **of the chosen theme**, following the OS.

The codebase is closer to this than most. The inventory below is the state
**before Phase 0**; what Phase 0 changed is listed under its heading.

| Surface | State |
|---|---|
| Stylesheet | One Tailwind 4 bundle, `theme/static_src/src/styles.css` (~5000 lines) → `theme/static/css/dist/styles.css`, one `<link>` in `templates/base.html` |
| Tokens | Three layers in one `:root` block, now `theme/static_src/src/tokens.css`: brand → semantic → component. The file's own header says "to rebrand, change only the brand tokens" |
| Templates (154) | **0** Tailwind palette utilities (`bg-gray-500` …), **0** `dark:` variants; colour arrives through `var(--…)` — 363 refs in inline `style`, 1062 in the CSS |
| Flow builder (React) | Tokens only (`var(--surface-2)`, `var(--flow-group-*)` …), no hex in `frontend/builder/src` |
| Charts | `templates/analytics/flow_detail.html:105` reads tokens with `getComputedStyle` — theme-aware at load |
| Preference precedent | `User.language` (`apps/accounts/models.py:69`), blank = "no preference", edited on `templates/accounts/preferences.html` |
| CSP | `style-src 'self' 'unsafe-inline'`; `base.html` already emits a nonced `<style>` |

What stood between that and a theme system (the first three items and the
last were closed by Phase 0):

- **Undefined tokens, broken today:** `--danger-500` / `--danger-500-rgb`
  (`templates/flows/import_upload.html:27`, `templates/flows/template_gallery.html:44`)
  and `--border-inner` (three rules in `styles.css`). The browser drops
  the declaration silently.
- **Literal colours outside the token block:** `color: white` / `#fff`
  (six rules in `styles.css`), `--flow-ink: #3730A3` / `#0F766E`. Inside it
  but not
  mode-able: `--surface-0: #FFFFFF`, `--text-inverse`, `--primary-tint-border`,
  `--color-illustration-card`, shadows hard-coding `rgba(23,20,18,…)`, and the
  `--chevron-neutral*` data-URIs with the stroke baked in.
- **RGB triplets:** `rgba(var(--x-rgb), a)` — 24 uses in the CSS, 6 in
  templates. A triplet cannot follow a mode (see Principles).
- **No `color-scheme`:** native controls, scrollbars and date pickers stay light.
- **xyflow defaults:** `apps/flows/static/flows/builder/builder.css` ships
  light `--xy-*` hex values.
- **Dead white-label fields:** `Workspace.primary_color` / `secondary_color`
  (`apps/workspaces/models.py:35-36`) are validated and saved, and never applied.
- **Enforcement by comment only:** "no literal colour outside Layer 1" is a
  convention in `styles.css`, not a test.

## Principles

1. **Two axes, two mechanisms.** Theme is a selector; mode is `color-scheme`
   (set from `data-color-mode`).
   Neither knows how the other was chosen.
2. **Tokens are the only source of colour.** Component CSS, templates and the
   React island reference semantic tokens; only theme files hold values.
3. **One bundle.** Built-in themes are `@import`ed into `styles.css`. No
   per-theme build, no extra request, no CSP change.
4. **Mode needs no JavaScript.** *System* is `color-scheme: light dark`; the
   browser resolves it before first paint. No anti-flash script, no
   duplicated `@media (prefers-color-scheme)` blocks.
5. **Every phase is mergeable on its own** and leaves the product working.
6. **Guards, not conventions.** Each rule that matters gets a test that fails
   with a message naming the fix.

## Architecture

```html
<html data-theme="brightbean" data-color-mode="system">
```

```css
/* tokens.css: the mode axis, one rule per mode */
[data-color-mode="light"]  { color-scheme: light; }
[data-color-mode="dark"]   { color-scheme: dark; }
[data-color-mode="system"] { color-scheme: light dark; }
```

```css
/* theme/static_src/src/themes/brightbean.css */
[data-theme="brightbean"] {
  /* colour: both branches, resolved by the inherited color-scheme */
  --surface-0:    light-dark(#FFFFFF, #1C1917);
  --text-primary: light-dark(#1C1917, #F5F5F4);
  --shadow-color: light-dark(rgb(23 20 18 / 8%), rgb(0 0 0 / 40%));
  /* non-colour: theme-only, no mode */
  --brand-font-display: Georgia, 'Times New Roman', serif;
  --radius-2xl: 1rem;
}
```

| `data-color-mode` | `color-scheme` on `<html>` | Result |
|---|---|---|
| light | `light` | left branch of every `light-dark()` |
| dark | `dark` | right branch |
| system | `light dark` | the OS preference, live |

- `tokens.css` keeps the token **names** and semantic wiring (Layer 2 →
  Layer 1); a theme file only supplies Layer 1 values (and may override a
  Layer 2 mapping). The default theme also matches `:root`, so a page without
  `data-theme` still renders.
- Alpha washes are `color-mix(in srgb, var(--x) a%, transparent)` (done in
  Phase 0). That is what lets a `light-dark()` colour carry alpha.
- Shadows compose from `--shadow-color` (done in Phase 0).
- Select chevrons (`--chevron-neutral*`) stay data-URIs in `tokens.css` for
  now: `mask-image` cannot be used on a `<select>` (it masks the whole
  control), and `light-dark()` only switches colours, not `url()`s. Phase 2
  solves them, most likely with a wrapper pseudo-element masked by the SVG
  and painted `currentColor`.
- Python side: `apps/common/themes.py` holds the registry (slug, label,
  palettes) and `resolve()`, the single source for the preferences
  view, the template tag, the system check and the tests. `{% theme_attrs %}`
  (`apps/common/templatetags/common_extras.py`) prints both attributes on
  `<html>`. It is a tag, not a context processor, because the 500 page is
  rendered with a bare `Context()` where no processor runs; with no signed-in
  user it prints `settings.THEME_DEFAULT` / `COLOR_MODE_DEFAULT`. It prints
  data attributes only, never `style`: `base.html`'s `{% block html_style %}`
  lets a page add its own attributes to `<html>`, and a second `style` there
  would be silently dropped.
- A theme declares **palettes** (`light`, `dark`), not modes. *System* is
  derived: it exists exactly when both palettes do. So a theme offers one mode
  or all three, and a mode select on screen always contains the stored value.
- `resolve()` never raises, since it also runs on the error pages. A bad
  default is reported by the `common.E007` system check; a malformed theme
  cannot exist (`Theme.__post_init__` rejects it at import), and the registry
  is keyed by each theme's own slug.

Browser floor: `light-dark()` and `color-mix()` are Baseline 2024 (Chrome 123,
Safari 17.5, Firefox 120). A custom property accepts any value, so an older
browser keeps `light-dark(…)` and the *using* property becomes invalid at
computed-value time (transparent text, no background). A plain `:root`
fallback would not help, because the theme rule overrides it. **Decided
2026-09-24: the floor is Baseline 2024**, used directly — no `@supports`
fallback. An older browser gets invalid colour on themed elements rather than
a second value set to maintain.

## Roadmap

```
Phase 0 hygiene ──► Phase 1 mechanism ──► Phase 2 dark ──► Phase 3 second theme
                                                   └──► Phase 4 selection levels ─► Phase 5 third-party themes

Phase 6 Twenty compatibility study (independent; research only)
```

### Phase 0 — hygiene ✅ done

- `--danger-*` → `--error-*`, `--border-inner` → `--divider`. These two were
  the only *visible* changes: the declarations had been silently dropped.
- Literal colours → tokens: `white`/`#fff` → `--text-on-fill` (text on a
  saturated fill, white in every mode; `--text-inverse` is kept for inverted
  neutral surfaces, which flip in dark mode); `--flow-ink` →
  `--flow-group-*-ink`; shadows → `--shadow-color`.
- All 30 `rgba(var(--*-rgb))` uses → `color-mix`; the 12 triplet tokens are
  gone; `--scrim` replaces `--scrim-rgb`.
- `theme/static_src/src/tokens.css` holds every value; `styles.css` imports it.
- **Guard:** `tests/test_theme_tokens.py` — no colour literal (hex, `%23`
  hex in data-URIs, colour functions, all 148 named colours in any case) in
  `styles.css`, template `style` attributes and `<style>`/`<script>` blocks
  (email exempt) or builder source; every `var(--x)` resolves to a definition.
  The chart's hex fallbacks in `analytics/flow_detail.html` are gone.
- Deferred to Phase 2: select chevrons (see Architecture).

Note: Lightning CSS (inside the Tailwind build) emits an opaque, pre-`color-mix`
fallback before each `color-mix` rule. Tailwind 4's own browser floor (Safari
16.4, Chrome 111, Firefox 128) always has `color-mix`, so the fallback never
applies in a supported browser.

### Phase 1 — the mechanism ✅ done (still one theme, light only)

- `apps/common/themes.py` registry + `resolve()`; `settings.THEME_DEFAULT`,
  `settings.COLOR_MODE_DEFAULT`, checked at startup (`common.E007`: an
  unknown theme or mode, or a mode the default theme has no tokens for).
  A malformed palette list fails at import.
- `User.theme`, `User.color_mode` (blank = default, no `choices=`), migration
  `accounts.0004`. A stored mode the theme cannot render is kept and clamped
  at render time, so *system* switches on by itself once dark exists.
- `{% theme_attrs %}` on both `<html>` roots: `base.html` and
  `layouts/error.html` (see Architecture for why a tag).
- Preferences: theme and mode selects are drawn only when they offer a real
  choice, so in Phase 1 the page is unchanged. The view writes a field only
  when its key was posted; otherwise a language save would reset the theme.
  The mode select lists the *currently saved* theme's modes, so switching to a
  theme with a dark palette and picking dark takes two saves; Phase 3 fixes
  that (all modes, clamped server-side, or an Alpine-dependent select).
- Guards: `tests/test_theme_tokens.py` requires `{% theme_attrs %}` on every
  `<html>` root; `test_shell.py::TestTheThemeReachesEveryRoot` checks the
  rendered attributes on shell, login, 404 and bare-context error pages;
  `test_themes.py` requires one `[data-color-mode]` rule per registered mode.

### Phase 2 — dark mode for BrightBean

Split into five PRs, each its own branch off a fresh `main`
(`claude/themes-phase-2a` … `2e`). The registry stays `("light",)` through
2a–2d, so users see nothing change before 2e.

**2a — contrast guard ✅ done**
- `tests/test_theme_contrast.py`: WCAG 2.x AA over an explicit `PAIRS` list
  of `--text-*` on `--surface-*` tokens — exactly what "Done when" below
  names. This is the contract 2c's palette solver reads too; keep it in one
  place, not duplicated between guard and script.
- Resolves `var()` chains from `tokens.css`, including `light-dark(a, b)`
  once 2c introduces it — split branches with a paren-balanced parser, not
  a naive `split(",")` (a branch value like
  `color-mix(in srgb, var(--x) 6%, transparent)` has its own commas).
- Confirms the browser floor in Architecture as decided, not conditional
  (see above).
- **Found, left out on purpose:** `--text-on-fill` on `--primary` is
  2.80:1 and on `--error-500` is 3.76:1; `--border-strong` (the form-control
  hairline) on `--surface-0` is 1.26:1; `--text-tertiary` on `--surface-page`
  is 4.44:1 and on `--surface-2` is 4.40:1 — all below their WCAG threshold
  already, on the *current, shipping light palette*. Pre-existing, unrelated
  to dark mode, not in `PAIRS`; needs its own design pass if it's worth
  fixing (would mean darkening `--brand-500`/`--error-500`, the form-control
  border, or `--text-tertiary` itself — a branding call, not a
  guard-writing one).

**2b — plumbing, light only ✅ done**
- Chart: `flow_detail.html` now resolves each token through a hidden probe
  element (`probe.style.color = 'var(--x)'` → `getComputedStyle(probe)
  .color`) instead of `getPropertyValue` on `:root` (which returns a custom
  property's own *unresolved* text — harmless today, would hand Chart.js
  the literal string `"light-dark(...)"` the moment 2c lands), and
  re-renders on `matchMedia('(prefers-color-scheme: dark)').change`.
- Select chevrons: `--chevron-neutral`/`--chevron-neutral-sm` are unchanged
  (kept, not deleted — the baked-in stroke colour stopped mattering the
  moment they became `mask-image` sources instead of `background-image`,
  since a mask only reads a source's alpha; re-encoding them for no reason
  would just be churn) but now consumed as `mask-image` on a wrapper's
  `::after`, painted with `currentColor` via `color: var(--text-tertiary)`
  — automatically dark-mode-correct once that token is, no separate dark
  stroke to pick. `<select>` doesn't carry pseudo-elements at a Baseline
  2024 floor, so this touched markup: **24 native `<select>` elements
  across 17 templates**, each now wrapped in `<span class="bb-select-wrap">`
  or `<span class="bb-filter-select-wrap">` (`--full` modifier, or an
  inline width moved onto the wrapper, for the ones that need to size
  themselves rather than shrink to content). Verified live (Playwright
  against a real dev server, not just guard tests) across all three wrapper
  shapes actually exercised — full-width, explicit pixel width, and the
  compact filter-select — no layout regression, chevron correctly
  positioned in every case.
- `#flow-builder` → `--xy-*`: **already mapped** (`styles.css:2336-2373`,
  every value already a Layer-2 token) — nothing to change there.
  `TestFlowBuilderXyMapping` (`tests/test_theme_tokens.py`) now asserts the
  mapping block still exists and every value stays `var()`, so a future
  edit can't silently drop it.

**2c — dark palette.**
- `scripts/derive_dark_palette.py`: no stdlib OKLCH conversion exists
  (`colorsys` only covers HSV/HLS/YIQ) — implement the sRGB↔linear↔XYZ↔OKLab
  matrices directly (Björn Ottosson's published constants), no dependency.
  - Neutrals invert lightness (`L' = 1 − L`, with a small lift so the
    darkest surface isn't pure black), keeping hue and chroma.
  - Accents and status colours keep their hue; lightness is solved against
    the same (foreground, background, role) pairs 2a's guard checks — not
    an independent notion of "meets AA contrast".
- Commit the generated `light-dark()` values to a new
  `theme/static_src/src/themes/brightbean.css` (per Architecture: `tokens.css`
  stays names/wiring only; a theme file holds the values). `--platform-*`
  brand literals stay as they are.
- The contrast guard (2a) now has real dark branches to check.

**2d — visual pass, app by app**, like the i18n rollout: shell/auth/errors,
inbox, contacts, flows+builder, broadcasts/campaigns, analytics, settings.
Flip the registry locally only, put light/dark screenshots in each PR.

**2e — switch on.**
- Add `("light", "dark")` to the registry; the mode select then appears by
  itself (`.modes` already derives *system* once both palettes exist — no
  registry code changes needed beyond the tuple).
- Rename the "Language" nav row to "Preferences".
- Check the raster logos on a dark background; add a dark variant only if
  needed.
- Update ru/ky translations and mark Phase 2 ✅ here.

Done when: every shell page is legible in dark at WCAG AA for text tokens,
enforced by 2a/2c's guard rather than manual review alone.
Size: 2–4 days, mostly 2d review.

### Phase 3 — a second theme

- One more `themes/<slug>.css` (e.g. a cool neutral "Slate").
- **Contract test:** a theme overrides only existing token names; every
  colour token has two `light-dark` branches if the theme declares dark;
  registry and file set match.

Done when: switching theme changes palette and fonts with no template edits.
This phase is the proof the system is swappable. Size: ~1 day.

### Phase 4 — who picks the theme (decision point)

Cascade: instance default → workspace → user. Open questions: may a user
override a workspace brand theme; is mode ever enforced above the user.
Revives `Workspace.primary_color`: a nonced `<style>` sets
`--brand-500`, and the ramp (50…900) is derived with
`color-mix(in oklch, var(--brand-500) n%, white|black)`. Contrast of the
derived `--primary` against `--text-on-fill` must be checked at save time.
Size: ~1–2 days after the decision.

### Phase 5 — third-party themes (decision point)

A theme outside the repo: a static CSS file loaded after the bundle,
registered through `settings.THEMES`, validated by the Phase 3 contract test
at startup (system check). Plus `docs/themes.md` — "how to author a theme".
Only worth building if self-hosters ask for it.

### Phase 6 — visual compatibility with Twenty CRM (study)

BrightBean Chat and Twenty CRM ship together as customer-facing frontends
(Zirek). The question for this phase: can they look like parts of one brand?
Research only: no code here, and no work on a Twenty theme, which would be a
separate project of a different scale.

- Compare the two design systems axis by axis: token architecture, neutrals,
  accent, status colours, fonts, radii, spacing scale and density, shadows,
  icons, the shape of key components (button, input, badge, table, modal),
  and light/dark.
- For each axis, record one outcome: already matches / can be aligned by
  tokens on our side / diverges.
- Deliverable: the compatibility table and a short verdict (yes / partly /
  no) naming exactly what stands in the way.

Size: ~1 day.

### Out of scope

- **Email templates** (`templates/notifications/email/*`, `templates/members/email/*`):
  mail clients don't support custom properties; email stays light with inline
  hex.
- **Django admin:** has its own dark mode.

## Cross-cutting: the regression guard

The Phase 0 guard is the backbone. Every later phase tightens it rather than
adding a new one: Phase 2 adds the contrast check, Phase 3 the theme contract,
Phase 5 runs the contract as a Django system check.

## Verification (every phase)

1. `npm run build:css` succeeds; the dist contains the expected
   `[data-theme=…]` rules.
2. `pytest tests/test_theme_tokens.py apps/common apps/accounts`.
3. `make server`: switch theme and mode in Preferences; `<html>` attributes
   change; *system* follows the OS toggle without reload. (From Phase 2: in
   Phase 1 the selects are hidden, so only the attributes can be checked.)
4. Open the flow builder and analytics charts in every theme × mode.
5. Build the container image and repeat 3 against it.
