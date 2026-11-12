# jobscout

A **local-first** job search toolkit: it collects public LinkedIn search results
at very low volume, normalizes and deduplicates them into SQLite, and scores each
job against a local structured profile. The default scorer reuses your already-
authenticated OpenCode CLI with the free deepseek-v4.1-flash model; two other scorers are also
available — a deterministic offline scorer and an OpenAI-compatible endpoint.

Nothing leaves your machine except the requests you explicitly authorize.
**jobscout never submits job applications.**

**Languages:** English (this file) · [Versión en español](MANUAL.md)

## What it does and does not do

| It does | It does not |
|---------|-------------|
| Collect public search cards (opt-in, bounded) | Log in, use cookies, solve CAPTCHAs, or evade anti-bot systems |
| Enrich jobs with the full description (opt-in, bounded) | Apply, contact anyone, or automate applications |
| Deduplicate and store everything in local SQLite | Upload data to any project-owned server |
| Score with heuristic, OpenAI-compatible, or OpenCode CLI | Schedule runs or send remote notifications (only manual, opt-in local notices) |
| Generate a local Spanish HTML dashboard | Write human feedback into the database on its own |

## Quick start (fixtures only, no network)

Requirements: **Python 3.11+** and `PyYAML` (only for YAML profiles; JSON
profiles need no third-party packages). The heuristic scorer and the test suite
need no network and no keys.

Editable install:

```bash
# POSIX (bash/zsh)
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Running without installing also works: prefix commands with `PYTHONPATH=src`
(POSIX) or set `$env:PYTHONPATH = "src"` once in PowerShell.

Copy the templates (`.env` and `profile.yaml` are git-ignored):

```bash
cp .env.example .env
cp profile.example.yaml profile.yaml
```

```powershell
Copy-Item .env.example .env
Copy-Item profile.example.yaml profile.yaml
```

Full end-to-end run with synthetic data:

```bash
python -m jobscout init-db
python -m jobscout ingest-fixture tests/fixtures/linkedin_search_cards_sample.json
python -m jobscout fetch-details --fixture tests/fixtures/linkedin_detail_responses.json
python -m jobscout score --all --rescore --scorer heuristic --profile profile.example.yaml
python -m jobscout rank --top 10
python -m jobscout dashboard --top 10
```

All commands run from the repository root and only write inside `data/` and
`reports/` (both git-ignored).

The `tests/fixtures/linkedin_search_sample.json` fixture already contains full
descriptions, so `fetch-details` finds nothing to enrich if you use it instead of
`linkedin_search_cards_sample.json`.

**Expected result:** `ingest-fixture` reports new and duplicate jobs; `score`
ends with `N job(s), M succeeded`; `rank` lists scored jobs; the dashboard prints
the path of the HTML file it wrote.

## Real-use safety warning

Live collection is **disabled by default** and requires `--confirm-network`.
Before using it:

- **You are responsible** for complying with the target site's terms of service
  and applicable local law.
- Volume is deliberately low: 2 pages, 40 items, ~8 s between requests (±2 s
  jitter), 20 s timeout.
- **Do not run live collection in CI** or any automated environment.
- Use `--dry-run` first: it prints the exact URLs and performs no request and no
  write.
- To test without network, use `--fixture`: it never touches the network.

## Recommended operating workflow (real use)

This is the manual, end-to-end cycle that takes you from a profile to reviewed
offers. No stage runs on its own, and **jobscout never submits an application**:
every command that touches the network or the model is one you launch.

Prepare the real profile before any sweep:

- Your local profile is **`profile.yaml`**, selected through
  `JOBSCOUT_PROFILE_PATH` in `.env` (the default value is
  `profile.example.yaml`).
- `profile.example.yaml` is **only a template**: copy it to `profile.yaml` and
  edit that copy. Do not turn the template into your real profile, and never
  commit it.

| # | Stage | Safe command | What it does | Automatic? |
|---|-------|--------------|--------------|------------|
| 0 | Prepare the profile | `cp profile.example.yaml profile.yaml` (POSIX) or `Copy-Item profile.example.yaml profile.yaml` (PowerShell), then `JOBSCOUT_PROFILE_PATH=profile.yaml` in `.env` | You set the target role, seniority, skills, dealbreakers, and exclusions. | Human decision |
| 1 | Controlled sweep | `python -m jobscout keyword-plan`, then `python -m jobscout search-linkedin --keywords "..." --dry-run`; to run it, replace `--dry-run` with `--confirm-network` | Discovers new offers. `keyword-plan` is offline; every live search is an explicit human action. | Semi-automatic: you authorize each query |
| 2 | Enrich | `python -m jobscout fetch-details --max-items 10 --confirm-network` (or `fetch-getmanfred-details` / `fetch-tecnoempleo-details` per platform, or `--fixture` to stay offline) | Fetches each offer's full description, bounded in volume. | Semi-automatic: you authorize each run |
| 3 | Score with deepseek-v4.1-flash | `python -m jobscout score --all --scorer opencode` | The default scorer (OpenCode + deepseek-v4.1-flash) evaluates only offers that have no evaluation yet. | Scoring is automatic; the apply decision is not |
| 4 | Generate the dashboard | `python -m jobscout dashboard` | Writes a local HTML report with every active offer, scores, the model's rationale, and preference flags. | Automatic and read-only |
| 5 | Review offers | Open `reports/jobscout-dashboard.html` | You mark **Aplicar** or **Descartar** in the browser. | Human decision only |
| 6 | Export/import feedback | Export the JSON from the dashboard, then run `python -m jobscout import-feedback jobscout-feedback.json` | Your decisions move from `localStorage` to the SQLite `job_feedback` table. | Import runs automatically once you invoke it |
| 7 | Calibrate | `python -m jobscout calibrate --scorer opencode` | Compares the model recommendation with your imported human decision. Read-only; it adjusts nothing. | Automatic and read-only |
| 8 | Analyze keywords | `python -m jobscout dashboard` → the *rendimiento de palabras clave* section | After several cycles, you decide which search families perform and which to drop. | Human decision |
| 9 | Notify high-value offers (opt-in) | `python -m jobscout notify` (preview) or `python -m jobscout notify --deliver` | Lists active offers at or above the threshold, with no imported human feedback and, if configured, the chosen preference outcome. It only reads SQLite; it never writes, and uses no network or model. | Manual and disabled by default |

Cycle notes:

- **It never applies automatically.** There is also no scheduler; local
  notifications are opt-in and manual (`jobscout notify`), and every run is one
  you invoke.
- `--rescore` **is not a daily step**: it re-evaluates every active offer even
  when its input has not changed. For new offers, the normal cycle is
  `score --all --scorer opencode`, which evaluates exactly the offers that have
  no score yet.
- `fetch-details` does not re-score on its own; stage 3 closes the enrichment.
- **No scoring without details:** `score --all` (with or without
  `--rescore`) drops, before `--limit`, active offers that still lack a full
  description and have detail attempts left. It prints
  `skipped N offer(s) waiting for details` and exposes `skipped_pending_details`
  in `--json`; `--job-id` forces scoring anyway. The dashboard marks them as
  "Pendiente de detalles". The exception avoids limbo: after two failed detail
  attempts that are not a 404/410, the offer is scored on its card.
- **It stops when the provider is down:** `score` ends the batch after 3
  consecutive provider errors, counted on each offer's final status (after the
  fallback retry when `JOBSCOUT_OPENCODE_FALLBACK_MODEL` is configured). A
  `parse_error` is the model's output, not the provider's, so it resets the
  streak. The failed offers keep their rows and the rest are not attempted: the
  next run picks them up. It prints `stopped after 3 consecutive provider
  errors: A of S offer(s) attempted, R left for the next run`, exposes
  `stopped_early` and `not_attempted` in `--json`, and exits with code 1 even
  when some offers were scored, so the sweep log shows a failed `score` run.
- Every evaluation is versioned: it stores the scorer's `prompt_version` and the
  profile `input_fingerprint`, so a policy change is distinguishable without
  rewriting history. For the OpenCode scorer the `prompt_version` also carries
  the effective reasoning variant as a `<version>+<variant>` suffix (default
  `+low`), so a `low` pass and a `max` pass are distinguishable after
  the fact. An explicitly empty `JOBSCOUT_OPENCODE_VARIANT=` keeps the bare
  historical string. The model is part of the input fingerprint, so changing it
  re-scores offers gradually.
- Re-evaluation **does look at the input fingerprint**: `score --all` re-evaluates
  an offer when its fingerprint changes (posting, profile, prompt, or model) and
  skips the ones that already have a successful evaluation with the current
  fingerprint. Offers that never had a successful evaluation come first, then
  the ones that only need a re-score, each group newest first; so a model,
  prompt or profile change cannot push a sweep's new offers past the `--limit`
  cap. Use `--rescore` to force a fresh pass over every active offer.
- `list`, `rank` and the dashboard **flag out-of-date scores**: an offer whose
  shown score comes from a scorer that no longer has any successful evaluation
  with the current fingerprint (the model, prompt, profile or posting text
  changed) shows `[needs re-score]` in text, `needs_rescore: true` in `--json`
  and "Re-score pendiente" next to the dashboard score. It is the same
  comparison `score --all` uses, so the flag clears once the next run re-scores
  the offer. If the profile cannot be read, `list` still works without the flag
  (`needs_rescore: null`).
- **Every offer has a stage** (`stage`), derived in one place
  (`jobscout/stage.py`) from the stored rows, never stored itself:
  `inactive` → `pending_details` → `ready_to_score` → `scored` →
  `needs_rescore`. A shown score wins over missing details, and a failed
  scoring is a mark (`score_failed`) on `ready_to_score`. `list --json` and
  `rank --json` include it on every offer, and the dashboard (header, rows and
  summary) reads it from there. Without a profile, a scored offer stays `scored`.

### Seniority and technology criterion (prompt v5)

The OpenCode prompt is versioned as **`opencode-v5-seniority-aware`**. The policy
deepseek-v4.1-flash applies is:

- **Strong penalty, with `skip` when it defines the role:** 5 or more required
  years of experience, explicit `senior`, `staff`, `principal`, `lead`, or
  `manager` titles, and leadership or architectural ownership as a core duty. A
  passing mention of "senior" in an otherwise junior or stretch posting is not a
  hard skip.
- **It does not block on one or two missing technologies:** if the main role is
  still Cloud / DevOps / DevSecOps / Platform, those absences are treated as
  learnable gaps.
- **It does block on the wrong core stack:** if most of the required core stack
  does not match your profile, that is a material mismatch and the recommendation
  and score drop a lot.
- **It separates hard requirements from preferences:** a reasonable junior or
  stretch role tends toward `consider` or `apply` even when it asks for more
  experience than you have, except for the strong thresholds above.
- **It keeps the dealbreakers** and the SOC/rotating-shift exclusions, which are
  additionally computed separately in the **preference filters** (offline, without
  touching the score or the stored evaluation).

The output format is unchanged: a single JSON object with `overall_score`,
`recommendation`, `confidence`, and `rationale`.

## Local dashboard

`dashboard` reads the existing SQLite database and writes **one** self-contained
HTML file (embedded CSS, JavaScript, and data). It uses no CDN, requests no
external asset, needs no server, and makes no network request. All visible text
is neutral/professional Spanish; internal schema keys and CLI command names stay
English so the report never drifts from the database. The sticky header
summarises the last sweep (date, new offers, pending details and failures;
unscored new offers and failures show in red).
"Detalles pendientes" counts offers, not postings, each under the provider that
first saw it, so it adds up to the rows marked "Pendiente de detalles". It offers two tabs, **Ofertas** and **Palabras clave**, with **Exportar N
decisiones** on the right, shown only while this browser holds a decision. The offers view uses a single horizontal toolbar
with text search, the model recommendation, **Solo nuevas** (offers first seen
in the latest sweep) and a minimum-score select (`Score ≥ 0`, `≥ 50`, `≥ 70`);
there is no side rail and no KPI cards. The typefaces (Charis SIL, Inter and DM Mono,
OFL-licensed, in `src/jobscout/fonts/`) are embedded as `data:` URIs too, so the file looks the same from `file://` without installed fonts or
any network request.

