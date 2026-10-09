# BrightBean Chat UI/UX redesign: engineering handoff

Prototype: [`brightbean-redesign.html`](brightbean-redesign.html) (screen switcher in the top bar).
Source tokens and templates referenced: `apps/theme/static_src/src/styles.css`, `templates/**`, `apps/flows/frontend/src/**`.

## 1. Principles applied everywhere

1. **Orange is the signal for one thing: the primary action.** At most one orange control per screen/step (New flow, Send, Continue/Go live, Publish, Activate sequence, Add contact). Everything else is neutral ink, green (live/success), or grey.
2. **State is shown with shape and colour, not only text.** Live = green dot/pill/tile/switch. Offline = amber. Draft/archived = grey. Sending = blue. Failed/errors = red.
3. **One filter pattern.** A single Filter button (funnel icon, count badge) opens a popover with grouped sections; active filters appear as removable chips; "Clear all" in the popover.
4. **Create flows use a short modal (name and one choice), then a full page** with a step rail, one task per step, and one primary button at the bottom.
5. **Plain-language copy, no codes or ids** (continues the existing principle 5 in the repo).
6. **Persistent feedback for system state**: save state, problem count, window status are visible in the chrome, not behind clicks.

## 2. Global changes (apply app-wide, including pages not redesigned)

### 2.1 Colour roles (use existing tokens, change which role uses them)

| Role | Today | New |
|---|---|---|
| Primary CTA (`.btn-pill-primary`) | `--primary` (brand-500) | Keep. Optionally use `--brand-600` fill for white text contrast (3.6:1 vs 2.8:1). |
| Active nav row, active tab, selected row, selected card/radio, active filter chip, focus-selected state | brand tint / orange | `--neutral-900` ink (nav: `--neutral-150` fill + ink text; tabs: ink 2px underline; chips/selected: ink fill or 2px ink border) |
| Links, hover on titles (`.btn-text-hover-primary`) | orange | `--text-primary`, underline on hover |
| Badges, unread dot, counters (sidebar badge, view counts) | orange | ink (`--neutral-900`) or `--text-tertiary` |
| Status Live / Active / Sent / Subscribed | mixed | `--success-50` bg, `--success-700` text, `--brand-green-500` dot |
| Offline / Paused | mixed | `--warning-50`, `--warning-700`, `--warning-500` dot |
| Sending / info | mixed | `--info-50`, `--info-700`, `--info-500` |
| Failed / Opted out | red | `--error-50`, `--error-700`, `--error-500` |
| Draft / Archived / neutral | grey | `--surface-2`, `--text-secondary`, `--neutral-400` |
| Input focus ring | varies | `border-color: --primary; box-shadow: 0 0 0 3px --primary-muted` (the only orange outside CTAs) |

Audit task: grep for `--primary`, `--brand-*`, `text-primary`, `bg-primary`, `.chip-filter.active`, `.sidebar-nav-item.active`, `.subnav-item.active`, `.bc-step-current`, `.list-row-icon-live`, `.status-pill-*`, `.ib-dot`, `.nav-badge` and re-point everything that is not a primary button or focus ring.

### 2.2 Components to add or change in `styles.css` / `templates/components`

- **`.filter-popover` and `components/filter_popover.html`** (new). Replaces `.chip-filter` rows and the multiple `ui_select` toolbars on: flows, sequences, templates, broadcasts, contacts (`_filter_bar.html`), inbox, media library, members, API keys/webhooks lists, analytics range selectors where more than 2 controls exist. Spec:
  - Trigger: 36px, radius `--radius-control`, 1px `--border-strong`, funnel icon (the supplied SVG), label "Filter", count badge (ink fill, white text) when >0; border becomes ink when active.
  - Panel: 340px (full rail width inside narrow panes), radius `--radius-tile`, `--shadow-lg`, padding 16. Sections have an 11px uppercase `--text-tertiary` title, options are toggle pills (28px; inactive white + border, active ink fill). Footer button "Show N results". Closes on outside click and Escape. Use `position: fixed` anchoring as `ui_select.html` does to avoid clipping in scrolling containers.
  - Active chips row below the toolbar, each removable, plus "Clear".
  - Keep the search input outside the popover. Keep at most one inline segmented control per page for true views (Inbox: All / Unassigned / Mine / Done) as underline tabs, not chips.
  - Backend: unchanged; hidden inputs and `filters-changed` event keep working. Multi-select per group is new, so the filter params become repeatable (`status=live&status=draft`) and selectors/views must accept lists.
