# Dashboard redesign

## Objective

Replace the generated HTML report (`src/jobscout/dashboard.py`) with a new desktop UI that the
owner can scan in seconds after each sweep. It must hold the expandable offer row (roadmap
item 1), and leave places for the salary estimator (item 3), the FAIL/FLAG gates (item 4) and
the divergence view (item 5), so that those features drop into the new UI instead of the old one.

Static mockup approved by the owner (synthetic data):
`odd/tasks/dashboard-redesign-mockup.html`. It is the visual reference for every step.

## Problem and why

- The dashboard is the only way to see results: the Telegram notification has been removed.
- Owner feedback on the current UI (2026-10-20):
  - It looks generic.
  - Cards are too long and have no real contrast between them.
  - The right rail shows explanatory boxes ("Dos ejes distintos", "Privacidad") that do not
    belong there.
  - The top nav has a "Filtros" button even though the filters already sit in the rail.
  - Some data is irrelevant: the detail-evidence timestamp and the "Origen" disclosure.
  - The owner wants the model's full justification.
- The page is 2.2 MB (`reports/jobscout-dashboard.html`, 2026-10-19) and renders all 700
  active offers as full cards at once.
- `evaluations.dimensions_json` is stored but never rendered (`dashboard.py` has no reference
  to it). The feedback import shows only as a filter, never on the offer itself (`:2053`).

## Owner decisions (2026-10-20)

- **Desktop only.** No responsive layout. The existing no-breakpoint test stays.
- **Light theme only.** No dark mode for now. Every colour is a token on `:root`, so adding
  dark mode later is cheap.
- **Apply/reject stays, minimal.** Two small buttons in the expanded row, plus an optional
  reason. The export button appears only when local decisions exist. The export format and
  `import-feedback` do not change.
- **Missing dimensions: not investigated yet.** Only 5 of the 700 active offers have a
  non-empty `dimensions_json` in their latest evaluation, and none was written after
  2026-10-01. The investigation is a separate task the owner will schedule. The UI must render
  well both with dimensions and without them.
- **Design inspiration:** the method from Taste Skill / design-taste, written into this card
  as a project-specific visual system (below). Not adopted:
  - No skill package installed via `npx`.
  - No React, Tailwind or animation libraries.
  - Nothing from navbar/footer/landing galleries: this is a tool, not a marketing site.

## Contract (unchanged)

- Self-contained, offline and safe over `file://`. No external URLs, no `@font-face`, no CDN.
- Read-only database access.
- No `innerHTML`: everything goes through `textContent`, and `safeHttpUrl` is used for links.
- Feedback lives in `localStorage` under `jobscout.feedback.v1:<project_id>`. The export is
  `{schema, project_id, exported_at, source, note, feedback: {id: {decision, reason,
  updated_at}}}`, accepted by `feedback.py` `validate_feedback_export` / `_parse_entry`.
- The security, offline, read-only and export tests in `tests/test_dashboard.py` keep their
  intent. Tests that only pin the old look are rewritten.

## Structure

### Header (sticky)

- **Masthead:** `jobscout`, plus one status line that replaces the six KPI cards:
  - date of the last sweep;
  - offers new since the previous sweep;
  - offers evaluated out of offers present;
  - pending details per provider;
  - failed runs, in the signal colour only when greater than 0.

  All of it comes from the database (`search_runs`, detail runs, `evaluations.status`).
- **Tabs, each with a count:** `Ofertas · Discrepancias · Vetos · Palabras clave`.
  - `Discrepancias` appears only when item 5 data exists.
  - `Vetos` appears only when item 4 data exists.
  - On the right: `Exportar N decisiones`, visible only when N > 0.
  - A small ↑ "back to top" control appears in the header after scrolling. There is no
    floating button.

### Toolbar (offers view; replaces the rail)

One horizontal row:
- text search;
- a recommendation segmented control (Todas / Aplicar / Considerar / Descartar);
- a "Solo nuevas" toggle;
- minimum score;
- keyword;
- sort (score / newest);
- a result count.

### Offer row, collapsed (one line)

`score + bar coloured by band | title / company · location · modality | chips | salary | age | chevron`

- **Chips:** recommendation, `nueva` (first seen in the latest sweep, from `first_seen_at`),
  `⚠ flag` (item 4, when present).
- **Salary column:**
  - the value, with a provenance line underneath: `indicado` (stated by the offer, from
    `jobs.salary`), `estimado` (item 3) or `sin dato`;
  - "—" when there is nothing. A number is never invented.
- **Removed from the row:** job id, detail-evidence timestamp, evaluator/model, the origin
  disclosure.