Each offer is **a one-line row**: score with a bar coloured by band (≥ 70 / ≥ 50),
title, company and location, chips (recommendation, `nueva`, the preference's
`⚠ flag` or `veto`), salary with its provenance (`indicado` or `sin dato`; a
number is never invented) and age. A failed evaluation shows as `sin evaluar
(error)`, never as 0. Rows are painted 50 at a time (**Mostrar 50 más**) and the
detail is built only when a row is opened: the model's full rationale (no longer
truncated), the stated salary, Puntos fuertes / Carencias / Bloqueos / Incógnitas when the
evaluation has them, and a footer with model, confidence, date, the imported
decision, the **✓ aplicar** / **✕ descartar** buttons (the reason field, up to 500
characters, opens after choosing) and **Ver la oferta ↗**, which opens the most recently seen live listing (same provider first), so a repost never sends you to the old, closed ad. Report warnings are quiet lines at the end, next
to the metadata.

A stated salary is shown in green with the `indicado` label. When the posting
does not state one, the row shows the model's estimate in grey, prefixed with
`≈` and labelled `estimado`: it is an indicative gross annual figure for Spain,
informational only, and it never changes the score or the recommendation.

```bash
# POSIX
python -m jobscout dashboard --output reports/jobscout-dashboard.html
xdg-open reports/jobscout-dashboard.html   # Linux
open reports/jobscout-dashboard.html       # macOS
```

```powershell
# Windows PowerShell
python -m jobscout dashboard --output reports\jobscout-dashboard.html
Start-Process reports\jobscout-dashboard.html
```

- Default output path: `reports/jobscout-dashboard.html` (git-ignored). By default
  the report embeds **every active offer**; `--top N` bounds it to the best `N`
  and keeps the same explicit cap as before. Deactivated offers stay in SQLite but
  are excluded by `is_active = 1`. Forward slashes also work on Windows:
  `--output reports/dashboard.html`.
- The report never embeds the job description, raw payloads, the profile,
  contact data, API keys, or absolute local paths.
- It opens the database with `PRAGMA query_only`: generating the report cannot
  insert, update, or delete a row.
- A missing database is an error; an empty or unscored one still produces a
  report with a visible warning explaining why it is empty.

### Model recommendation vs. human feedback

These are two different signals, and the report keeps them apart:

| | Model recommendation | Human feedback |
|---|---|---|
| Produced by | `score` (heuristic, LLM, or OpenCode) | you, clicking in the report |
| Stored in | SQLite `evaluations` table, with full provenance | this browser's `localStorage`, keyed by project + job id; then SQLite `job_feedback` |
| Read by `jobscout` | yes | only by `import-feedback`, which copies an export into `job_feedback` |
| Portable | yes, it is in the database | with **Exportar N decisiones** plus `import-feedback` |

Each job has **two** local feedback buttons (**Aplicar / Descartar**, stored as
`apply` / `reject`) and an optional reason box of up to 500 characters (the field
accepts nothing more at the cap). The active decision is binary:
*Aplicar* or *Descartar*. The buttons themselves are **browser-local**: they do
not apply, do not touch LinkedIn, and write nothing to SQLite. **Exportar N
decisiones** downloads `jobscout-feedback.json` (`jobscout.feedback.v1` schema). N
counts the decisions stored in this browser (a legacy `consider` included), not the
imported ones. After `import-feedback`, regenerating the report (the sweep does
it) and reopening it makes the browser forget the local decisions the database
already holds with the same value and date, so **Exportar N decisiones**
disappears; a decision edited after the export, or not yet imported, stays and
keeps the button visible. Over an imported decision, pressing the same button
again does not clear it (the import never deletes decisions from the database):
it can only be switched to the other one. Opening the report also drops empty
local marks that were hiding an imported decision.

