# Dashboard unreviewed view

## Objective
Add a local, self-contained dashboard view that isolates the active offers which
have **no imported human decision**, so the reader can work the real review
backlog that exists in SQLite. The view is built from durable stored evidence
only (the absence of a `job_feedback` row) and is kept clearly separate from the
report's existing browser-local review filter.

The view must never invent a metric to describe itself. In particular it must
not fabricate a "latest cycle" count, rate, or bucket, and it must not turn an
empty import into an imaginary backlog.

## Scope
- Read the imported human decision for each shown offer from `job_feedback`
  through one batched, read-only SELECT, and expose an `imported_review` state
  on each offer in the payload.
- Add a dashboard control that isolates the shown offers without an imported
  human decision, using the existing filter path so the visible list and the
  filter summary can never disagree.
- Keep the imported-decision axis distinct from the browser-local review filter
  (`reviewed` / `unreviewed`), which reads `localStorage` marks, not SQLite.
- Preserve the human decision UI exactly: binary **Aplicar / Descartar** local
  buttons, with a stored legacy `consider` still displayed as historical and
  replaceable. No third choice is added.
- Keep the report read-only, offline, `file://`-safe, and within the existing
  reduced-motion guard.
- Keep the summary and keyword analytics consistent with the imported-decision
  source they already use.

Out of scope: any "new" bucket or latest-cycle metric; schema changes;
migrations; CLI commands; collectors; scoring; `import-feedback`; the model
recommendation vocabulary; any new external asset or network call.

## Constraints
- **Read-only SQLite.** The new read joins the existing read-only connection
  (`connect_readonly`, `mode=ro`, `PRAGMA query_only`) and issues `SELECT` only.
  It must not create, migrate, or write the database, directly or through a
  helper.
- **No fabricated latest-cycle metric.** `search_runs.cycle_id` is a nullable,
  human-set convention and runs from different platforms or cycles are not
  guaranteed comparable. The view must not compute or display a "new in the
  latest cycle" count, rate, or bucket. The only authorized isolation predicate
  is the absence of a `job_feedback` row for the offer.
- **Binary decisions with legacy `consider` visible.** The active human decision
  stays `apply` / `reject` rendered as **Aplicar / Descartar**. A stored legacy
  `consider` continues to render as a historical (legacy) decision that can be
  replaced; it is never offered as a new button.
- **Analytics consistency.** `summary.human_reviewed_jobs` and the keyword
  `human_*` metrics keep reading `job_feedback`, unchanged. The new per-offer
  read must not alter those numbers. The browser-local "Revisadas (tu decisión)"
  KPI stays a separate signal and is never merged with the imported count.
- **Honest when nothing is imported.** With an empty `job_feedback` table the
  view states that no decision has been imported and points to
  `jobscout import-feedback`; it shows no backlog count it cannot support and
  does not silently present every active offer as a measured backlog.
- **Offline and `file://` safe.** One self-contained HTML file: no CDN, no
  external font or asset, no network request, no server. The view must still
  render when `localStorage` is unavailable (as some browsers restrict it on
  `file://`).
- **Reduced motion.** No new `@media` query and no new animation. Any transition
  a new control needs is covered by the report's single existing
  `@media (prefers-reduced-motion:reduce)` guard.
- **Data minimization.** Only the imported decision value and its review state
  are serialized for the view. The imported free-text reason is never placed in
  the HTML, matching the report's existing minimization boundary.
- **Exact allowed files.** Only these files may be changed:
  - `src/jobscout/dashboard.py` — payload and rendered view.
  - `tests/test_dashboard.py` — focused tests.
  - `README.md` — dashboard section kept in sync with the new semantics.
  - `README.en.md` — English dashboard section kept in sync.
  - `odd/tasks/dashboard-unreviewed-view.md` — this artifact.
  `reports/jobscout-dashboard.html` is regenerated offline as a git-ignored
  output, not an authored source edit. No other file is authorized (no
  `db.py`, `cli.py`, `models.py`, migration, collector, or `NEXT_STEPS.md`).
- **No commit.** No `git add`, commit, or push is authorized for this change.

## Tasks
- [x] DU-1 Read imported decisions read-only: add one batched SELECT in
  `dashboard.py` that returns the `job_feedback` decision per shown job id
  (missing ids stay absent, never defaulted to a value). Attach an
  `imported_review` object to each offer (`reviewed` boolean, `decision` value,
  no reason). Do not touch `_summary` or the keyword analytics.
