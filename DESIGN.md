# Design System: ansible-gui (assumed, confirm before shipping)

Character: a violet control room, after BorgWarehouse. A dark nav rail and soft white cards on a lavender-tinted canvas. Calm and friendly, but it still tells you exactly where every value came from.

World: inventory files, rack labels, terminal logs. The signature element comes from this world: every variable carries its **source stack**, the file that won and the layers it overrode, shown inline rather than hidden. The run log is drawn as a terminal (dark in both themes).

## Typography
- Families: **Inter** (UI, prose and headings; headings 700 with -0.02em tracking) and **Red Hat Mono** (variable names, values, file paths, hostnames: things people copy or compare character by character). Loaded by a Google Fonts `<link>` with `preconnect` and `display=swap` in `base.html`; fallbacks `system-ui, sans-serif` and `ui-monospace, monospace` so an offline homelab still renders.
- Scale (px): 11 / 12 / 14 / 15 / 18 / 34, plus 28 for the identity heading under 640px. Body and tables are 14. The identity heading is 34, about 2.4× body, with tight tracking.
- Micro labels: 11px, 600, uppercase, 0.06–0.08em tracking. Used for table headers, `dt` labels (facts, run meta, counts) and the eyebrow (eyebrow in `--accent-ink`).
- Weights: 400 body, 500 nav items and run-meta values, 600 labels, 700 headings and big figures.
- Figures (counts, durations, times) use the text face with `tabular-nums`, not mono.

## Color
Neutrals are lavender-tinted. Tokens live in `src/ansible_gui/static/app.css` under `:root` and `[data-theme=dark]` / `prefers-color-scheme: dark`.

| Token | Light | Dark | Why |
|---|---|---|---|
| `--canvas` | `#f7f7fb` | `#0d0f15` | Page background |
| `--well` | `#ffffff` | `#15181f` | Cards, inputs, the inventory list column |
| `--plane` | `#efedf8` | `#1d2130` | Recessed fills: segmented tracks, chips, stack counts |
| `--code` | `#f6f5fb` | `#10131a` | Code wells inside cards |
| `--ink` | `#1c1b29` | `#e6e8ef` | Text |
| `--ink-muted` | `#5d6478` | `#9aa1b3` | Meta text; 5.5:1 / 7.4:1 on canvas |
| `--rule` / `--rule-strong` | `#e6e4f0` / `#d6d3e6` | `#272c38` / `#333a49` | Card borders and table hairlines / control borders |
| `--accent` | `#6d4aff` | `#8b6bff` | Violet signature, as a fill: the primary button, the selected-item bar, focus rings. 5.2:1 under its label in both themes |
| `--accent-ink` | `#5a37f0` | `#a28cff` | The accent as text (winning source, eyebrow, current item). 6.5:1 on white / 7.1:1 on canvas, and above 5:1 on its own tint |
| `--accent-2` | `#8b6bff` | `#a28cff` | Top stop of the accent gradient (rails, brand tile) |
| `--accent-fg` | `#ffffff` | `#0d0f15` | Label on accent |
| `--danger` | `#b91c1c` | `#ff8a8a` | Failed runs, destructive actions, resolve errors |
| `--ok` | `#0b7340` | `#2ee48a` | Ansible "ok" |
| `--changed` | `#8a5a00` | `#fbbf24` | Ansible "changed", and attribution warnings |
| `--unreachable` | `#a3206e` | `#f075b5` | Ansible "unreachable" |
| `--nav-*` | same in both themes | `--nav-bg` `#08090d` in dark | The rail and phone bar are always dark (`#12111c`, ink `#a9a6c4` at 8:1, current item white on a violet mix) |

Shadows: `--shadow-card` (1px contact plus a soft 16px drop), `--shadow-pop` (tooltips, menus), `--shadow-btn` (primary button). They are heavier in dark mode.