Historical compatibility: `consider` decisions (the old *Revisar* label) are not
deleted or silently converted. An offer with a stored `consider` shows it as a
**historical (legacy) decision** and lets you replace it with Aplicar or
Descartar; the export keeps the `v1` schema and the legacy value verbatim, and the
importer still accepts it. The model recommendation keeps its own vocabulary
(`apply` / `consider` / `skip` / `need_more_info`), separate from your decision.

Import that export to make the decisions durable:

```bash
python -m jobscout import-feedback jobscout-feedback.json
```

- Writes one row per job in `job_feedback`, next to — never instead of — the
  model recommendation in `evaluations`.
- Validates the envelope first: a foreign schema, a missing `project_id`, or a
  `project_id` that does not match this database fails the command and writes
  nothing. `--project-id ID` accepts the export's own id explicitly, for a
  database that moved.
- Rejects individual entries (unknown job id, unknown decision, malformed
  timestamp) with a message on stderr while the valid entries are still
  imported, like `score --job-id` does for a missing job.
- Idempotent: re-importing the same file changes nothing, and an entry older than
  the stored decision never overwrites it. An entry with no decision (the
  dashboard's "not reviewed" state) is skipped and never deletes a stored
  decision.

There is still no automatic scheduling: importing is a command you run yourself.

### Imported decisions

Human decisions imported with `import-feedback` appear in the footer of each
opened offer, but the dashboard no longer offers a filter that isolates offers
without a decision, so the toolbar fits on one line. The review backlog is queried in SQLite, not invented as an HTML counter.

- The imported decision is read with a single read-only `SELECT` over the
  `mode=ro` connection and is a separate axis from the local `localStorage`
  marks. The header counter only reflects browser decisions and is never merged
  with the imported feedback.
- An imported decision's free-text reason **never** enters the HTML.
- With no imported decisions, the footer simply omits that line.

**Explicit limitation:** `search_runs.cycle_id` is nullable and free-form (a
human convention), and runs from different platforms or cycles are not guaranteed
to be comparable. Today there is therefore **no** "new in the latest cycle" view:
it cannot be computed trustworthily and is not approximated.

### Title-based seniority pre-filter

Offers whose title is clearly senior (`senior`, `sr`, `lead`, `principal`,
`architect`, `arquitecto`, `arquitecta`, `manager`, `head`, `director`, `staff`,
`jefe`) are marked as filtered at read time, with no schema change. Junior
exceptions (`junior`, `jr`, `trainee`, `beca`, `becario`, `becaria`,
`prácticas`/`practicas`, `intern`, `internship`) cancel the filter. `score` skips
them before applying `--limit` and prints `skipped N offer(s) filtered by
seniority`; `--job-id` scores them anyway. The dashboard keeps every active
offer (`jobs_shown == len(offers)`) but hides them by default, shows the
"N ocultas por seniority" count and a `#filter-show-seniority` toggle to reveal
them; resetting the filters hides them again. A revealed offer shows the reason
in its row. Nothing is deleted or deactivated.

Once feedback is imported, `calibrate` produces the evidence of how well the
model agrees with you:

```bash
python -m jobscout calibrate                    # each job's latest valid evaluation vs. feedback
python -m jobscout calibrate --scorer opencode  # only evaluations from that scorer
python -m jobscout calibrate --json             # machine-readable report
```

`calibrate` is **read-only and offline**: it opens the database read-only
(`mode=ro`, `PRAGMA query_only`), issues only `SELECT` statements, and never
creates the database. It never adjusts prompts, weights, or scores — it only
produces evidence. It compares the model recommendation with the human decision
through one explicit mapping:

| Model | Human |
|-------|-------|
| `apply` | `apply` |
| `consider` | `consider` (legacy) |
| `skip` | `reject` |

`consider` is **legacy**: the binary dashboard no longer offers it, but a
historical row is still counted and the report flags it as legacy
(`legacy_total` / `"legacy": true` on each sample) instead of dropping it.

`need_more_info` is **not** part of the three-way agreement: it is reported
separately as an unresolved category and never silently becomes `skip`. The
report shows comparable samples, exact agreement, the confusion matrix,
unresolved counts, and the `overall_score` distribution by human decision. A
decision with no valid evaluation (`status = 'ok'`) is counted separately and
stays out of the agreement math.

### Preference filters (exclusions)

Next to the professional-fit score, every offer is checked against the user's
**explicit exclusions**. This is a separate axis: it never edits the score, the
recommendation, or the stored evaluation, and it is recomputed when the offer is
read instead of being persisted as a second versioned verdict. Classification is
deterministic and **offline**; there is no model call.

| Outcome | Meaning |
|---------|---------|
| `pass` | No exclusion cue was found. |
| `flag` | Ambiguous match (for example "turnos" without saying they rotate, or a SOC role with no operational evidence); surfaced for human review. |
| `fail` | Clear match with an exclusion: rotating shifts, or an operational SOC role. |

Implemented exclusions:

- **Rotating shifts** in Spanish and English (`turnos rotativos`, `rotación de
  turnos`, `rotating shifts`, `shift rotation`, `24/7 shifts`). A `24/7` mention
  without shifts is only `flag`, and a flexible schedule or occasional on-call
  never produces `fail`.
- **Operational SOC** only when the term implies monitoring, operations,
  on-call/shifts, or continuous surveillance (for example `SOC Analyst` with
  `24/7`, `SIEM monitoring`, `incident monitoring`). A generic SOC mention, a
  knowledge requirement, or `SOC 2` (compliance) stay eligible; a SOC role with
  no operational evidence is `flag`.

In the report, the row carries a `⚠ flag` chip (ochre) or a `veto` chip (red,
since it is a blocker); when opened, the reason and evidence are the first item of
Incógnitas (`flag`) or Bloqueos (`fail`). The summary counts excluded vs. flagged
offers. The CLI exposes the same verdict in `list`, `rank`, and `score` (a
`[preference=...]` marker in text and a `preference` object in `--json`).

## CLI reference

Global options: `--db PATH`, `--profile PATH`, `--json` (before or after the
subcommand), and `--version`.

| Command | Purpose |
|---------|---------|
| `init-db` | Create or verify the schema. Idempotent. |
| `ingest-fixture PATH [--source S] [--label L]` | Normalize, deduplicate, and store a JSON fixture (offline). |
| `search-linkedin --keywords K [--location L] [--distance N] [--remote] [--employment-type T] [--max-pages N] [--max-items N] [--cycle-id ID] [--dry-run \| --confirm-network]` | Bounded public search. Disabled without `--confirm-network`. |
| `search-getmanfred [--keywords K] [--max-age-days N] [--max-documents N] [--max-items N] [--cycle-id ID] [--label L] [--dry-run \| --confirm-network]` | Public GetManfred sitemap inventory. `--keywords` applies a **local** title/slug filter (no fan-out, no private endpoint). `--max-age-days` skips entries whose `lastmod` is older than N days (default 120; `0` = no cut). No ranking. Disabled without `--confirm-network`. |
| `search-tecnoempleo (--keywords K [--location L] \| --browse) [--max-pages N] [--max-items N] [--cycle-id ID] [--label L] [--dry-run \| --confirm-network]` | Bounded Tecnoempleo public server-rendered search. `--browse` walks Madrid offers by professional function; it is mutually exclusive with `--keywords` and `--location`. Enforces `robots.txt` at run time. Disabled without `--confirm-network`. |
| `keyword-plan [--family ID…] [--location L] [--remote]` | List the keyword families and the exact command per query (no network, no database). |
| `fetch-details [--job-id ID… \| --all \| --refresh [--stale-days N]] [--max-items N] [--dry-run \| --fixture PATH \| --confirm-network] [--label L]` | Enrich LinkedIn jobs with the full description. Defaults to jobs that are not yet enriched; `--refresh` revisits already-fetched offers with stale or retryable evidence. |
| `backfill-salary [--dry-run]` | Fill the salary of already-enriched LinkedIn and Tecnoempleo offers from their description or the stored detail page (one-off, offline). |
| `fetch-tecnoempleo-details [--job-id ID… \| --all] [--max-items N] [--dry-run \| --confirm-network] [--label L]` | Enrich Tecnoempleo offers from their public detail page, including the salary when the page publishes it. Re-reads `robots.txt`; no login or cookies. Disabled without `--confirm-network`. |
| `fetch-getmanfred-details [--job-id ID… \| --all] [--max-items N] [--dry-run \| --confirm-network] [--robots PATH] [--label L]` | Enrich GetManfred offers from the recognized `JobPosting` JSON-LD on their public page. Re-reads `robots.txt`; no login or cookies. Disabled without `--confirm-network`. |
| `list [--limit N] [--min-score S] [--include-inactive]` | Recent jobs with their latest score; `[needs re-score]` flags out-of-date scores. |
| `rank [--top N] [--min-score S]` | Jobs with a successful score, best first; same `[needs re-score]` flag. |
| `score (--job-id ID… \| --all) [--scorer heuristic\|llm\|opencode] [--scorer-model MODEL] [--effort EFFORT] [--limit N] [--rescore]` | Evaluate jobs and print the explainable result. `--all` skips senior titles and offers waiting for details before `--limit`; `--job-id` forces both. |
| `dossier (--job-id ID… \| --apply) [--refresh] [--timeout S]` | Research each offer's company with OpenCode and write one Markdown dossier per company under `data/dossiers/`. Never fetches or cites LinkedIn. |
| `runs [--limit N]` | Recent search runs and stored counts. |
| `dashboard [--output PATH] [--top N]` | Write a self-contained local HTML report (read-only, offline). |
| `import-feedback PATH [--project-id ID]` | Store a dashboard feedback export in SQLite (offline, idempotent). |
| `calibrate [--scorer heuristic\|llm\|opencode]` | Compare each job's latest valid evaluation with imported human feedback (read-only, offline). |
| `deactivate-closed [--apply]` | List active LinkedIn/GetManfred offers whose stored detail page says they are closed (offline, read-only); `--apply` deactivates them and keeps their history. |
| `prune [--apply]` | Plan the worst quarter of scored offers and provider-specific keyword series (read-only preview; `--apply` deactivates only the selected offers). |
| `notify [--deliver] [--min-score S]` | Active offers at or above the score threshold, opt-in and read-only (see "Local notifications"). |

Exit codes: `0` success, `1` error, `2` refusal (collector disabled, access
denied, or a request that was intentionally not performed).

Inspect a plan without running it:

```bash
python -m jobscout search-linkedin --keywords "senior backend engineer" --location "Buenos Aires, Argentina" --max-pages 2 --dry-run
```

Other public sources (opt-in, all disabled by default):

```bash
python -m jobscout search-getmanfred --dry-run                       # plan, offline: reads the public sitemap
python -m jobscout search-getmanfred --max-items 40 --confirm-network
python -m jobscout search-getmanfred --keywords "Platform Engineer" --max-items 40 --confirm-network  # local filter
python -m jobscout search-tecnoempleo --keywords "cloud security" --location madrid --dry-run
python -m jobscout search-tecnoempleo --keywords "cloud security" --max-pages 2 --confirm-network
python -m jobscout search-tecnoempleo --browse --dry-run                 # function browse, Madrid
python -m jobscout search-tecnoempleo --browse --max-pages 5 --max-items 150 --confirm-network
```

`search-getmanfred` is an **inventory**, not a search: it does no fan-out, and
records every observed position as `position_kind = none` with rank `0`. The title
is derived from the public URL slug and the company is left unknown because the
sitemap does not expose it. Postings stay `partial`.

GetManfred exposes no keyword search endpoint. `--keywords` applies the term
**locally** to the sitemap in a single download: only offers whose normalized
title/slug contains every token of the term are kept (accent- and case-insensitive,
token matching, never substring: `front` does not match `frontend`). There is no
private endpoint and no per-keyword request. Each kept offer records the approved
keywords it matched in `extra['matched_keywords']`, so one offer observed under one
or several keywords never mixes terms or platforms.

The filter is **exact token matching**: every token of the approved term must
appear as a whole token in the normalized title/slug. There are no synonyms,
aliases, or family variants, so `DevOps` does not match `DevSecOps`, `Cloud` does
not match `Cloud Security`, and a reordered variant does not widen the result.
The candidate variants observed in the sitemap are **DevOps, DevSecOps, Cloud
Engineer, Cloud Security, Azure**, and possibly **SRE**; adding them to the
approved catalog is a **separate decision** (a human catalog review), not
automatic behavior of this adapter.

The sweep does not send the catalog queries to GetManfred: it uses the provider's
own short terms, published by `keyword-plan --json` in the `getmanfred` block
(`devops`, `devsecops`, `sre`, `cloud`, …). Sitemap slugs are short
(`fcm-sre-devops-engineer-jul26`) and a catalog query requires **every** one of
its tokens, so it almost never matches; with those terms any single one keeps the
offer. The sitemap also keeps offers from years ago, so `--max-age-days` (default
120; `0` = no cut) drops entries whose `lastmod` is older than that before
`--max-items` is counted. An entry with no `lastmod` or an unparseable one is
kept: an unknown age is not evidence of a stale offer.

GetManfred is no longer a **title-only inventory**: prior written permission from
Manfred covers automated requests and the storage of offer descriptions in this
project. The technical gate is also satisfied: the offer page publishes a
`JobPosting` JSON-LD block in its static HTML, so no JavaScript rendering is
required. A detail adapter exists (`fetch-getmanfred-details`), with a fixture
built from one real capture, and neither the terms text nor the offer-page
content is reproduced here. The adapter still re-fetches `robots.txt` before the
first request: if that policy changes, the run fails closed instead of relying
only on today's permission.

`search-tecnoempleo` uses only the public server-rendered search under
`/ofertas-trabajo/`. In text mode it builds the URL with the keyword as the `te`
query parameter, the location as a path segment, and `pagina` only from the
second page onward. In `--browse` mode it walks Madrid offers by professional
function (`pr=,263,&es=,34,43,40,39,8,45,19,`: DevOps, Técnico de Sistemas,
Ciberseguridad, Redes, Soporte Técnico, Administrador, and Helpdesk), with the
commas literal because that is how the live URL was verified; the provenance
label stored as `query` is `función: sistemas, devops y seguridad (Madrid)`. It
re-reads `robots.txt` before the first request and fails closed when the path is
disallowed. It uses no login, cookies, private RSS, or unauthorized API. The URL
shape was verified against live site pages; the parser remains conservative and
fails closed when the HTML is not recognizable.

Provenance convention: `provider` (platform), `query` (keyword), `cycle_id`
(comparable search cycle), and `position_kind` (`search_rank` = real search
position; `none` = inventory with no order). The dashboard keyword analytics
identify each series by `provider|keyword`: the cycle is provenance, not identity,
so offers for the same `provider`/`keyword` seen across several cycles aggregate
into one row and a shared filter key, while per-offer provenance keeps its actual
`cycle_id` in SQLite. The dashboard embeds only each offer's distinct series keys
(what the keyword filter reads), not one entry per sighting. The same term on LinkedIn, GetManfred, and Tecnoempleo is never
merged. Cycles whose ID starts with `smoke` (trimmed, case-insensitive) are
excluded from the analytics table; no rows or per-offer provenance are deleted.

InfoJobs stays **blocked**: `https://www.infojobs.net/candidate/my-infojobs.xhtml`
is a private, authenticated page and may only be added after an official API
authorization and explicit app credentials. There is no CLI command and no
network path.

Detail enrichment:

```bash
python -m jobscout fetch-details --dry-run                                            # plan only, no network or writes
python -m jobscout fetch-details --fixture tests/fixtures/linkedin_detail_responses.json
python -m jobscout fetch-details --max-items 5 --confirm-network                      # live, bounded
python -m jobscout fetch-details --all --max-items 10 --confirm-network               # re-fetch everything
python -m jobscout fetch-details --refresh --stale-days 14 --dry-run                  # refresh plan, offline
python -m jobscout backfill-salary --dry-run                                          # already-enriched salaries, offline
```

When a LinkedIn detail description states a euro salary, `fetch-details` fills
`salary` with that text. On Tecnoempleo, `fetch-tecnoempleo-details` picks, in
order: the range (two figures) the description gives; otherwise the page's own
"Salario" field, a rounded band the employer picks; and when there is no field
either, the description's single figure ("hasta 45.000 €"). `backfill-salary
[--dry-run]` does the same once, offline, for already-enriched offers that still
have no salary —on Tecnoempleo it reads the field from the stored detail page—
and never overwrites an existing value.

Every `fetch-details` selection route — the default, `--all`, `--refresh`, and an
explicit `--job-id` — is restricted to LinkedIn offers (`source = linkedin`). A
non-LinkedIn id is reported and never planned, so a GetManfred or Tecnoempleo
numeric id can never be read inside LinkedIn's id space.

The default route (and Tecnoempleo's and GetManfred's) skips reposts: when a
provider republishes the same offer under a new id, it is stored as another
variant of the same offer, and it is not fetched again when the offer already
has a full description whose latest successful detail came from that same
provider. A variant from another provider is still fetched.

A closed offer counts as gone even though its page answers `200`: LinkedIn marks
it "No longer accepting applications" and GetManfred says `status: CLOSED` in
the page data. The detail is still stored, the attempt's note reads `offer
closed on the provider`, and the variant is deactivated as for a `404`/`410`
(the offer stays active while another variant, such as a repost, is live).
Unlike a `404`, closure is final: a later re-sighting (GetManfred's sitemap keeps
listing closed offers) does not reactivate it. `deactivate-closed` applies the same rule offline to pages already stored:
without `--apply` it only lists them.

`--refresh` is mutually exclusive with `--job-id` and `--all`. It only considers
active offers that already have fetch history and whose latest attempt is one of:

- an `ok`/`partial` fetch older than the cutoff (`--stale-days`, default `0`);
- a `failed` attempt that never received an HTTP response (`http_status` and
  `raw_document_id` both null), i.e. a retryable transient failure;
- a `404`/`410` refuted by a later re-sighting.

Inactive offers, never-fetched offers, `skipped` attempts, and failures that did
record an HTTP response stay out. With `--stale-days 0` there is no age floor.
The `--max-items` budget is spent on the oldest evidence first.

`--refresh --dry-run` is read-only and offline: it never creates the database or
writes a row. A real refresh keeps the same network gates as the rest of
`fetch-details` (`--confirm-network`, or `--fixture` to stay offline).

`fetch-details` does not re-score on its own: run `score --all --rescore` after
enriching.

Tecnoempleo detail enrichment:

```bash
python -m jobscout fetch-tecnoempleo-details --dry-run                        # plan, no network or writes
python -m jobscout fetch-tecnoempleo-details --max-items 10 --confirm-network # live, bounded
python -m jobscout fetch-tecnoempleo-details --all --max-items 20 --confirm-network
```

It defaults to **active Tecnoempleo offers** that still lack a full description
(`--all` widens to every active offer from that platform). It reuses the same
safety model as the search: no login, cookies, private RSS, or API; it re-reads
`robots.txt` and fails closed when the detail path is disallowed; conservative
limits, pacing, and download ceiling.

It extracts description, company, location, date, technologies, and salary
**only from recognized markup** (`itemprop`/stable classes; the date prefers
`<time datetime>`, and the salary follows the order above: description range,
page "Salario" field, single description figure). A recognized detail page leaves the offer at
`content_status = ok` with a non-empty description. An unrecognized page, a 403,
or a truncated body stays `partial`/`failed` with an honest error: `ok` is never
invented. The pipeline applies the rule that a partial or failed attempt never
replaces a successful full description.

Full flow: **search -> detail enrichment -> scoring**. `fetch-tecnoempleo-details`
does not re-score on its own; run `score --all --rescore` (or `score --job-id ID`)
after enriching.

GetManfred detail enrichment:

```bash
python -m jobscout fetch-getmanfred-details --dry-run                        # plan, no network or writes
python -m jobscout fetch-getmanfred-details --max-items 10 --confirm-network # live, bounded
python -m jobscout fetch-getmanfred-details --all --max-items 20 --confirm-network
```

Prior written permission from Manfred covers automated requests and the storage
of offer descriptions in this project. It defaults to **active GetManfred offers**
that still lack a full description (`--all` widens to every active offer from that
platform; every route — default, `--all`, and `--job-id` — is scoped to
`source = getmanfred`). It reuses the same safety model as the search: no login,
cookies, private RSS, or API; it re-reads `robots.txt` and fails closed when the
detail path is disallowed; conservative limits, pacing, and download ceiling.

It extracts description, company, location, date, employment type, and salary
**only from the recognized `JobPosting` JSON-LD** on the page. A field the page
does not expose stays `NULL`: the company is never derived from the slug, and an
empty locality is never replaced by an inferred value. A recognized detail page
leaves the offer at `content_status = ok` with a non-empty description. An
unrecognized page, a 403, or a truncated body stays `partial`/`failed` with an
honest error: `ok` is never invented. The pipeline applies the rule that a partial
or failed attempt never replaces a successful full description.

`fetch-getmanfred-details` does not re-score on its own; run
`score --all --rescore` (or `score --job-id ID`) after enriching.

Feedback import:

```bash
python -m jobscout import-feedback jobscout-feedback.json            # import this database's export
python -m jobscout import-feedback jobscout-feedback.json --json     # machine-readable accounting
python -m jobscout import-feedback old-export.json --project-id ID   # accept a moved database's export
```

Calibration report (offline, read-only):

```bash
python -m jobscout calibrate                          # all imported feedback
python -m jobscout calibrate --scorer heuristic       # only one scorer's evaluations
python -m jobscout calibrate --json                   # machine-readable report
```

### Company dossiers (OpenCode, on demand)

`dossier` asks OpenCode (the same isolated invocation as the scorer) to research
an offer's company with `webfetch` and writes one Markdown file per company under
`<database directory>/dossiers/<slug>.md`. It spends OpenCode quota and network
through the model; the periodic sweep does not run it.

```bash
python -m jobscout dossier --job-id 42                       # one offer
python -m jobscout dossier --apply                           # every active offer with an 'apply' decision
python -m jobscout dossier --apply --refresh                 # regenerate existing dossiers
python -m jobscout dossier --job-id 42 --timeout 900         # more time per company (default 600 s)
```

Exactly **one dossier per company** is written: when several offers share a
company, the first selected one wins. An existing dossier is skipped unless
`--refresh` is passed. Offers without a company are skipped with a warning, and
one company failing does not stop the others (the command exits 1 if any
failed). The model is forbidden from fetching or citing `linkedin.com`; any
line that still mentions it is removed before saving and the count is reported.
Only the final answer is kept, from its last `## Sector` heading (the OpenCode
stream also carries the fetched pages); an answer without that section counts as
a failure and nothing is written. Because that stream includes the fetched
pages, `dossier` raises the output limit to at least 8 MiB
(`JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES` only counts when larger); the scorer keeps
its 1 MiB. The dashboard embeds the dossier text in each offer of that company (inside a
collapsible block), with no external requests or file links: the report stays
self-contained and safe over `file://`.