- [x] DU-2 Render the isolated view: add a rail control that filters the shown
  offers down to those without an imported decision, routed through the single
  `applyFilters()` / `activeFilterCount()` path. Label it as "imported decision"
  in Spanish while the stored keys stay English, and keep the browser-local
  `reviewed` / `unreviewed` filter visibly separate.
- [x] DU-3 Preserve the decision UI: keep **Aplicar / Descartar** binary and the
  legacy `consider` historical label; add no third button and no new stored
  vocabulary.
- [x] DU-4 Honest empty-import state: when no decision has been imported, the
  view says so and references `jobscout import-feedback`; it never shows a
  fabricated backlog figure.
- [x] DU-5 Consistency check: confirm the new read does not change
  `summary.human_reviewed_jobs`, the keyword `human_*` metrics, or the
  browser-local KPI, and that no offered metric is derived from `cycle_id`.
- [x] DU-6 Accessibility, motion, offline: give the new control an accessible
  name and a non-colour state (`aria-pressed` or equivalent); add no `@media`
  query and keep every transition under the existing reduced-motion guard;
  verify the view renders with `localStorage` unavailable.
- [x] DU-7 Focused tests in `tests/test_dashboard.py`, including:
  - `test_unreviewed_view_isolates_offers_without_imported_decision`
  - `test_imported_review_is_a_single_readonly_select`
  - `test_unreviewed_view_is_honest_when_no_feedback_is_imported`
  - `test_imported_review_does_not_change_keyword_analytics_or_summary`
  - `test_reviewed_view_keeps_consider_legacy_and_binary_buttons`
  - `test_unreviewed_view_adds_no_media_query_and_keeps_one_reduced_motion_guard`
  - `test_imported_review_never_serializes_the_imported_reason`
  - (also added `test_imported_review_read_leaves_the_database_unchanged` and
    `test_unreviewed_view_renders_when_localstorage_is_unavailable`)
- [x] DU-8 Focused test run: `py -m pytest tests/test_dashboard.py -q`.
- [x] DU-9 Full offline suite (parent bounded verification):
  `python -m pytest -q` → `913 passed in 17.53s`.
- [x] DU-10 Regenerate the report offline (parent bounded verification) and
  confirm the isolated view renders, the imported-note branch is honest, and the
  HTML stays a single self-contained file. Exact command, blocker, and
  render evidence are recorded in *Bounded verification* below.
- [x] DU-11 Boundaries: confirm only the authorized files changed, the report is
  git-ignored, and no commit or push was made.

## Progress

Implemented (no commit, no push). The results under *Observed evidence* were
observed in this authoring session. DU-9 (full offline suite) and DU-10 (report
regeneration) were completed later by the parent's bounded verification; their
exact command results are recorded under *Bounded verification* at the end of
this file.

### What changed

- `src/jobscout/dashboard.py`
  - `_fetch_imported_feedback(conn, job_ids)` — one batched read-only `SELECT
    job_id, decision FROM job_feedback WHERE job_id IN (...)`; ids without a row
    are absent, so "not imported" is never defaulted to a value. Only the
    decision is read; the reason is never selected.
  - `_imported_review(decisions, job_id)` — serializable
    `{"reviewed": bool, "decision": str|None}`, reason omitted.
  - `build_payload` calls the batched read once for every shown offer and adds
    `imported_review` to each offer. `_summary` and the keyword analytics are
    unchanged.
  - Rail control `#filter-imported-review` (**Solo sin decisión importada**) sets
    `filters.importedReview`, routed through the single `applyFilters()` /
    `activeFilterCount()` / `refreshFilterState()` path, with `aria-pressed`.
  - `refreshImportedReviewNote()` renders the honest empty-import message and the
    `jobscout import-feedback` reference; no backlog count is shown and no metric
    is derived from `cycle_id`.
  - Hue-free `.filter-toggle` styling reuses the ink marker; no new `@media`
    query and no new animation.
- `tests/test_dashboard.py` — nine new focused tests (see DU-7).
- `README.md` / `README.en.md` — dashboard sections document the view, the axis
  separation, the empty-import honesty, and the explicit `cycle_id` limitation.

### Observed evidence

- `py -m pytest tests/test_dashboard.py -q -k "imported or unreviewed"`
  → `9 passed, 94 deselected in 1.33s`
