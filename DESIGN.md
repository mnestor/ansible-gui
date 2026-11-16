# Design System: ansible-gui (assumed, confirm before shipping)

Character: a server-room clipboard — dense, cool and exact; it tells you where every value came from.

World: inventory files, rack labels, terminal logs. The signature element comes from this world: every variable carries its **source stack**, the file that won and the layers it overrode, shown inline rather than hidden.

## Typography
- Families: **Red Hat Text** (UI and prose), **Red Hat Display** (the one identity heading per screen), **Red Hat Mono** (variable names, values, file paths, hostnames: things people copy or compare character by character). Ansible's own house faces, so the tool reads as part of that world rather than a generic dashboard. Loaded by a Google Fonts `<link>` with `preconnect` and `display=swap` in `base.html`; fallbacks `system-ui, sans-serif` and `ui-monospace, monospace` so an offline homelab still renders.
- Scale (px): 12 / 13 / 15 / 18 / 32, plus 28 for the identity heading under 640px so a long host name fits a phone. Body and tables are 13 (dense expert tool, deliberately low contrast inside panes); the identity heading (host or group name) is 32 in Display, about 2.5× body, so the subject of the screen is found at once.
- Weights: 400 body, 600 labels and identity column, 700 the identity heading only.
- Figures (counts, durations, times) use the text face with `tabular-nums`, not mono.

## Color
Neutrals are cool slate (a server room, not paper). Tokens live in `src/ansible_gui/static/app.css` under `:root` and `[data-theme=dark]` / `prefers-color-scheme: dark`.

| Token | Light | Dark | Why |
|---|---|---|---|
| `--canvas` | `#f7f9fa` | `#0f171e` | Main pane, where reading happens |
| `--plane` | `#e8edf1` | `#16212b` | Sidebar and header planes; separation is by plane, not by border |
| `--well` | `#ffffff` | `#1b2833` | Inputs and code wells |
| `--ink` | `#14212b` | `#e3e9ee` | Text; 15.5:1 / 14.8:1 on canvas |
| `--ink-muted` | `#4f5d6a` | `#9aa8b5` | Meta text; 6.4:1 / 7.4:1 on canvas, 5.7:1 / 6.7:1 on plane |
| `--rule` | `#d5dce3` | `#2a3845` | Table hairlines only |
| `--accent` | `#0b6e79` | `#4cc2cd` | Teal signature (hue ≈186°, 182° from danger). Used only for the primary action and the winning source of a variable. 5.7:1 / 8.5:1 on canvas |
| `--accent-fg` | `#ffffff` | `#062a2f` | Label on accent; 6.0:1 / 7.2:1 |
| `--danger` | `#b42318` | `#ff7b72` | Failed runs, destructive actions, resolve errors |
| `--ok` | `#126b2e` | `#4ac26b` | Ansible "ok" |
| `--changed` | `#7a5300` | `#d6a02a` | Ansible "changed", and attribution warnings |
| `--unreachable` | `#9c2367` | `#f075b5` | Ansible "unreachable" |

