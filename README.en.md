# jobscout

A **local-first** job search toolkit: it collects public LinkedIn search results
at very low volume, normalizes and deduplicates them into SQLite, and scores each
job against a local structured profile. The default scorer reuses your already-
authenticated OpenCode CLI with the free MiMo model; two other scorers are also
available — a deterministic offline scorer and an OpenAI-compatible endpoint.

Nothing leaves your machine except the requests you explicitly authorize.
**jobscout never submits job applications.**

**Languages:** English (this file) · [Versión en español](README.md)

## What it does and does not do

| It does | It does not |
|---------|-------------|
| Collect public search cards (opt-in, bounded) | Log in, use cookies, solve CAPTCHAs, or evade anti-bot systems |
| Enrich jobs with the full description (opt-in, bounded) | Apply, contact anyone, or automate applications |
| Deduplicate and store everything in local SQLite | Upload data to any project-owned server |
| Score with heuristic, OpenAI-compatible, or OpenCode CLI | Schedule runs or send notifications |
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
| 2 | Enrich | `python -m jobscout fetch-details --max-items 10 --confirm-network` (or `--fixture` to stay offline) | Fetches each offer's full description, bounded in volume. | Semi-automatic: you authorize each run |
| 3 | Score with MiMo | `python -m jobscout score --all --scorer opencode` | The default scorer (OpenCode + MiMo) evaluates only offers that have no evaluation yet. | Scoring is automatic; the apply decision is not |
| 4 | Generate the dashboard | `python -m jobscout dashboard --top 25` | Writes a local HTML report with offers, scores, link evidence, and preference filters. | Automatic and read-only |
| 5 | Review offers | Open `reports/jobscout-dashboard.html` | You mark **Aplicar / Revisar / Descartar** in the browser. | Human decision only |
| 6 | Export/import feedback | Export the JSON from the dashboard, then run `python -m jobscout import-feedback jobscout-feedback.json` | Your decisions move from `localStorage` to the SQLite `job_feedback` table. | Import runs automatically once you invoke it |
| 7 | Calibrate | `python -m jobscout calibrate --scorer opencode` | Compares the model recommendation with your imported human decision. Read-only; it adjusts nothing. | Automatic and read-only |
| 8 | Analyze keywords | `python -m jobscout dashboard` → the *rendimiento de palabras clave* section | After several cycles, you decide which search families perform and which to drop. | Human decision |

Cycle notes:

- **It never applies automatically.** There is also no scheduler and no
  notifications: every run is one you invoke.
- `--rescore` **is not a daily step**: it re-evaluates offers that already have a
  score and is only needed when you change the profile or the prompt. For new
  offers, the normal cycle is `score --all --scorer opencode`, which evaluates
  exactly the offers that have no score yet.
- `fetch-details` does not re-score on its own; stage 3 closes the enrichment.
- Every evaluation is versioned: it stores the scorer's `prompt_version` and the
  profile `input_fingerprint`, so a policy change is distinguishable without
  rewriting history.

### Seniority and technology criterion (prompt v5)

The OpenCode prompt is versioned as **`opencode-v5-seniority-aware`**. The policy
MiMo applies is:

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
English so the report never drifts from the database.

```bash
# POSIX
python -m jobscout dashboard --output reports/jobscout-dashboard.html --top 25
xdg-open reports/jobscout-dashboard.html   # Linux
open reports/jobscout-dashboard.html       # macOS
```

```powershell
# Windows PowerShell
python -m jobscout dashboard --output reports\jobscout-dashboard.html --top 25
Start-Process reports\jobscout-dashboard.html
```

- Default output path: `reports/jobscout-dashboard.html` (git-ignored). Default
  `--top`: `25`. Forward slashes also work on Windows:
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
| Portable | yes, it is in the database | with **Exportar feedback local (JSON)** plus `import-feedback` |

Each job has local feedback buttons (**Aplicar / Revisar / Descartar**, stored as
`apply` / `consider` / `reject`) and an optional reason box. The human label
*Revisar* is deliberately distinct from the model recommendation *Considerar*.
The buttons themselves are **browser-local**: they do not apply, do not touch
LinkedIn, and write nothing to SQLite. **Exportar feedback local (JSON)**
downloads `jobscout-feedback.json` (`jobscout.feedback.v1` schema).

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
| `consider` | `consider` |
| `skip` | `reject` |

`need_more_info` is **not** part of the three-way agreement: it is reported
separately as an unresolved category and never silently becomes `skip`. The
report shows comparable samples, exact agreement, the confusion matrix,
unresolved counts, and the `overall_score` distribution by human decision. A
decision with no valid evaluation (`status = 'ok'`) is counted separately and
stays out of the agreement math.

### Link evidence (detail)

Each offer shows a **link evidence** line next to its title, kept apart from the
two decision axes. It is a stored fact, not a recommendation: it is computed only
from that offer's latest `fetch-details` attempt, and it never changes scores,
recommendations, or the database.