### Offer row, expanded (built on first open, not up front)

- **Left column:**
  - **Justificación del modelo:** the full `rationale`, in a serif reading size.
  - **Salario · bruto anual** (when there is salary data): a range bar with the stated value
    marked on it, plus the stated value and the reference source and bracket. The reference
    and bracket come with item 3; until then only the stated value shows.
- **Right column: Desglose por dimensiones,** one line per dimension:
  - name, weight, bar and score;
  - its own rationale;
  - its evidence quotes.

  Without dimensions the column says so in one line (`sin desglose en esta evaluación`) and
  the justification takes the full width.
- **Full-width strip, four columns:** Puntos fuertes, Carencias, Bloqueos (heading in the
  signal colour), Incógnitas.
- **Footer line:** model · confidence · evaluation date · `✓ aplicar` `✕ descartar` (toggle;
  the reason field opens after choosing) · `Ver la oferta ↗`.
- **Imported decision:** when an imported decision exists, it shows in the footer line.

### Palabras clave view

- One sortable table: keyword, provider, sightings, active, mean score, ≥ 70, decisions.
- Retired pairs are greyed and marked `retirada`.
- The three "Top 10" lists are removed: sorting the table covers them.

### Discrepancias view (item 5, later)

- Same row component as Ofertas, with two score marks (heuristic · LLM), sorted by the size of
  the gap.
- Then a small random control sample of agreements.
- Hidden until `calibration.py` provides scorer-vs-scorer data. Today no heuristic evaluation
  is stored: all 3,207 rows are `opencode`.

### Vetos view (item 4, later)

- FLAG/FAIL offers, each with the quoted posting evidence that triggered the gate, for the
  weekly review.
- Hidden until item 4 lands.

### Removed

- The rail, with Filtros, Acciones, "Dos ejes distintos" and Privacidad.
- The "Alcance del informe" footer.
- The KPI cards.
- The nav "Filtros" link.
- The "Origen" disclosure and the detail-evidence timestamp.
- The enhancement IIFE that rewraps cards in `<details>` (`:2671-2693`).
- Payload fields the UI does not render: `retired_on`/`retired_reason` (unless the keyword
  table shows them), `sighting_count`, `source`, and `detail.checked_at`/`content_status`/
  `http_status`.

## Visual system (DESIGN.md for this report)

**Direction: "printed data sheet".** Warm paper, near-black ink, hairline rules instead of
shadowed cards, one signal colour. Serif for reading, monospace for numbers, sans for UI.

### Tokens (starting values, from the mockup)

| Token | Value | Use |
|---|---|---|
| `--paper` | `#f3f0e8` | page |
| `--sheet` | `#fbfaf6` | hover / expanded row / inputs |
| `--ink` / `--ink-2` / `--ink-3` | `#17160f` / `#55524a` / `#8a867b` | text levels |
| `--rule` / `--rule-2` | `#d8d3c6` / `#e8e4d9` | hairlines, bar tracks |
| `--signal` / `--signal-wash` | `#c8361b` / `#f6e3dc` | high score, apply, blockers, failures |
| `--mid` / `--mid-wash` | `#7a6a1f` / `#f0ead2` | mid score, consider, flag |

### Typography

System stacks only:
- `--serif`: Iowan Old Style / Charter / Georgia;
- `--sans`: system-ui;
- `--mono`: ui-monospace.

Numbers use `font-variant-numeric: tabular-nums`. Keep a fixed type scale as tokens.

### Spacing and scale (borrowed from Ramp's DESIGN.md, styles.refero.design)

Ramp ("editorial finance desk") is the closest refero style to this direction. Only its
measurable discipline is adopted. Its yellow, its `lausanne` web font, its radii and its
marketing components are not.

- Spacing comes only from a 4 px base scale: 4, 8, 12, 16, 24, 32, 48. Use no other values.
- Type sizes are a closed set of tokens: 11, 12, 13, 14, 16, 22. Hierarchy comes from size,
  case and tracking before weight. 600 is the only bold.
- Micro-labels are uppercase at 11 px with 0.05–0.08em tracking.
- Elevation is a 1 px hairline, never a shadow. Body text is never centred.

### Banned

- Gradients and card shadows.
- Purple/indigo.
- `rounded-2xl`-style pill radii (corners are square or ≤ 2 px).
- Inter.
- Decorative emoji (only ↗ › ✓ ✕ ⚠ ↑ as functional glyphs).
- Identical stacked cards.
- Explanatory prose boxes inside the UI.
- Scroll-triggered animations.
- Loading screens or skeletons (the data is embedded, nothing loads).
- Hero animation.
- Repeated calls to action.
- Leave-page prompts.