- **`.tabs` (underline tabs)**: replaces `.subnav-item` and filter chips used as views. 44px height, 14px, active = 700 weight, ink text, 2px ink underline.
- **`.status-pill`**: unify as pill with dot, colours per table above; use on every list (flows, sequences, broadcasts, contacts, channels, API keys, webhooks, imports).
- **`.switch`** (new): 40x22 toggle, green when on, grey when off, disabled 40% opacity. Used for flow live/offline, triggers, sequence status.
- **`.stepper-rail`** (generalise `.bc-rail`): vertical (editor pages) and horizontal (broadcast wizard) variants. Numbered circle (done = green tick, current = ink, upcoming = grey), 2px connector (done = green).
- **`.option-card`** (radio card): 1px border, selected = 2px ink border and ink radio. Replaces plain radios in wizards and modals.
- **Summary cards** (flows page): `number + label` on one row, with status dot.
- **Empty states**: icon tile, one-line title, one-line explanation, one secondary action.
- **Toast**: keep `_toast_host`; success tone uses the green role, no orange.
- Page header: keep Georgia 28px title (`--fs-display`), 14px lede, and one primary action right-aligned; allow wrapping at narrow widths.

### 2.3 Layout rules

- Page content max-width 1060px for list pages; full-bleed for inbox, builders and editors.
- List rows: icon tile, title with status pill, one meta line, right-aligned metric, switch (if toggleable), secondary "Open/Continue" button, "…" menu. Allow wrap below ~900px.
- Table-style lists (sequences, broadcasts, contacts): sticky header row of 11px uppercase labels, horizontal scroll under 780px instead of clipping.
- Editors and wizards: header bar (back link, inline-editable name, status pill, save state), left or top step rail, content max 620–640px, right summary or settings panel (300px), primary button bottom-right.
- Sidebar: keep 240px/60px collapse. Active row uses the ink treatment above. Do not show badges in the collapsed 60px state except as a corner dot.

### 2.4 Copy and interaction conventions

- Buttons: verbs ("Create flow", "Publish", "Send broadcast"). Final step button states the consequence ("Schedule broadcast" vs "Send broadcast").
- Destructive actions: red text link, separated by a rule, confirm dialog if irreversible.
- Empty-search state always offers "Clear filters".
- Keep keyboard shortcuts (j/k/e/r) and show them as a hint on the inbox page.

## 3. Screen-by-screen changes

**Inbox** (`inbox/list.html`, `_conversation_rows.html`, `_thread*.html`)
- View buttons become underline tabs with counts. Channel, label and assignee move into the Filter popover. Search sits on the left, filter button beside it.
- Rows: avatar with real channel glyph badge, bold name and preview when unread, ink unread dot (was orange), current row = grey fill with 1px ring.
- No conversation selected: centred empty state with unread count, shortcut hint, "Open first unread".
- Thread: header with avatar, channel, assignee button and Mark done; a status bar for the 24-hour window with "Pause automation (take over)"; bubble styles differ for contact (white), flow (grey, labelled with the flow name) and agent (dark); composer has Reply / Internal note tabs; Send is the only orange control.
- Right panel: contact details, tags, last automation that ran.

**Flows / Sequences / Templates** (`flows/list.html`, `_list_rows.html`, `campaigns/list.html`, `flows/template_gallery.html`)
- One tab strip across the three, same header shape on all.
- Flows: summary cards (Live/Offline/Draft counts), rows grouped into Live, Offline, Draft sections, each row has a switch (disabled for drafts, button reads "Continue"), green tile when live, runs in the last 7 days, trigger sentence. Search + Filter (status, channel, folder).
- Sequences: step dots, active contacts, completion bar, status pill.
- Templates: 3-column cards with channel glyph, trigger type tag, message count, "Use template". Filter: channel, trigger.

**Create flow** (modal on `flows/list.html`, then `flows/edit.html` host)
- Modal: name plus "Start from" option cards (Blank / Template / Import). Import leaves the page header.
- Setup page with step rail: Trigger, Reply, Follow-up, Review. One task per step, summary sentence on Review, "Continue" then "Go live" as the orange button. "Open visual builder" is a secondary action for power users.

**Create sequence**
- Modal for the name. Page is a vertical timeline (entry, wait chip, step card, add step) with the selected step expanded inline, and a settings panel (channel, sending window, exit conditions, total length). "Activate sequence" is the orange button.