- `py -m pytest tests/test_dashboard.py -q` → `103 passed in 6.97s`
- `node --check` on the embedded `_SCRIPT` → accepted with no syntax errors.
- `git status` (after the change) shows this change's five authored files touched
  (`src/jobscout/dashboard.py`, `tests/test_dashboard.py`, `README.md`,
  `README.en.md`, `odd/tasks/dashboard-unreviewed-view.md`). `reports/` was not
  touched: it is git-ignored (`.gitignore` line 53, `/reports/`). No commit or
  push was made.
  Pre-existing unrelated notifications work-in-progress edits (`.env.example`,
  `cli.py`, `config.py`, `tests/test_cli.py`, `tests/test_config.py`, `notify.py`,
  `test_notify.py`, `local-notifications.md`) were left untouched.

### Bounded verification (parent)

Read-only verification only: no source or test file was modified, the database was
opened read-only, and `README.md` / `README.en.md` were not touched.

- DU-8 focused suite, exactly:
  `python -m pytest tests/test_dashboard.py -q` → `103 passed in 6.44s`.
- DU-9 full offline suite, exactly: `python -m pytest -q` → `913 passed in 17.53s`.
- DU-10 report regeneration. The preferred command
  `python -m jobscout dashboard --output reports/jobscout-dashboard.html --top 25`
  is blocked in this environment: there is no editable install, so the module is
  not importable (`C:\Python314\python.exe: No module named jobscout`; exit 1).
  The README documents the offline fallback `PYTHONPATH=src` (README lines 46–47),
  so the report was regenerated with:
  `PYTHONPATH=src python -m jobscout dashboard --output reports/jobscout-dashboard.html --top 25`
  → exit 0, stdout
  `wrote reports\jobscout-dashboard.html: 25 offer(s), 25 scored, 32 keyword(s), 166310 byte(s)`.
  Default local DB `data/jobscout.sqlite3` (435 active offers) was used; its mtime
  (`Sep 18 23:35`) and bytes were unchanged after generation, confirming read-only.
- Render evidence, no browser and no network: the embedded view `<script>` from
  the generated report was executed under a minimal non-browser DOM shim
  (`node v24.14.0`). Observed on the real payload
  (`summary.human_reviewed_jobs == 47`, 25 shown offers):
  - 25 offer cards render.
  - Clicking `#filter-imported-review` isolates **11** cards, exactly the payload's
    offers with `imported_review.reviewed == false` (14 of 25 carry an imported
    decision); `aria-pressed="true"`, `#filters-summary == "1 filtro activo"`.
  - Clicking again restores 25 cards and `aria-pressed="false"`.
  - With `localStorage.getItem` throwing (simulating a restricted `file://`
    context) the report still renders 25 cards and the toggle still isolates 11.
  - On a synthetic empty-import payload (`human_reviewed_jobs = 0`), the note
    reads `Todavía no hay ninguna decisión humana importada`, references
    `jobscout import-feedback`, states the offers `no son una medida de backlog`,
    and the toggle isolates all shown offers with no fabricated count. On the real
    DB (47 imported) the note correctly shows the imported branch instead.
    The empty-import branch is also pinned by
    `test_unreviewed_view_is_honest_when_no_feedback_is_imported`.
- Static `file://` / self-containment checks on the generated HTML:
  - 0 external `<script src>` / `<link rel>` / `<img>` / `<iframe>` / `@import`.
  - Exactly 1 `<style>`, 1 `<script type="application/json">`, 1 plain `<script>`.
  - No `fetch(`, `XMLHttpRequest`, `WebSocket`, `sendBeacon`, `EventSource`,
    `import(`, or CSS `url()`.
  - 25 `http(s)` occurrences, all stored offer URLs in the embedded payload
    (`es.linkedin.com`, `www.tecnoempleo.com`), assigned to `href` at runtime only
    through the existing `safeHttpUrl` guard; none is an asset or a network call.
  - `offer.imported_review` present in the payload; `reason` never serialized;
    no `unreviewed_backlog` / `latest_cycle` key.
- `git check-ignore -v reports/jobscout-dashboard.html` →
  `.gitignore:53:/reports/	reports/jobscout-dashboard.html` (exit 0).
- Workspace status after verification is unchanged from the authoring session: the
  same five authored files plus the pre-existing notifications work-in-progress
  edits; `reports/jobscout-dashboard.html` is ignored, not tracked. No commit,
  checkout, stash, or branch operation was performed.