### Allowed interaction

- **Hover:** row highlight, sortable header. Every hover state has a matching
  `:focus-visible` state.
- **Button feedback:**
  - pressed state for apply/reject;
  - the existing "Guardado en este navegador" confirmation, which fades out;
  - the export count updating live.
- **Transitions, two only:** row open/close (~150 ms) and the tab switch (a minimal fade or
  none). Both are disabled under `prefers-reduced-motion`.
- **Error and empty states:**
  - a filter with no results shows "Ninguna oferta con estos filtros · limpiar";
  - an evaluation with `status=error` shows as `sin evaluar (error)`, never as a 0 score;
  - unavailable `localStorage` shows a one-line warning that decisions will not be kept;
  - sweep failures show in signal colour in the status line.
- **Input limit:** only a length cap on the decision reason. Nothing else needs validating:
  filters are selects, and `import-feedback` validates the export.

### Speed

- Render collapsed rows in batches of 50, with `Mostrar 50 más`.
- Build expanded content lazily on first open.
- Trim the payload.
- Record the page weight and the time to first screen before and after, in the progress log.

### Pre-flight checklist (every step, before handing back)

1. No external URL, `@font-face` or CDN; the page opens over `file://` with the network off.
2. Text contrast AA against `--paper` and `--sheet`.
3. Keyboard: tabs, toolbar, rows (open/close) and buttons are reachable, with a visible focus.
4. `prefers-reduced-motion` removes every transition.
5. Every colour and font size comes from a token; no raw hex outside `:root`.
6. Numbers are tabular and right-aligned in tables.
7. The signal colour is used only for its four meanings (high score, apply, blocker, failure).
8. Empty and error states render (no offers, no dimensions, evaluation error, no salary).
9. Nothing on the banned list.
10. Compare against `dashboard-redesign-mockup.html` at a desktop width of 1280–1440 px.

## Acceptance criteria

- Full offline suite green after every step.
- The export and `import-feedback` tests (`tests/test_dashboard.py` feedback block,
  `tests/test_feedback.py`) pass without modification.
- The security, offline and read-only tests keep their assertions; only look-pinning tests
  are rewritten, and each rewrite is listed in the step's commit message.
- The generated page matches the structure above and passes the pre-flight checklist.
- Page weight and first-render time are lower than the 2026-10-19 baseline (2.2 MB).
- `README.md` and `README.en.md` describe the new views in the same commit as the change.

## Tasks (in order; suite green and one commit per step)

- [ ] DR-1 **Payload.**
  - Add `dimensions` (parsed `dimensions_json`), `is_new` (`first_seen_at` within the latest
    sweep cycle), the stated salary (`jobs.salary`), the imported decision per offer, and the
    status-line counts.
  - Drop the unused fields.
  - UI unchanged; only payload tests change.
- [ ] DR-2 **Shell and visual system.**
  - Tokens, header with status line, tabs, toolbar.
  - Remove the rail, KPIs, explanation boxes and footer.
  - Rewrite the look-pinning tests (ids, classes, tokens, grids).
  - Run the pre-flight checklist.
- [ ] DR-3 **Offer row.** Collapsed line, batched rendering, lazy expanded body (justification,
  dimensions or their absence, four lists, salary block with the stated value, footer), and
  the empty/error states. Roadmap item 1 is closed here.
- [ ] DR-4 **Palabras clave view.** One sortable table, retired pairs greyed, Top 10 lists
  removed.
- [ ] DR-5 **Minimal decision + conditional export.** Buttons in the footer line, the export
  count in the header, and the reason length cap. Export tests untouched.
- [ ] DR-6 **Speed pass.** Measure weight and time to first screen against the baseline and
  record both here.
- [ ] Later, owned by their own cards:
  - Discrepancias tab (item 5);
  - Vetos tab and the `⚠ flag` chip (item 4);
  - the estimated salary and the reference line (item 3).

  Each one fills the slot this card leaves; none of them is built here.

## Delegation notes

- Each DR step goes to the DeepSeek `ejecutor`.
- The brief carries the step, the relevant sections of this card and the mockup path. The
  success command is `.venv/bin/python -m pytest -q`.
- The orchestrator reviews every diff, checks which tests were rewritten and why, runs the
  pre-flight checklist on the generated page, and shows the owner the result before the
  next step.

## Progress, verification, and next step

- 2026-10-20: scope and design agreed with the owner. Card and mockup written. No code
  changed. Next: DR-1 in a new session.