**Flow builder** (`flows/edit.html`, `apps/flows/frontend/src`)
- One 56px top bar replaces the page header + `.fb-toolbar`: back link, inline-editable name, status pill (Draft / Live vN), save state, undo/redo, stats toggle (icon), Test, problems button, Publish (orange; becomes secondary "Set offline" when live). Export moves to an overflow menu.
- Problems: the always-visible `ProblemsRail` becomes a popover from the problems button ("N issues" or "Ready"); each item jumps to the step. A failed publish opens it.
- Left outline (224px): "When it runs" with a switch per trigger plus "Manage triggers", then the numbered step list with a dot for problems. Replaces `TriggerSidebar` + right-hand `StepList`.
- Canvas: dotted grid; cards use the group accent from `--flow-group-*` on the icon tile only (content=sky, logic=indigo, actions=teal), plain-language eyebrow from `schema/plain.ts`, "Starts here" tag, error border for invalid steps, one output handle per button, "+" on each edge to insert a step. Zoom control (+, -, percent) top-left, minimap bottom-right, "Add a step" button bottom-left (menu grouped by `GROUP_PHRASE`).
- Right inspector (312px) shows only the selected step with Settings / Preview tabs; step problems at the top, Delete below a rule. Preview shows how the step looks in the chat (previously stacked above the editor).
- Selection uses a 2px ink outline, not orange.

**Broadcasts** (`broadcasts/list.html`, `_rows.html`, `_wizard.html`, `_step_*.html`)
- List: table with status pill, delivery bar (green sent, blue sending, red failed), audience, when. Filter popover for status and channel.
- New broadcast modal keeps name + channel. Wizard becomes a full page: horizontal stepper (Channel, Audience, Message, Schedule, Review), option cards for channel/audience/schedule, message editor with live preview, right summary panel with compliance checks (opted-out excluded, channel allows broadcasts, audience, message, send time). Final button label reflects Send vs Schedule.

**Contacts** (`contacts/list.html`, `_filter_bar.html`)
- Filter bar collapses into the same popover (channel, tag, status) with chips; table with channel glyph, tag, last active, status pill.

## 4. Pages not redesigned: how to bring them in line

Apply sections 2.1 to 2.4 mechanically. Per page type:

| Page type | What to do |
|---|---|
| Any list with 2+ filter controls (media library, members, API keys, webhooks, imports, analytics, notifications history, channels) | Replace with search + Filter popover + chips. |
| Settings and org pages (`layouts/settings.html`, billing, workspaces, members) | Recolour active/selected to ink, use `.status-pill`, one orange primary per page, page header shape as above. |
| Channels list/detail and connect pages | Status pills for health (Healthy green, Needs reauth amber, Error red), real platform glyphs, one orange "Connect" per page. Connect flows adopt the stepper rail and option cards. |
| Analytics | Range control and filters in one popover; counter cards use number + label rows; no orange series colours except the primary metric. |
| Flow import wizard (`import_upload`, `import_review`) | Use the stepper rail and summary panel pattern. |
| Trigger drawer and forms | Option cards for trigger type, switches for enabled, focus ring only orange. |
| Auth pages | Keep layout, ensure the single orange button is the submit. |

## 5. Suggested rollout order

1. Tokens and roles (2.1) plus `.status-pill`, `.tabs`, `.switch` (small CSS change, big consistency win).
2. `filter_popover` component and migrate list pages one at a time (backend: accept multi-value params).
3. Stepper rail and option cards, then rebuild flow, sequence and broadcast creation flows.
4. Inbox restyle.
5. Flow builder: top bar, outline, popover problems, then canvas and inspector restyle.
6. Sweep of remaining pages using the table in section 4; add a lint/test that fails on `--primary` usage outside an allowlist (primary buttons, focus ring).

## 6. Known gaps in the prototype

- Channel glyphs and nav icons are copied from the repo; the logo is a workspace initial placeholder.
- Collapsed sidebar, channel list in the sidebar and the account menu are not drawn.
- Flow builder step types are limited to message, tag, wait; undo/redo and drag-to-connect are not implemented. Card styling is approximate and not matched against `.fb-*` rules in `styles.css`.
- Dark mode and mobile layouts were not designed.
- White text on orange is below 4.5:1; decide between `--brand-600` or dark text on the CTA.