## Layout
- Widths: `--rail` 200px (nav rail), `--rail-icon` 64px (icon rail), `--sidebar` 288px (inventory host list), `--icon` 20px.
- Bands: phone under 640 (top bar and menu), tablet 640–899 (icon rail on every page), desktop 900 and up (full rail, or the icon rail on every page if collapsed), wide 1200 and up (the Overview's side column). The rail is the same width on every page: it never changes between sections.

## Radii
- `--radius-control` 8px: buttons, inputs, segmented tracks, code wells, banners.
- `--radius-card` 16px: cards and the run log.
- `--radius-pill` 999px: badges, chips, stack counts.

## Spacing
- Base 4px. Scale `--s1` 4 / `--s2` 8 / `--s3` 12 / `--s4` 16 / `--s5` 20 / `--s6` 24 / `--s10` 40. Card padding is 16–20, and table cells are 12 with 20 at the card edge.
- Rhythm: tight inside a group (4–8: table cells, a label and its value), generous between groups (24–40: between the identity header and the variables, between panes).

## Separation
- **Cards**: one surface language. Anything that stands on the canvas as a group (tables via `.table-region`, side blocks, issue cards, the run preview, the error panel) is a `--well` card with a 1px `--rule` border, `--radius-card` and `--shadow-card`. Hairlines separate table rows inside a card. The inventory list column and status strip are `--well` with a 1px rule edge. Nothing else has a border or shadow.
- **Left rail**: a 3px bar inset 14px from the card's top and bottom, with radius 0 3px 3px 0. It uses the status colour on issue cards and the accent gradient on the run preview, the selected nav item and the selected list item.

## States
- Hover: accent 4–8% tint on rows, list items and quiet buttons (quiet text turns `--accent-ink`). The primary button darkens to `color-mix(in oklab, var(--accent) 88%, black)` and gets an accent glow. Secondary buttons and inputs move their border toward the accent.
- Focus: 2px outline `var(--accent)`, offset 2px, on every focusable element (`:focus-visible`). Inputs take an accent border with a 3px 22% accent ring instead. Rail items take an inset `--nav-accent` ring (offset -2px).
- Disabled: 45% opacity, `cursor: not-allowed`.
- Selected: list item gets an accent 10% tint, name in `--accent-ink`, and the gradient left rail. Rail item gets a violet mix on `--nav-bg`, white text, a `--nav-accent` icon and the gradient bar at the rail edge. Both use `aria-current="page"`.

## Status and badges
Pills: text in the status colour on a tint of 12% status over `--well` (`color-mix(in oklab, var(--status) 12%, var(--well))`), radius `--radius-pill`. Every pair clears 4.5:1, measured with `contrast.mjs` in both themes.

Accent badges are only for the winning source. "Running", "default" and the active-run count use neutral. A dry run that would change hosts uses the changed tone ("Dry run: would change"), never ok.

## Motion
- 160ms ease-out for hover, focus and disclosure. Status dots for failed and unreachable pulse their halo every 4s. Under `prefers-reduced-motion: reduce`, nothing animates.

## Components
- Button: primary (accent fill, accent-fg text, soft shadow, one per screen), secondary (`--well` fill, 1px `--rule-strong` border), quiet (text only). 36px tall, 8px radius.
- Run log: a terminal card. `--nav-bg` background and `--nav-text` in both themes, `--radius-card`. Destructive is danger text on danger tint, never next to primary.
- Source chip: mono file path; the winning layer in `--accent-ink`, overridden layers in `--ink-muted` struck through.
- Badge: status text on status tint, 12px, 600. `badge-lg` (15px) only for the run status on a run page.
- Danger-quiet button: danger text, no fill; destructive controls on a screen (Cancel run). Needs a second click within 4s to confirm.
- Segmented radio (`segmented-inline`): 2–5 mutually exclusive options (Dry run / Apply, verbosity), selected option on `--well`.
- Preview panel: card with the accent-gradient left rail that holds the live result of a form (matched hosts) and the form's one primary button, whose label states the count and mode ("Dry run on 3 hosts").
- Status dot: 8px core with a 3px halo at 22% of its colour; failed and unreachable pulse.
- Page banner: `banner-ok` (ok tint, ink text) for a completed save, `banner-warn` (changed text on changed tint, 5.4:1 / 6.4:1) for a standing risk. 8px radius inside a page.
- Kind switch: segmented links (`aria-current`) choosing what a form creates, same look as the segmented radio.
- Secret field: write-only. Never pre-filled; on an edit, empty means "keep". Password inputs have a Show/Hide quiet button (`aria-pressed`).
- Issue card: card with the inset left rail in the status colour (danger, unreachable, changed), a badge, a one-line count, host chips, and at most two actions (secondary, then quiet). Used only on the Overview's "Needs attention".
- Side block: card for the Overview's side column (running, next 24 hours, counts). Counts sit in a 2×2 grid: a micro label over a 22px 700 figure.
- Lost-contact banner: `banner-warn`, `role="status"`, shown when a live poll fails and hidden on the next success.
- Invalid field: 2px `--danger` outline, message below in `--danger` 600, linked with `aria-describedby`.

- Nav rail: `--nav-bg` column (dark in both themes), sticky, full window height. Brand (a 28px violet gradient tile with an "a" monogram, then the name), the sections (Overview, Hosts, Groups, Runs, Repositories, Credentials; 20px inline SVG icon, stroke `currentColor`, plus label), then the signed-in name, Sign out and Collapse at the bottom. Collapse switches every page to the icon rail and is remembered per browser. Hosts and Groups each open the inventory with that one list in the second column; there is no Hosts/Groups switch inside the page. Items 38px (44px on touch), 8px radius, `--nav-ink` text, hover tint, current as Selected. The Runs count is a neutral badge, read as hidden text ("2 running").
- Rail sub-items: Runs lists Run playbook, Run command, Templates and Schedules beneath it (everything that starts runs; Runs itself is the history), label only, indented to the parent's label, 32px (44px on touch), weight 400. The open one is Selected; Runs then takes `aria-current="true"` (strong ink and accent icon, no fill). Hidden in the icon rail, where a label-less item would be a blank; there Runs takes the full Selected look, since its selected child can't show, and the Runs page links to Templates and Schedules.
- Tag field: chips (mono, with a × remove button) and a borderless entry inside one box styled as an input, with an accent ring on `:focus-within`. The entry suggests the playbook's tags one at a time. Enter or a comma adds; Backspace on an empty entry removes the last. The comma list stays in the original input, which is the fallback without script.
- Icon rail: the same rail at 64px, showing only the brand tile. Labels are visually hidden; a tooltip (`--well`, `--shadow-pop`, 14px 600, 8px radius) shows on hover and keyboard focus. The Runs count overlays the icon as a 16px violet pill.
- Phone menu: a sticky `--nav-bg` top bar with the brand and a `<details>` Menu button (secondary). A neutral 8px dot on it when runs are active. The sheet overlays the page, 44px items at 15px; Escape and a tap outside close it.
- Skip link: first focusable element, hidden until focused, `--well` background.
- Inventory status strip: above the two panes, `--ink-muted` text: sources (mono), host count, load time, and Reload inventory (secondary) at the end. Hidden when the inventory didn't load; the error panel offers Try again.

## Voice
- Plain and specific, sentence case, verbs on buttons ("Reload inventory", "Open in git"). Errors say what failed, the command, and what to do next.