## Layout
- Widths: `--rail` 180px (nav rail), `--rail-icon` 56px (icon rail), `--sidebar` 288px (inventory host list), `--icon` 20px.
- Bands: phone under 640 (top bar and menu), tablet 640–899 (icon rail on every page), desktop 900 and up (full rail; icon rail on Inventory so the variables keep their width), wide 1200 and up (the Overview's side column).

## Radii
- `--radius-control` 6px: buttons, inputs, badges.
- 0 on panes and tables: planes meet edge to edge.

## Spacing
- Base 4px. Scale `--s1` 4 / `--s2` 8 / `--s3` 12 / `--s4` 16 / `--s6` 24 / `--s10` 40.
- Rhythm: tight inside a group (4–8: table cells, a label and its value), generous between groups (24–40: between the identity header and the variables, between panes).

## Separation
- Flat colour planes: sidebar and header on `--plane`, content on `--canvas`. Hairline `--rule` only between table rows. No shadows, no boxed cards.

## States
- Hover: background `color-mix(in oklab, var(--ink) 6%, transparent)` on rows and quiet buttons; primary button `color-mix(in oklab, var(--accent) 88%, black)`.
- Focus: 2px outline `var(--accent)`, offset 2px, on every focusable element (`:focus-visible`). Exception: rail items take an inset ring (offset -2px), since an outside ring is clipped at the window edge.
- Disabled: 45% opacity, `cursor: not-allowed`.
- Selected (sidebar or rail item): `--well` background + 3px left bar in `--accent`, with `aria-current="page"`.

## Status and badges
Text in the status colour on a tint of 12% status over canvas (`color-mix(in oklab, var(--status) 12%, var(--canvas))`).

| Status | Light ratio | Dark ratio |
|---|---|---|
| ok | 5.3:1 | 6.5:1 |
| changed / warning | 5.4:1 | 6.4:1 |
| failed / danger | 5.1:1 | 6.0:1 |
| unreachable | 5.7:1 | 5.8:1 |
| skipped / neutral (`--ink-muted`) | 5.4:1 | 5.9:1 |
| accent (winning source) | 4.8:1 | 6.9:1 |

Accent badges are only for the winning source. "Running", "default" and the active-run count use neutral. A dry run that would change hosts uses the changed tone ("Dry run: would change"), never ok.

## Motion
- 120ms ease-out for hover and disclosure; nothing else animates.

## Components
- Button: primary (accent fill, accent-fg text, one per screen), secondary (transparent, 1px `--ink-muted` outline), quiet (text only). Destructive is danger text on danger tint, never next to primary.
- Source chip: mono file path; the winning layer in `--accent`, overridden layers in `--ink-muted` struck through.
- Badge: status text on status tint, 12px, 600. `badge-lg` (15px) only for the run status on a run page.
- Danger-quiet button: danger text, no fill; destructive controls on a screen (Cancel run). Needs a second click within 4s to confirm.
- Segmented radio (`segmented-inline`): 2–5 mutually exclusive options (Dry run / Apply, verbosity), selected option on `--well`.
- Preview panel: `--plane` block that holds the live result of a form (matched hosts) and the form's one primary button, whose label states the count and mode ("Dry run on 3 hosts").
- Status dot: 8px, the host's last run result in the sidebar.
- Page banner: `banner-ok` (ok tint, ink text) for a completed save, `banner-warn` (changed text on changed tint, 5.4:1 / 6.4:1) for a standing risk. 6px radius inside a page.
- Kind switch: segmented links (`aria-current`) choosing what a form creates, same look as the segmented radio.
- Secret field: write-only. Never pre-filled; on an edit, empty means "keep". Password inputs have a Show/Hide quiet button (`aria-pressed`).
- Issue card: `--plane` block with a 3px left bar in the status colour (danger, unreachable, changed), a badge, a one-line count, host chips, and at most two actions (secondary, then quiet). Used only on the Overview's "Needs attention".
- Side block: `--plane` panel, 6px radius, for the Overview's side column (running, next 24 hours, counts).
- Lost-contact banner: `banner-warn`, `role="status"`, shown when a live poll fails and hidden on the next success.
- Invalid field: 2px `--danger` outline, message below in `--danger` 600, linked with `aria-describedby`.

- Nav rail: `--plane` column, sticky, full window height. Brand, the six sections (20px inline SVG icon, stroke `currentColor`, plus label), then the signed-in name and Sign out at the bottom. Items 36px (44px on touch), `--ink-muted` text, hover tint, current as Selected. The Runs count is a neutral badge, read as hidden text ("2 running").
- Icon rail: the same rail at 56px. Labels are visually hidden; a tooltip (`--well`, 13px 600, 6px radius) shows on hover and keyboard focus. The Runs count overlays the icon as a 16px block, `--plane` text on `--ink-muted`. Brand becomes an "a" monogram.
- Phone menu: a sticky `--plane` top bar with the brand and a `<details>` Menu button (secondary). A neutral 8px dot on it when runs are active. The sheet overlays the page, 44px items at 15px; Escape and a tap outside close it.
- Skip link: first focusable element, hidden until focused, `--well` background.
- Inventory status strip: above the two panes, `--ink-muted` text: sources (mono), host count, load time, and Reload inventory (secondary) at the end. Hidden when the inventory didn't load; the error panel offers Try again.

## Voice
- Plain and specific, sentence case, verbs on buttons ("Reload inventory", "Open in git"). Errors say what failed, the command, and what to do next.