| State | Meaning |
|-------|---------|
| `unchecked` | No detail attempt has been recorded. |
| `skipped` | The latest attempt was deliberately skipped. |
| `ok` | The latest attempt succeeded (ok/partial) within the 21-day window. |
| `stale` | The latest attempt succeeded but is older than the 21-day window. |
| `definitive_missing` | The latest attempt returned HTTP 404/410. |
| `retryable_failure` | The latest attempt failed without receiving an HTTP response. |
| `failed` | The latest attempt failed with another recorded HTTP response. |

- The 21-day window is explicit; evidence with no usable date is reported as
  `stale`, never as current.
- A redirect's `final_url` is shown only as safe evidence of a successful
  attempt, goes through the same URL validation as every other link, and only
  when it differs from the requested URL. It is never presented as a broken link.
- If a visible offer has a 404/410 and a later re-sighting exists, the report
  says so: the evidence was refuted. Brokenness is never invented or inferred
  from a redirect.

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

The report shows a neutral (hue-free) band next to the title with the outcome,
the reason, and the evidence, and the summary counts excluded vs. flagged
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
| `search-getmanfred [--max-documents N] [--max-items N] [--cycle-id ID] [--label L] [--dry-run \| --confirm-network]` | Public GetManfred sitemap inventory. No keyword fan-out and no ranking. Disabled without `--confirm-network`. |
| `search-tecnoempleo --keywords K [--location L] [--max-pages N] [--max-items N] [--cycle-id ID] [--label L] [--dry-run \| --confirm-network]` | Bounded Tecnoempleo public server-rendered search. Enforces `robots.txt` at run time. Disabled without `--confirm-network`. |
| `keyword-plan [--family ID…] [--location L] [--remote]` | List the keyword families and the exact command per query (no network, no database). |
| `fetch-details [--job-id ID… \| --all \| --refresh [--stale-days N]] [--max-items N] [--dry-run \| --fixture PATH \| --confirm-network] [--label L]` | Enrich jobs with the full description. Defaults to jobs that are not yet enriched; `--refresh` revisits already-fetched offers with stale or retryable evidence. |
| `list [--limit N] [--min-score S] [--include-inactive]` | Recent jobs with their latest score. |
| `rank [--top N] [--min-score S]` | Jobs with a successful score, best first. |
| `score (--job-id ID… \| --all) [--scorer heuristic\|llm\|opencode] [--limit N] [--rescore]` | Evaluate jobs and print the explainable result. |
| `runs [--limit N]` | Recent search runs and stored counts. |
| `dashboard [--output PATH] [--top N]` | Write a self-contained local HTML report (read-only, offline). |
| `import-feedback PATH [--project-id ID]` | Store a dashboard feedback export in SQLite (offline, idempotent). |
| `calibrate [--scorer heuristic\|llm\|opencode]` | Compare each job's latest valid evaluation with imported human feedback (read-only, offline). |

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
python -m jobscout search-tecnoempleo --keywords "cloud security" --location madrid --dry-run
python -m jobscout search-tecnoempleo --keywords "cloud security" --max-pages 2 --confirm-network
```

`search-getmanfred` is an **inventory**, not a search: it takes no keywords, does
no fan-out, and records every observed position as `position_kind = none` with
rank `0`. The title is derived from the public URL slug and the company is left
unknown because the sitemap does not expose it. Postings stay `partial`.

`search-tecnoempleo` uses only the public server-rendered search under
`/ofertas-trabajo/`. It re-reads `robots.txt` before the first request and fails
closed when the path is disallowed. It uses no login, cookies, private RSS, or
unauthorized API. **Declared limitation:** its selectors are not verified against
a live page (its `robots.txt` disallows automated crawlers), so confirm
`JOBSCOUT_TECNOEMPLEO_OFFER_PATH_MARKER` before a live run.

Provenance convention: `provider` (platform), `query` (keyword), `cycle_id`
(comparable search cycle), and `position_kind` (`search_rank` = real search
position; `none` = inventory with no order). The dashboard keyword analytics are
keyed by `provider|keyword|cycle`, so the same term on LinkedIn, GetManfred, and
Tecnoempleo is never merged.

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
```

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

## Scoring

All three scorers return the same explainable shape: `overall_score` (0-100),
`dimensions`, `blockers`, `strengths`, `gaps`, `unknowns`, `confidence` (0-1),
`recommendation` (`apply` / `consider` / `skip` / `need_more_info`), and
`rationale`. Every evaluation also stores the scorer, model, prompt version,
profile snapshot id, an `input_fingerprint`, and the raw document id, so each
score is auditable after the fact.

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

