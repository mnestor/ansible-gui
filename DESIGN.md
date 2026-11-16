# Design System: ansible-gui (assumed, confirm before shipping)

Character: a server-room clipboard — dense, cool and exact; it tells you where every value came from.

World: inventory files, rack labels, terminal logs. The signature element comes from this world: every variable carries its **source stack**, the file that won and the layers it overrode, shown inline rather than hidden.

## Typography
- Families: **Red Hat Text** (UI and prose), **Red Hat Display** (the one identity heading per screen), **Red Hat Mono** (variable names, values, file paths, hostnames: things people copy or compare character by character). Ansible's own house faces, so the tool reads as part of that world rather than a generic dashboard. Loaded by a Google Fonts `<link>` with `preconnect` and `display=swap` in `base.html`; fallbacks `system-ui, sans-serif` and `ui-monospace, monospace` so an offline homelab still renders.
- Scale (px): 12 / 13 / 15 / 18 / 32. Body and tables are 13 (dense expert tool, deliberately low contrast inside panes); the identity heading (host or group name) is 32 in Display, about 2.5× body, so the subject of the screen is found at once.
- Weights: 400 body, 600 labels and identity column, 700 the identity heading only.
- Figures (counts, durations, times) use the text face with `tabular-nums`, not mono.

## Color
Neutrals are cool slate (a server room, not paper). Tokens live in `app/static/app.css` under `:root` and `[data-theme=dark]` / `prefers-color-scheme: dark`.

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
- Focus: 2px outline `var(--accent)`, offset 2px, on every focusable element (`:focus-visible`).
- Disabled: 45% opacity, `cursor: not-allowed`.
- Selected (sidebar item): `--well` background + 3px left bar in `--accent`.

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

## Motion
- 120ms ease-out for hover and disclosure; nothing else animates.

## Components
- Button: primary (accent fill, accent-fg text, one per screen), secondary (transparent, 1px `--ink-muted` outline), quiet (text only). Destructive is danger text on danger tint, never next to primary.
- Source chip: mono file path; the winning layer in `--accent`, overridden layers in `--ink-muted` struck through.
- Badge: status text on status tint, 12px, 600.

## Voice
- Plain and specific, sentence case, verbs on buttons ("Reload inventory", "Open in git"). Errors say what failed, the command, and what to do next.