### Local notifications (opt-in, no scheduling)

`notify` is a manual, read-only preview of high-value offers. It is **disabled
by default** (`JOBSCOUT_NOTIFY_ENABLED=false`) and, while disabled, it neither
creates nor opens the database.

```bash
# Preview (prints only; never sends anything)
python -m jobscout notify
python -m jobscout notify --limit 10 --json

# Explicit local delivery (opt-in); stdout by default
python -m jobscout notify --deliver

# Only the offers the preference filter marks for review
JOBSCOUT_NOTIFY_PREFERENCE_OUTCOME=flag python -m jobscout notify
```

It selects **active** offers that have a stored successful evaluation with a
score ≥ `JOBSCOUT_NOTIFY_SCORE_THRESHOLD` (0–100, default 80), that optionally
match `JOBSCOUT_NOTIFY_PREFERENCE_OUTCOME` (`pass`/`flag`/`fail`, empty = no
filter), and that have **no imported human feedback row at all**. It reuses the
stored evaluation and preference assessment: it never re-scores, never calls a
model, and makes no network request. `--limit` reuses the existing 250 cap.

Delivery is a separate action: without `--deliver` it only prints. With
`--deliver` it uses `JOBSCOUT_NOTIFY_DELIVERY` (`stdout` by default, or
`system`). `system` uses only an already-installed local notifier (`notify-send`
on Linux or `terminal-notifier` on macOS) discovered with `shutil.which`; when
none exists it falls back to stdout. **There is no scheduler, daemon, cron,
background service, automatic run, remote delivery, or application.**

