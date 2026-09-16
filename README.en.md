# jobscout

A **local-first** job search toolkit: it collects public LinkedIn search results
at very low volume, normalizes and deduplicates them into SQLite, and scores each
job against a local structured profile. The default scorer is deterministic and
works offline; two optional scorers are also available — an OpenAI-compatible
endpoint and a scorer that reuses your already-authenticated OpenCode CLI (no API
key required).

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
| Generate a local Spanish HTML dashboard | Write human feedback into the database |

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
| Stored in | SQLite `evaluations` table, with full provenance | this browser's `localStorage`, keyed by project + job id |
| Read by `jobscout` | yes | no — the CLI never reads it |
| Portable | yes, it is in the database | only via **Exportar feedback local (JSON)** |

Each job has local feedback buttons (**Aplicar / Revisar / Descartar**, stored as
`apply` / `consider` / `reject`) and an optional reason box. The human label
*Revisar* is deliberately distinct from the model recommendation *Considerar*.
Feedback is **local only**: it does not apply, does not touch LinkedIn, and does
not write to SQLite. Export downloads `jobscout-feedback.json` using the
`jobscout.feedback.v1` schema.

**Feedback import does not exist yet**, and there is no automatic scheduling:
the exported JSON is the manual carry-forward format.

## CLI reference

Global options: `--db PATH`, `--profile PATH`, `--json` (before or after the
subcommand), and `--version`.

| Command | Purpose |
|---------|---------|
| `init-db` | Create or verify the schema. Idempotent. |
| `ingest-fixture PATH [--source S] [--label L]` | Normalize, deduplicate, and store a JSON fixture (offline). |
| `search-linkedin --keywords K [--location L] [--distance N] [--remote] [--employment-type T] [--max-pages N] [--max-items N] [--dry-run \| --confirm-network]` | Bounded public search. Disabled without `--confirm-network`. |
| `fetch-details [--job-id ID… \| --all] [--max-items N] [--dry-run \| --fixture PATH \| --confirm-network] [--label L]` | Enrich jobs with the full description. Defaults to jobs that are not yet enriched. |
| `list [--limit N] [--min-score S] [--include-inactive]` | Recent jobs with their latest score. |
| `rank [--top N] [--min-score S]` | Jobs with a successful score, best first. |
| `score (--job-id ID… \| --all) [--scorer heuristic\|llm\|opencode] [--limit N] [--rescore]` | Evaluate jobs and print the explainable result. |
| `runs [--limit N]` | Recent search runs and stored counts. |
| `dashboard [--output PATH] [--top N]` | Write a self-contained local HTML report (read-only, offline). |

Exit codes: `0` success, `1` error, `2` refusal (collector disabled, access
denied, or a request that was intentionally not performed).

Inspect a plan without running it:

```bash
python -m jobscout search-linkedin --keywords "senior backend engineer" --location "Buenos Aires, Argentina" --max-pages 2 --dry-run
```

Detail enrichment:

```bash
python -m jobscout fetch-details --dry-run                                            # plan only, no network or writes
python -m jobscout fetch-details --fixture tests/fixtures/linkedin_detail_responses.json
python -m jobscout fetch-details --max-items 5 --confirm-network                      # live, bounded
python -m jobscout fetch-details --all --max-items 10 --confirm-network               # re-fetch everything
```

`fetch-details` does not re-score on its own: run `score --all --rescore` after
enriching.

## Scoring

All three scorers return the same explainable shape: `overall_score` (0-100),
`dimensions`, `blockers`, `strengths`, `gaps`, `unknowns`, `confidence` (0-1),
`recommendation` (`apply` / `consider` / `skip` / `need_more_info`), and
`rationale`. Every evaluation also stores the scorer, model, prompt version,
profile snapshot id, an `input_fingerprint`, and the raw document id, so each
score is auditable after the fact.

| Scorer | When to use it | Requirements |
|--------|----------------|--------------|
| `heuristic` (default) | Always available, deterministic, dependency-free | none |
| `llm` | Any OpenAI-compatible `/chat/completions` endpoint | `JOBSCOUT_LLM_BASE_URL` and `JOBSCOUT_LLM_API_KEY` |
| `opencode` | You already use OpenCode locally and prefer not to manage a key | an installed, authenticated `opencode` CLI |

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

# Default model: opencode-go/deepseek-v4.1-flash
python -m jobscout score --all --scorer opencode --profile profile.yaml
```

It runs `opencode run --pure --model <model> [--variant minimal] --format json
<prompt>` as a subprocess argument array — never through a shell. The executable
is resolved only through an absolute path or a `PATH` lookup. Child output is
bounded **while the process runs**: crossing
`JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES` or the timeout kills the child. It reuses
the same hardened prompt as the HTTP scorer, so the posting is treated as
untrusted data. Incomplete output stays `parse_error` and is **never retried
automatically**: jobscout never invents a score. Re-run the scorer to try again.

## Keyword optimization workflow

Per-keyword provenance (which search surfaced each job, and at what position) is
stored in SQLite, and the dashboard aggregates it:

1. Run bounded searches with the keywords you want to evaluate
   (`search-linkedin ... --confirm-network`), or ingest labeled fixtures.
2. Enrich with `fetch-details` and score with `score --all --rescore`.
3. Generate the dashboard and open the **keyword performance** section: one row
   per keyword with *Apariciones*, *Ofertas únicas*, *Apariciones duplicadas*,
   *Puntaje promedio*, *Con puntaje*, *Aplicar*, and *Considerar*.
4. Prioritize the keywords with the best average score and more
   apply/consider; drop the ones that only add duplicate sightings. *Puntaje
   promedio* only averages jobs with a successful evaluation and reads *sin dato*
   when there are none.
5. Adjust the keywords and repeat. **Every run is manual**: there is no automatic
   scheduling and no notifications.

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
│   ├── normalize.py          # Job normalization
│   ├── dedupe.py             # Fingerprints and exact/probable deduplication
│   ├── pipeline.py           # Fixture, search, and detail ingestion
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
- **There is no feedback import, scheduler, notification, or automatic
  application** — by design.