# Default free model: opencode/mimo-v2.5-free
python -m jobscout score --all --scorer opencode --profile profile.yaml
```

When `--scorer` is omitted, OpenCode with MiMo is used. For an explicit offline
evaluation, use `--scorer heuristic`.

It runs `opencode run --pure --model <model> [--variant minimal] --format json
<prompt>` as a subprocess argument array — never through a shell. The executable
is resolved only through an absolute path or a `PATH` lookup. Child output is
bounded **while the process runs**: crossing
`JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES` or the timeout kills the child. It reuses
the same hardened prompt as the HTTP scorer, so the posting is treated as
untrusted data. Incomplete output stays `parse_error` and is **never retried
automatically**: jobscout never invents a score. Re-run the scorer to try again.

## Keyword optimization workflow

Search coverage lives in an explicit catalog (`src/jobscout/keywords.py`): bounded
families of queries, with no generation and no expansion past that list. The
`baseline` families are the ones that already worked (`Junior Cloud Engineer`,
`Junior DevOps Engineer`, `Platform Engineer`); the `security` families add Cloud
Security and DevSecOps coverage (`Cloud Security Engineer`, `Junior Cloud Security
Engineer`, `DevSecOps Engineer`, `Junior DevSecOps Engineer`, `Azure Cloud
Security`, `Azure DevSecOps`, IAM, and security infrastructure). Each family
declares at most `HARD_MAX_QUERIES_PER_FAMILY` queries.

```bash
python -m jobscout keyword-plan                          # every family, offline
python -m jobscout keyword-plan --family cloud-security --family devsecops
python -m jobscout keyword-plan --family cloud-security --location "Madrid, Spain" --remote
python -m jobscout keyword-plan --json                   # machine-readable report
```

Every printed line is a `--dry-run` preview command. Turning one into a real
search means replacing `--dry-run` with `--confirm-network`, always an explicit
human action. `JOBSCOUT_KEYWORD_FAMILIES` chooses which families are listed when
`--family` is omitted; an unknown id fails at startup.

Per-keyword provenance (which search surfaced each job, and at what position) is
stored in SQLite, and the dashboard aggregates it:

1. Run bounded searches with the queries in the plan
   (`search-linkedin ... --confirm-network`), or ingest labeled fixtures.
2. Enrich with `fetch-details` and score with `score --all --rescore`.
3. Export your feedback from the dashboard and import it with `import-feedback`.
   Until feedback is imported, the human-decision columns read *sin dato*.
4. Generate the dashboard and open the **keyword performance** section: one row
   per keyword with *Apariciones*, *Ofertas únicas*, *Tasa duplicados*, *Puntaje
   promedio*, *Top 5*, *Top 10*, *Con puntaje*, *Aplicar*, *Considerar*,
   *Revisadas*, *Tasa aplicar (humano)*, *Frescura*, and *No activas*.
5. Prioritize the keywords with the best average score, more model
   apply/consider, and the best human approval rate; drop the ones that only add
   duplicate sightings or no longer surface active postings.
6. Adjust the keywords and repeat. **Every run is manual**: there is no automatic
   scheduling and no notifications.

How to read the metrics:

- *Puntaje promedio*, *Top 5*, and *Top 10* only average active jobs with a
  successful evaluation and read *sin dato* when there are none. *Top 5*/*Top 10*
  answer "how good are my best offers for this search?", not "how good is the
  average?".
- *Tasa duplicados* is duplicate sightings over total sightings: a keyword that
  only repeats what others already surfaced adds little.
- *Tasa aplicar (humano)* uses only the imported decisions
  (`apply`/`consider`/`reject`), never the model recommendation. It reads *sin
  dato* until there are decisions for that keyword, and `0 %` only when there were
  real decisions and none was apply.
- *Frescura* is the share of postings 21 days old or less among those with a known
  date. jobscout has **no** verified "expired on LinkedIn" signal, so it does not
  invent one: *No activas* is the share of postings jobscout itself deactivated,
  and it is the only claim made about dropped postings.

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

`opencode run` (v1.14.30) **exposes no reliable flag to deny tools or sandbox
execution**. The only related switch is `--dangerously-skip-permissions`, which
*widens* access and is never passed by jobscout. Tool policy lives in your local
OpenCode configuration, which jobscout does not control or verify.

What jobscout does today: it runs with `--pure` (no external plugins) by default,
runs from a neutral working directory (the system temp directory, so this
repository's agent instructions are not loaded), and neutralizes
prompt-delimiter forgery.

What remains: a prompt-injected or misbehaving model could still attempt tool
calls permitted by your local OpenCode configuration. **This is an accepted
residual risk (H2).** If you need a hard guarantee, configure a restrictive
OpenCode agent in your local config, or do not use the OpenCode scorer with
untrusted postings.

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
- The dashboard embeds only the top `--top` offers, but keyword analytics are
  computed over the whole database.
- `keyword-plan` does not run searches: it prints the bounded plan and one command
  per query. There is no batch mode that fires several live searches at once; each
  query is an explicit `search-linkedin` run with `--confirm-network`.
- The dashboard's human metrics (approval rate) require importing the feedback
  export; until then they read *sin dato*. The comparison between the model
  recommendation and the human decision is available through `calibrate`
  (read-only, without adjusting prompts or scores).
- **There is no scheduler, notification, or automatic application** — by design.
  Feedback import exists, but it only runs when you invoke `import-feedback`.