### Catalog pruning (offline, preview by default)

`prune` is a deterministic, offline plan for the worst quarter of the catalog. It
uses only metrics jobscout already stores, adds no composite score, and changes
nothing unless you ask it to.

```bash
# Preview: read-only, never creates or writes the database
python -m jobscout prune
python -m jobscout prune --json

# Explicit action: deactivate only the offers the plan selected
python -m jobscout prune --apply
```

What the plan reports:

| Part | Rule |
|------|------|
| Offer cut | Active offers with a successful `latest_evaluations.overall_score`, ranked ascending, `floor(25% x count)` selected, ties broken by ascending job id. Unscored and inactive offers are never selected. |
| Keyword candidates | Per **provider** series (`provider` + literal query) ranked by ascending `avg_score`, `floor(25% x eligible series)` per provider, ties broken by query. A provider with fewer than four eligible series is not pruned. |
| Unique-offer cost | For each worst-quarter series, `unique_offers` counts the active offers **no other query of the same provider** surfaced, and `unique_good_offers` counts those scoring at least `45`. |
| Context columns | `avg_score`, `top5_avg_score`, `top10_avg_score`, `duplicate_rate`, and `human_apply_rate`, shown as-is. A missing human approval rate reads as unavailable, never `0`. |

Safety guarantees:

