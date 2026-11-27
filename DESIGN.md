# Design System: ansible-gui (assumed, confirm before shipping)

Tokens, components and states. What each screen does with them (the launcher, the playbook list, the inventory and credentials pages) is in [SCREENS.md](SCREENS.md): read both before changing a screen.

Character: a violet control room, after BorgWarehouse. A dark nav rail and soft white cards on a lavender-tinted canvas. Calm and friendly, but it still tells you exactly where every value came from.

World: inventory files, rack labels, terminal logs. The signature element comes from this world: every variable carries its **source stack**, the file that won and the layers it overrode, shown inline rather than hidden. The run log is drawn as a terminal (dark in both themes).

## Typography
- Families: **Inter** (UI, prose and headings; headings 700 with -0.02em tracking) and **Red Hat Mono** (variable names, values, file paths, hostnames: things people copy or compare character by character). Loaded by a Google Fonts `<link>` with `preconnect` and `display=swap` in `base.html`; fallbacks `system-ui, sans-serif` and `ui-monospace, monospace` so an offline homelab still renders.
- Scale (px), each with its token or its one use: 11 `--fs-2xs` (micro labels), 12 `--fs-xs` (meta: help, badges, paths, counts), 13 `--fs-mono` (the run log and code wells), 14 `--fs-sm` (body, tables, decision text), 15 `--fs-md` (`btn-run`, `badge-lg`, phone menu items), 18 `--fs-lg` (card titles), 34 `--fs-xl` (the identity heading, about 2.4× body, tight tracking). Literals with one use each: 16 (inputs under 640px, so iOS doesn't zoom on focus; 14 from 640px), 22 (the Overview's count figures), 28 (the identity heading under 640px).
- Decision text is 14px on every width: text someone reads to decide what a run will do. The inventory note under a field, the names and old values in the run preview's override list, the attribution mismatch note, a source stack's overridden values. The 12px meta beside it (the file a value is from, host counts, a role's default) stays 12px on a desktop.
- Help text: 12px (`--fs-xs`) on a desktop, 14px (`--fs-sm`) under 640px.
- Micro labels: 11px, 600, uppercase, 0.06–0.08em tracking. Used for table headers, `dt` labels (facts, run meta, counts) and the eyebrow (eyebrow in `--accent-ink`).
- Weights: 400 body, 500 nav items and run-meta values, 600 labels, 700 headings and big figures.
- Figures (counts, durations, times) use the text face with `tabular-nums`, not mono.

## Color
Neutrals are lavender-tinted. Tokens live in `src/ansible_gui/static/app.css` under `:root`, and twice for dark: `prefers-color-scheme: dark` and `[data-theme=dark]` hold the same values and must be changed together. Ratios are computed from the tokens with the WCAG 2 formula, tints mixed in oklab as the CSS does.

| Token | Light | Dark | Why |
|---|---|---|---|
| `--canvas` | `#f7f7fb` | `#0d0f15` | Page background |
| `--well` | `#ffffff` | `#15181f` | Cards, inputs, the inventory list column |
| `--plane` | `#efedf8` | `#1d2130` | Recessed fills: segmented tracks, chips, stack counts |
| `--code` | `#f6f5fb` | `#10131a` | Code wells inside cards |
| `--ink` | `#1c1b29` | `#e6e8ef` | Text |
| `--ink-muted` | `#5d6478` | `#9aa1b3` | Meta text; 5.5:1 / 7.4:1 on canvas, 5.9:1 / 6.9:1 on well |
| `--rule` / `--rule-strong` | `#e6e4f0` / `#d6d3e6` | `#272c38` / `#333a49` | Card borders and table hairlines / control borders |
| `--accent` | `#6d4aff` | `#8b6bff` | Violet signature, as a fill: the primary button, the selected-item bar, focus rings. 5.15:1 under its label in both themes |
| `--accent-ink` | `#5a37f0` | `#a28cff` | The accent as text (winning source, eyebrow, current item). Light 6.5:1 on well, 6.1:1 on canvas; dark 6.5:1 on well, 7.1:1 on canvas; above 5.6:1 on its own 10% tint |
| `--accent-2` | `#8b6bff` | `#a28cff` | Top stop of the accent gradient (rails, brand tile) |
| `--accent-fg` | `#ffffff` | `#0d0f15` | Label on accent |
| `--danger` | `#b91c1c` | `#ff8a8a` | Failed runs, destructive actions, resolve errors |
| `--ok` | `#0b7340` | `#2ee48a` | Ansible "ok" |
| `--changed` | `#8a5a00` | `#fbbf24` | Ansible "changed", and attribution warnings |
| `--unreachable` | `#a3206e` | `#f075b5` | Ansible "unreachable" |
| `--nav-*` | same in both themes | `--nav-bg` `#08090d` in dark | The rail and phone bar are always dark (`#12111c`, ink `#a9a6c4` at 8.0:1, current item white on a violet mix) |
| `--log-*` | same in both themes | same | The run log's status colours (ok, changed, failed, unreachable, muted): the dark theme's, since the log is dark in both |

Shadows: `--shadow-card` (1px contact plus a soft 16px drop), `--shadow-pop` (tooltips, menus), `--shadow-btn` (primary button). They are heavier in dark mode.

Tints are `color-mix(in oklab, …)` of a token, at these strengths only:
- 4–8% accent: hover on rows, list items, nav items and quiet buttons.
- 10% accent: a selected list item. 12% accent over `--well`: the accent badge and a pressed chip toggle. 14% accent: chip hover and a picker's chosen option.
- 12% status over `--well`: pills. 12% status (or `--ink-muted`) over `--canvas`: page banners.
- 22%: the input focus ring and a status dot's halo.
- 30% accent: the primary button's hover glow and the source stack's line. 35% / 45% accent into `--rule-strong`: input and secondary button hover borders.
- 55% danger: into `--rule` for the danger zone's frame, into `--rule-strong` for `btn-danger`'s border. 10% / 14% danger: danger-quiet hover and armed.
- 8% changed over `--well`: an attribution mismatch row. 25% changed: the changed banner's border.
- 88% accent with black: the primary button's hover fill. The nav rail's violet mixes (24–55%) belong to the rail.

## Layout
- Widths: `--rail` 200px (nav rail), `--rail-icon` 64px (icon rail), `--icon` 20px.
- Bands: phone under 640 (top bar and menu), tablet 640–899 (icon rail on every page), desktop 900 and up (full rail, or the icon rail on every page if collapsed), wide 1200 and up (the Overview's side column), 1280 and up (the launcher's side column; below it the Run button floats). The rail is the same width on every page: it never changes between sections.

## Sizes
- Controls: buttons 36px tall (`btn-run` 42px, 48px when it floats), inputs and chip fields 38px, icon buttons 36px square, checkbox and radio rows 28px, nav items 38px and rail sub-items 32px. Under `pointer: coarse` every target is at least 44px.

## Radii
- `--radius-control` 8px: buttons, inputs, segmented tracks, code wells, banners.
- `--radius-card` 16px: cards and the run log.
- `--radius-pill` 999px: badges, chips, stack counts.

## Spacing
- Base 4px. Scale `--s1` 4 / `--s2` 8 / `--s3` 12 / `--s4` 16 / `--s5` 20 / `--s6` 24 / `--s10` 40. Card padding is 16–20, and table cells are 12 with 20 at the card edge.
- Below the scale, 2px only inside one item: between the stacked lines of a value (old value, its file, its hosts). The left rail's 14px inset is its own.
- Rhythm: tight inside a group (4–8: table cells, a label and its value), generous between groups (24–40: between the identity header and the variables, between panes).

## Separation
- **Cards**: one surface language. Anything that stands on the canvas as a group (tables via `.table-region`, side blocks, issue cards, the run preview, the error panel, the run log) is a `--well` card (the log `--nav-bg`) with a 1px `--rule` border, `--radius-card` and `--shadow-card`. Hairlines separate table rows inside a card.
- **Borders and shadows, the whole list**: cards as above; controls (inputs, chip fields, secondary buttons, segmented tracks: 1px `--rule-strong`; the primary button `--shadow-btn`); popovers (menus, dropdown lists, tooltips, the floating Run button: `--shadow-pop`); code wells (1px `--rule`); the status strip (a 1px rule edge); the changed banner's tinted border; the danger zone frame. Nothing else has a border or a shadow.
- **Left rail**: a 3px bar inset 14px from the card's top and bottom, with radius 0 3px 3px 0. It uses the status colour on issue cards and the accent gradient on the run preview, the selected nav item and the selected list item.

## States
- Hover: accent 4–8% tint on rows, list items and quiet buttons (quiet text turns `--accent-ink`). The primary button darkens to `color-mix(in oklab, var(--accent) 88%, black)` and gets an accent glow. Secondary buttons and inputs move their border toward the accent.
- Focus: 2px outline `var(--accent)`, offset 2px, on every focusable element (`:focus-visible`). Inputs take an accent border with a 3px 22% accent ring instead. Rail items take an inset `--nav-accent` ring (offset -2px).
- Disabled: 45% opacity, `cursor: not-allowed`.
- Selected: list item gets an accent 10% tint, name in `--accent-ink`, and the gradient left rail. Rail item gets a violet mix on `--nav-bg`, white text, a `--nav-accent` icon and the gradient bar at the rail edge. Both use `aria-current="page"`.

## Status and badges
Pills: text in the status colour on a tint of 12% status over `--well` (`color-mix(in oklab, var(--status) 12%, var(--well))`), radius `--radius-pill`. Every pair clears 4.5:1, computed in both themes; the lowest is 4.98:1 (light ok and changed).

Accent badges are only for the winning source. "Running", "default" and the active-run count use neutral. A dry run that would change hosts uses the changed tone ("Dry run: would change"), never ok.

## Motion
- 160ms ease-out (`--ease`) for hover, focus and disclosure. A tooltip shows after 350ms on hover, at once on keyboard focus. Status dots for failed and unreachable pulse their halo every 4s. A two-click confirm stays armed for 4s. Under `prefers-reduced-motion: reduce`, nothing animates.

## App shell
- Nav rail: `--nav-bg` column (dark in both themes), sticky, full window height. Brand (a 28px violet gradient tile with an "a" monogram, then the name), the sections (Overview, Hosts, Groups, Runs, Repositories, Credentials; 20px inline SVG icon, stroke `currentColor`, plus label), then the signed-in name, Sign out and Collapse at the bottom. Collapse switches every page to the icon rail and is remembered per browser. Hosts and Groups each open their own page; there is no Hosts/Groups switch inside a page. Items 38px (44px on touch), 8px radius, `--nav-ink` text, hover tint, current as Selected. The Runs count is a neutral badge, read as hidden text ("2 running").
- Rail sub-items: Runs lists Run playbook, Run command, Templates and Schedules beneath it (everything that starts runs; Runs itself is the history), label only, indented to the parent's label, 32px (44px on touch), weight 400. The open one is Selected; Runs then takes `aria-current="true"` (strong ink and accent icon, no fill). Hidden in the icon rail, where a label-less item would be a blank; there Runs takes the full Selected look, since its selected child can't show, and the Runs page links to Templates and Schedules.
- Icon rail: the same rail at 64px, showing only the brand tile. Labels are visually hidden; a tooltip (`--well`, `--shadow-pop`, 14px 600, 8px radius) shows on hover and keyboard focus. The Runs count overlays the icon as a 16px violet pill.
- Phone menu: a sticky `--nav-bg` top bar with the brand and a `<details>` Menu button (secondary). A neutral 8px dot on it when runs are active. The sheet overlays the page, 44px items at 15px; Escape and a tap outside close it.
- Skip link: first focusable element, hidden until focused, `--well` background.

## Components
- Button: primary (accent fill, accent-fg text, soft shadow, one per screen), secondary (`--well` fill, 1px `--rule-strong` border), quiet (text only). 36px tall, 8px radius. `btn-run`, a form's Run or Save button, is the primary at 42px and 15px (48px when it floats). Danger-quiet (danger text, no fill) is for destructive controls on a screen, like Cancel run, and needs a second click within 4s.
- Row actions: list rows end with `row-actions` (right-aligned, wrapping): icon buttons (`btn-icon`, 36px square, 44px on touch, 18px icon from `_icons.html`). Run (play) is the secondary style; Schedule (stopwatch), New template (copy) and Edit (pencil) are quiet. Each has an aria-label and a `tip` showing the same words above it, after 350ms on hover and at once on keyboard focus; the last in a row aligns right. A label ending in "…" opens the launcher; a schedule's run button starts it (two clicks for apply, the second label replacing the icon).
- Touch: icon actions show their name beside the icon under `pointer: coarse` (no hover to reveal a tooltip). On a phone, a table row with actions puts them on their own line under the row's name.
- Segmented radio (`segmented-inline`): 2–5 mutually exclusive options (Dry run / Apply, verbosity), selected option on `--well`. As links with `aria-current` it is the kind switch (what a form creates), in the same look.
- Badge: status text on status tint, 12px, 600. `badge-lg` (15px) only for the run status on a run page.
- Status dot: 8px core with a 3px halo at 22% of its colour; failed and unreachable pulse.
- Page banner: `banner-ok` (ok tint, ink text, 600) for a completed save, `banner-warn` (changed text on changed tint, 4.7:1 light / 9.6:1 dark) for a standing risk, `banner-neutral` (ink on a 12% `--ink-muted` tint) for work still going, like a pull started from the page; the ok or warn banner takes its place when it ends. 8px radius inside a page. The lost-contact banner is a `banner-warn` with `role="status"`, shown when a live poll fails and hidden on the next success.
- Source chip: mono file path; the winning layer in `--accent-ink`, overridden layers in `--ink-muted` struck through.
- Run log: a terminal card in `--fs-mono` (13px). `--nav-bg` background and `--nav-text` in both themes, `--radius-card`. While a run is going the box stays on its last line and the page on the box's foot, so new lines land in view; scrolling up stops either one until the reader scrolls back down, and neither animates. Lines take the status they report, in the dark theme's status colours whichever theme is on (the card is dark in both): the `--log-*` tokens: ok and a diff's added lines `--log-ok` `#2ee48a` (11.2:1 on the light theme's `--nav-bg`, 11.9:1 on the dark one's), changed and warnings `--log-changed` `#fbbf24` (11.2 / 11.9), failed lines `--log-failed` `#ff8a8a` (8.2 / 8.8), unreachable `--log-unreachable` `#f075b5` (7.1 / 7.5), skipped lines, a diff's removed lines and ansible-gui's own lines `--log-muted` `#9aa1b3` (7.2 / 7.7); PLAY, TASK and recap headings are `--nav-ink-strong` at 700. Red is only ever something that failed: a diff's removed lines are the old content, muted like every replaced value here, and its added lines are what stands out. The words stay ("ok:", "changed:", "fatal:"): colour is never the only sign. Destructive is danger text on danger tint, never next to primary.
- Preview panel: card with the accent-gradient left rail that holds the live result of a form (matched hosts) and the form's one primary button, whose label states the count and mode ("Dry run on 3 hosts").
- Issue card: card with the inset left rail in the status colour (danger, unreachable, changed), a badge, a one-line count, host chips, and at most two actions (secondary, then quiet). Used only on the Overview's "Needs attention".
- Side block: card for the Overview's side column (running, next 24 hours, counts). Counts sit in a 2×2 grid: a micro label over a 22px 700 figure.
- Danger zone (every delete: credential, repository, template, schedule): `_danger_zone.html`. A "Danger zone" heading 40px below the content, over a frame (`--well`, 1px border of `--danger` mixed 55% into `--rule`, `--radius-card`, 20px padding); any "used by" warnings first, then a row with "Delete this <thing>" (600) and its consequence on the left, and on the right `btn-danger`: outlined in danger, filling with `--danger` on hover and while armed. Two clicks: the first relabels it "Click again to delete <name>…".
- Empty states (`.empty` is a flex column): the sentence in one `<span>` (inline links stay inline), then the call to action as its own link.
- Subject line (`.subject-meta`): a paragraph, so sentences wrap as text; `.subject-meta-row` is the flex row for chip lists ("Inside", "Contains").

## Forms
- Field width: in forms (`.launcher`), inputs, selects and secret fields span the card; `input-small` and `input-medium` only narrow fields outside forms.
- Invalid field: 2px `--danger` outline, message below in `--danger` 600, linked with `aria-describedby`.
- Dropdowns: every `select.input` is upgraded by `select.js` into the picker's look (a field-like button with the choice, a second muted line from the option's `data-desc`, a chevron; a popover listbox with the chosen option tinted and the keyboard's current option outlined (2px inset `--accent`), keyboard: arrows, Home/End, Enter, Escape, type-ahead). The `<select>` stays underneath and is what's submitted. Touch screens keep the native picker.
- Chip field (Targets, Tags, Environment secrets): one box styled as an input that holds a chip per choice (mono, with a × remove button; a secret's chip is its name in the text face, then its `$VARIABLE` in muted mono 12px) and a borderless entry, with an accent ring on `:focus-within`. Clicking the box or typing opens the dropdown's list under the whole box (`--well`, `--shadow-pop`), without what is already a chip; choosing adds a chip. Enter or a comma adds what was typed (a colon too in Targets, outside a slice); Backspace on an empty entry removes the last; the entry's placeholder says what an empty field means ("all of the playbook's hosts"), then "Add a …". Targets and tags take free text, since a target is a pattern (each term is a chip: `web`, `!web2`); secrets only what is on the list. Tags is one field in the same terms: a tag runs only its tasks and `!tag` skips them. That `!` is the form's own shorthand, not Ansible's, so the server takes it apart into `--tags` and `--skip-tags`, the help line says so, and everything after the form (the run page, the template changes, the command line in the log) still says Only tags and Skip tags. What the form sends stays underneath and is the fallback without script: the comma list, the pattern, the secrets' tick boxes. Touch screens keep the browser's own list, and the tick boxes for secrets.
- Suggestion list (`suggest.js`): the list a chip field's entry opens, in the dropdown's look. Targets: a "Groups" heading, then each group in mono with its host count in muted `--fs-xs` at the end of the line (`all` last), then "Hosts". Tags: the playbook's tags. Environment secrets: each secret's name with its `$VARIABLE` at the end of the line. It matches what is being typed, names that start with it first; arrow keys, Enter and Escape as in a dropdown.
- Secret field: write-only. Never pre-filled; on an edit, empty means "keep". Password inputs have a Show/Hide quiet button (`aria-pressed`) and `autocomplete="off"`. Every field opts out of password managers (`data-1p-ignore`, `data-lpignore`, `data-bwignore`, `data-form-type="other"`, set by early.js): nothing here is the person's own login.
- Names that must be capitals (environment variables) carry `data-upper`: typed in any case, shown in capitals at once (spaces and dashes become underscores), and capitalised again on save.

## Voice
- Plain and specific, sentence case, verbs on buttons ("Reload inventory", "Open in git"). Errors say what failed, the command, and what to do next.