- **Preview is non-mutating.** The default opens the existing database read-only
  (SQLite `mode=ro` plus `PRAGMA query_only`) and writes no row; a missing
  database is a clear error, not a new file.
- **`--apply` is bounded and history-preserving.** It deactivates exactly the
  selected offers through the existing `deactivate_job` path, so evaluations,
  feedback, and fetch records stay. It never deletes a row and never creates a
  database.
- **Provider identity is preserved.** Cutting `query|tecnoempleo` can never touch
  `query|linkedin`.
- **Never cost a good offer (owner rule).** A series that alone surfaced an offer
  scoring `>= 45` is not proposed: it moves to `held_back_keywords` (text and
  JSON) with its measured `unique_offers`/`unique_good_offers` and the reason.
- **Recommendation only.** Keyword candidates are never applied: the keyword
  catalog is not mutated and no future query is suppressed automatically. No
  cycle-count gate exists.

## Scoring

All three scorers return the same explainable shape: `overall_score` (0-100),
`dimensions`, `blockers`, `strengths`, `gaps`, `unknowns`, `confidence` (0-1),
`recommendation` (`apply` / `consider` / `skip` / `need_more_info`), and
`rationale`. Every evaluation also stores the scorer, model, prompt version,
profile snapshot id, an `input_fingerprint`, and the raw document id, so each
score is auditable after the fact.

Before scoring, offers with a clearly senior title are skipped by the title-based
seniority pre-filter: `score --all` drops them before applying `--limit`, prints
`skipped N offer(s) filtered by seniority`, and exposes `skipped_seniority` in
`--json`. `--job-id` scores them anyway, and existing evaluations of filtered
offers stay untouched.

A search card is never scored on its own: `score --all` (with or without
`--rescore`) also drops, before `--limit`, active offers that still lack a full
description and have detail attempts left. It prints
`skipped N offer(s) waiting for details` and exposes `skipped_pending_details`
in `--json`; `--job-id` forces scoring anyway. After two failed detail attempts
that are not a 404/410 the offer is scored on its card, so it never stays in
limbo.

The batch also stops when the provider is down: after 3 consecutive provider
errors (each offer's final status, after the fallback retry) it prints
`stopped after 3 consecutive provider errors: A of S offer(s) attempted, R left
for the next run`, exposes `stopped_early` and `not_attempted` in `--json`, and
exits with code 1 even when some offers were scored. The error rows already
written stay, and the remaining offers are left for the next run. A
`parse_error` does not count: it is the model's output, not the provider's.

| Scorer | When to use it | Requirements |
|--------|----------------|--------------|
| `heuristic` | Always available, deterministic, dependency-free | none |
| `llm` | Any OpenAI-compatible `/chat/completions` endpoint | `JOBSCOUT_LLM_BASE_URL` and `JOBSCOUT_LLM_API_KEY` |
| `opencode` (default) | You already use OpenCode locally and prefer not to manage a key | an installed, authenticated `opencode` CLI |

### Heuristic (offline)

Deterministic and dependency-free. Dimensions: `must_have_skills`, `skills`
(nice-to-have), `seniority`, `location`, `language`, `recency`, and
`compensation`. Weights come from the profile and are renormalized over the
dimensions that apply.

### OpenAI-compatible LLM

```bash
export JOBSCOUT_LLM_BASE_URL=https://api.example.com/v1
export JOBSCOUT_LLM_API_KEY=...
python -m jobscout score --all --scorer llm --profile profile.yaml
```

Without `JOBSCOUT_LLM_BASE_URL` and `JOBSCOUT_LLM_API_KEY`, the LLM scorer refuses
to run and points back at the heuristic scorer. The default model is
`deepseek-v4.1-flash`, always configurable through `JOBSCOUT_LLM_MODEL`.
Responses are parsed defensively; unparseable output is recorded as `parse_error`
instead of aborting the batch.

### OpenCode CLI (no API key)

```bash
# One-time: authenticate OpenCode with the provider that serves the model
opencode auth login

# Default included model: opencode-go/deepseek-v4.1-flash
python -m jobscout score --all --scorer opencode --profile profile.yaml
```

When `--scorer` is omitted, OpenCode with deepseek-v4.1-flash is used. For an explicit offline
evaluation, use `--scorer heuristic`.

It runs `opencode run --standalone --model <model>#<variant> --format json
<prompt>` as a subprocess argument array — never through a shell. OpenCode 2.x
removed `--pure` and `--variant`, so isolation lives in `--standalone` and the
variant in the model id itself. With `JOBSCOUT_OPENCODE_PURE=true` (the default)
it also uses a private `XDG_CONFIG_HOME` at
`<database folder>/opencode-config` and disables project config, autoupdate, and
Claude Code skills; isolation is done through the environment because OpenCode
plugins are arbitrary code. The private `opencode.json` denies the actions that
can change the machine or hang a non-interactive run (`shell`, `write`, `edit`,
`execute`, `subagent`, `skill`, `question`, `websearch`, `external_directory`,
`doom_loop`) and reading secret files (`*.env`, `*.env.*`, `*.pem`, `*.key`,
`*auth.json`), so the model keeps only `read`, `grep`, `glob`, and `webfetch`;
an outdated config file is rewritten automatically on the next run. By default
the CLI runs from a private, empty directory next to the database
(`<database folder>/opencode-work`), not the shared temp directory;
`JOBSCOUT_OPENCODE_WORKDIR` sets another path.

The variant is set with `JOBSCOUT_OPENCODE_VARIANT` (default `low`).
`JOBSCOUT_OPENCODE_FALLBACK_MODEL` is optional and accepts `#variant`: after the
first `status=error` in a `score` run, the rest of that run uses the fallback and
the `score` JSON reports it as `fallback_model`. Because the model is part of the
input fingerprint, changing it re-scores offers gradually. The executable is
resolved only through an absolute path or a `PATH` lookup. Child output is
bounded **while the process runs**: crossing
`JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES` or the timeout kills the child. It reuses
the same hardened prompt as the HTTP scorer, so the posting is treated as
untrusted data. Incomplete output stays `parse_error` and is **never retried
automatically**: jobscout never invents a score. Re-run the scorer to try again.

`--scorer-model` and `--effort` set the model and effort for one `score` run,
handy for A/B comparisons; `--effort ''` sends no variant. Both are part of the
input fingerprint, so changing them re-scores offers. When
`JOBSCOUT_OPENCODE_TIMEOUT_SECONDS` is not set, the per-offer timeout is derived
from the effort: none, `minimal`, or `low` 180 s; `medium` 240 s; `high` 360 s;
`max` 600 s.

## Keyword optimization workflow

Search coverage lives in an explicit catalog (`src/jobscout/keywords.py`): bounded
families of queries, with no generation and no expansion past that list. The
`baseline` families are the ones that already worked (`Junior Cloud Engineer`,
`Junior DevOps Engineer`, `Platform Engineer`); the `security` families add Cloud
Security and DevSecOps coverage (`Cloud Security Engineer`, `Junior Cloud Security
Engineer`, `DevSecOps Engineer`, `Junior DevSecOps Engineer`, `Azure Cloud
Security`, `Azure Security Engineer`, `Azure DevSecOps`, and security
infrastructure). Each family declares at most `HARD_MAX_QUERIES_PER_FAMILY`
queries.

```bash
python -m jobscout keyword-plan                          # every family, offline
python -m jobscout keyword-plan --family cloud-security --family devsecops
python -m jobscout keyword-plan --family cloud-security --location "Madrid, Spain" --remote
python -m jobscout keyword-plan --json                   # machine-readable report
```

`keyword-plan --json` adds `retired_for` to each `planned` entry: the sorted
providers that retired that query (`[]` when none did). In text output those
queries are marked as `- Platform Engineer (retired for: tecnoempleo)`.

`keyword-plan --json` also publishes the Tecnoempleo sweep block under the
top-level `tecnoempleo` key: `browse` with `label`, `url`, `max_pages` (5), and
`max_items` (150), plus `text_queries` (`["prácticas"]`). The Hermes sweep reads
that block instead of hard-coding the URL. In text output the same two lines are
printed under the plan header. Tecnoempleo text mode (`--keywords`) is unchanged.

Every printed line is a `--dry-run` preview command. Turning one into a real
search means replacing `--dry-run` with `--confirm-network`, always an explicit
human action. `JOBSCOUT_KEYWORD_FAMILIES` chooses which families are listed when
`--family` is omitted; an unknown id fails at startup.

### Retired searches (owner rule, 2026-09-30)

A `(provider, query)` pair is retired only when cutting it would cost **no good
offer**. The cost is the active offers that no other query of the same provider
surfaced; a cost offer is *good* when it scores at least `45`. Fourteen pairs are
retired and can no longer be planned or run, not even in `--dry-run`:

| Provider | Retired queries |
|----------|-----------------|
| Tecnoempleo | `Platform Engineer`, `Azure Security Engineer`, `IAM Engineer`, `Junior Cloud Security Engineer`, `Security Infrastructure Engineer`, `Junior Cloud Engineer`, `Junior DevOps Engineer`, `Cloud Security Engineer`, `DevSecOps Engineer`, `Junior DevSecOps Engineer`, `Azure Cloud Security`, `Azure DevSecOps` |
| LinkedIn | `Identity and Access Management Engineer`, `IAM Engineer` |

- **Restored.** `Cloud Security Engineer|linkedin` (0 unique offers) and
  `Platform Engineer|linkedin` (30 unique, 4 good) are active again; the audit
  showed retiring `Platform Engineer` would cost good offers.
- **Retired for the function browse (2026-10-27).** The seven text searches that
  were still active on Tecnoempleo (`Junior Cloud Engineer`, `Junior DevOps
  Engineer`, `Cloud Security Engineer`, `DevSecOps Engineer`, `Junior DevSecOps
  Engineer`, `Azure Cloud Security`, `Azure DevSecOps`) are replaced by the
  function browse: it found 12 of the 13 known-good offers and the 13th is the
  `prácticas` case, which stays as the only Tecnoempleo text query. LinkedIn
  keeps all those queries. The CP-8 rule still holds: no query that cost a good
  offer was retired.
- **Kept active.** `Identity and Access Management Engineer|tecnoempleo`
  (8 unique, 2 good) and `devops|tecnoempleo` (18 unique, 2 good) are **not**
  retired: each would cost good offers.
- The durable registry lives in `RETIRED_SEARCHES` (`src/jobscout/keywords.py`)
  with provider, query, date, and reason, and each provider's search-config
  constructor enforces it (direct callers, CLI, and `--dry-run`).
- Matching is **full normalized equality** (case, whitespace, and diacritics
  folded), never a substring: `Senior Platform Engineer` stays allowed.
- The same literal stays available on any provider not listed; that is why
  `Azure Security Engineer` remains in the LinkedIn catalog.
- Search history and dashboard analytics are preserved: no row is deleted, and
  retired pairs are only omitted as `prune` candidates.
- In the dashboard, retired series stay in the keyword table, greyed and marked
  `· retirada`, and in the payload as `retired_keywords` (without date or reason,
  which live in `keyword-plan`). The Ofertas origin filter offers active series
  only. `summary.keywords` still counts every analyzed series, retired ones
  included. Per-offer provenance is unchanged.

Per-keyword provenance (which search surfaced each job, and at what position) is
stored in SQLite, and the dashboard aggregates it:

1. Run bounded searches with the queries in the plan
   (`search-linkedin ... --confirm-network`), or ingest labeled fixtures.
2. Enrich with `fetch-details` and score with `score --all --rescore`.
3. Export your feedback from the dashboard and import it with `import-feedback`.
   Until feedback is imported, the human-decision columns read *sin dato*.
4. Generate the dashboard and open the **Palabras clave** tab: one sortable table
   with a row per keyword/provider pair and the columns *Keyword*, *Proveedor*,
   *Vistas*, *Activas*, *Score medio*, *≥ 70*, and *Aplicadas*. It opens sorted by
   *Score medio*, descending; clicking a header re-sorts. Retired series show
   greyed with `· retirada`. With no series at all it shows "Todavía no hay
   procedencia de palabras clave registrada.".
5. Prioritize the keywords with the best mean score, more offers at 70 or above,
   and more apply decisions; drop the ones that barely surface active postings.
6. Adjust the keywords and repeat. **Every run is manual**: there is no automatic
   scheduling. Local notifications are opt-in and manual (`jobscout notify`).

How to read the metrics:

- *Vistas* is the distinct postings the search ever surfaced, including those
  jobscout has deactivated; *Activas* is the ones still active. jobscout has
  **no** verified "expired on LinkedIn" signal: the gap between the two is only
  what jobscout itself deactivated.
- *Score medio* averages active jobs with a successful evaluation, rounded, and
  reads *sin dato* when there are none; *≥ 70* counts those reaching 70, the same
  bar that paints a score in the signal colour.
- *Aplicadas* counts the `apply` decisions imported with `import-feedback`, never
  the model recommendation nor marks that only live in the browser.
- The payload keeps the other per-series metrics (duplicates, freshness, human
  rates), which `keyword-plan` still uses.

Observed position is provenance only: LinkedIn result order is **not** treated as
a quality signal.

## Privacy and security

These guarantees are enforced by the code, not just documented:

- Local data (profile, SQLite database and its sidecars, raw captures, reports,
  and every `.env*` file other than `.env.example`) is git-ignored. Never publish
  it.
- Configured LinkedIn URLs must be **HTTPS on `linkedin.com` or a
  `*.linkedin.com` host**; the LLM base URL must be HTTPS. `file:`, `ftp:`,
  `data:`, `http:`, lookalike hosts, and embedded credentials are rejected at
  startup.
- Redirects are followed only to the same host (or under the LinkedIn parent
  domain); a cross-host redirect fails closed and is never retried.
- Only transient failures are retried (timeouts, 5xx). 401/403/429 and unexpected
  statuses **fail closed immediately**.
- Raw payloads are stored only when they are under `JOBSCOUT_RAW_MAX_BYTES` and
  contain no credential-looking content.
- Job text is treated as untrusted data: the LLM is instructed never to follow
  embedded instructions, and prompt delimiters are neutralized.

### Residual risk: OpenCode tool permissions (H2)

OpenCode 2.x does accept `permissions` rules in its `opencode.json`. jobscout
writes a private file that denies the actions that can change the machine or
hang a non-interactive run, plus reading secret files (see above);
`--dangerously-skip-permissions` *widens* access, so it is never passed. Tool
policy still lives in your local OpenCode configuration, which jobscout does not
control or verify beyond that file.

What jobscout does today: it runs with `--standalone` and a private
`XDG_CONFIG_HOME` (environment isolation, not `--pure`, which OpenCode 2.x
removed) by default, runs from a private, empty directory next to the database
(so this repository's agent instructions are not loaded and other processes'
files are not visible), and neutralizes prompt-delimiter forgery.

What remains: a prompt-injected or misbehaving model could still attempt the
allowed tools (`read`, `grep`, `glob`, `webfetch`) inside the working directory,
and `webfetch` could exfiltrate what it reads. **This is an accepted residual
risk (H2).** If you need a hard guarantee, configure a restrictive OpenCode
agent in your local config, or do not use the OpenCode scorer with untrusted
postings.

### Dashboard scope

The dashboard embeds no descriptions, raw payloads, profile or contact data, API
keys, or absolute local paths. Browser feedback is keyed by a hash of the
database path (not the path itself). Some browsers restrict `localStorage` on
`file://` pages; if yours does, the report still renders and filters, but the
export button is the only way to keep your decisions.

## Project structure

```text
.
├── .env.example              # Configuration template (every JOBSCOUT_* variable)
├── profile.example.yaml      # Example profile
├── pyproject.toml            # Metadata, dependencies, pytest configuration
├── src/jobscout/
│   ├── cli.py                # Commands, argument validation, exit codes
│   ├── config.py             # Settings, URL validation, secret redaction
│   ├── db.py                 # SQLite schema, queries, read-only connection
│   ├── keywords.py           # Bounded catalog of search families and queries
│   ├── normalize.py          # Job normalization
│   ├── dedupe.py             # Fingerprints and exact/probable deduplication
│   ├── pipeline.py           # Fixture, search, and detail ingestion
│   ├── preference.py         # Preference exclusions (offline, score untouched)
│   ├── notify.py             # Local notification selection and delivery (opt-in, read-only)
│   ├── dashboard.py          # Self-contained HTML report generator
│   ├── url_policy.py         # URL and redirect validation
│   ├── collectors/           # LinkedIn guest, fixtures, rate limit, HTTP transport
│   └── scoring/              # Heuristic, LLM, OpenCode, schema, and profile
└── tests/
    ├── fixtures/             # Synthetic search, detail, and LLM response data
    └── test_*.py             # Offline suite
```

## Tests

```bash
python -m pytest -q
```

The suite is fully offline: the HTTP transport is always replaced by a test
double, tests that must prove no network is reachable block `socket.socket`, and
the OpenCode CLI is replaced by an injected fake runner. No test invokes a model
or the real `opencode` binary.

CLI smoke test against the fixtures (the database lives in `data/`, git-ignored):

```bash
python -m jobscout --db data/smoke.sqlite3 init-db
python -m jobscout --db data/smoke.sqlite3 ingest-fixture tests/fixtures/linkedin_search_cards_sample.json
python -m jobscout --db data/smoke.sqlite3 fetch-details --fixture tests/fixtures/linkedin_detail_responses.json
python -m jobscout --db data/smoke.sqlite3 --profile tests/fixtures/profile_test.yaml score --all --rescore
python -m jobscout --db data/smoke.sqlite3 rank --top 5
python -m jobscout --db data/smoke.sqlite3 dashboard --top 5 --output reports/smoke.html
```

## Publication and development notes

- The repository ships **no publish pipeline and no CI**. The intended install is
  local and editable (`pip install -e ".[dev]"`), or directly via
  `PYTHONPATH=src`.
- Current version: `0.1.0`.
- Before any publication or PR, make sure local data does not leak: `data/`,
  `reports/`, `profile.yaml`, and `.env` are git-ignored, but still check
  `git status`.
- Use conventional commit messages; do not add AI attribution.
- This project is a local research tool, not a service.

## Known limitations and next steps

- Detail extraction only recognizes known guest markup containers
  (`show-more-less-html__markup`, `description__text`, and similar). An unknown
  markup revision yields `partial` with an explicit reason instead of an invented
  description.
- `fetch-details` does not re-score: run `score --all --rescore` afterwards.
- The search parser supports two card shapes; a 200 response with a non-empty body
  but zero cards is recorded as a diagnostic note and never crashes the run.
- The OpenCode scorer requires a locally installed, authenticated `opencode`
  binary; jobscout does not manage that authentication.
- `JOBSCOUT_OPENCODE_COMMAND` must be an absolute path or a `PATH`-resolvable
  name; a relative path is refused.
- Salary parsing is heuristic and only used when the profile defines a minimum.
- The dashboard embeds every active offer by default; `--top N` bounds the report
  to the best `N`, but keyword analytics are computed over the whole database.
  Deactivated offers stay saved but are not shown.
- `keyword-plan` does not run searches: it prints the bounded plan, the
  Tecnoempleo block (`browse` + `prácticas`), and one command per query. There is
  no batch mode that fires several live searches at once; each query is an
  explicit `search-linkedin` run with `--confirm-network`, and Tecnoempleo runs
  as either the browse or an explicit text query.
- The dashboard's human metrics (approval rate) require importing the feedback
  export; until then they read *sin dato*. The comparison between the model
  recommendation and the human decision is available through `calibrate`
  (read-only, without adjusting prompts or scores).
- **There is no scheduler or automatic application** — by design. Local
  notifications are opt-in, manual, and read-only (`jobscout notify`, disabled
  by default). Feedback import exists, but it only runs when you invoke
  `import-feedback`.
