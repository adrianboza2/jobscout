# jobscout

Local-first job search toolkit: collect public LinkedIn search results at very low
volume, normalize and deduplicate them into SQLite, and score each job against a
local structured profile — with a deterministic offline scorer by default, an
optional OpenAI-compatible LLM scorer, and an optional scorer that reuses your
locally authenticated OpenCode CLI (no API key required).

Nothing leaves your machine except the requests you explicitly opt into.

## Scope and safety rules

These are enforced by the code, not just documented:

| Rule | Where it lives |
|------|----------------|
| The LinkedIn collector is **disabled by default**; it refuses to run without `--confirm-network` | `collectors/linkedin.py` |
| The job-detail fetcher is **disabled by default**; it refuses to run without `--confirm-network` (or an offline `--fixture`) | `collectors/linkedin_details.py`, `cli.py` |
| `--dry-run` prints the exact request plan and performs **no I/O** | `collectors/linkedin.py`, `collectors/linkedin_details.py` |
| No login automation, no cookies, no CAPTCHA handling, no anti-bot evasion | only the public guest endpoints are used, with an honest user agent |
| Bounded page/item caps, request delay, jitter, timeout, and download size | `config.py`, `collectors/ratelimit.py`, `collectors/base.py` |
| Only *transient* failures are retried (timeouts, 5xx); 401/403/429/unexpected statuses **fail closed immediately** | `collectors/ratelimit.py` |
| A partial or failed detail result **never overwrites** a previously successful full description | `pipeline._merge_detail_description` |
| Raw payloads are stored for provenance, size-bounded, and never store credentials | `config.is_safe_to_store` |
| Configured LinkedIn endpoints must be **HTTPS on `linkedin.com` or `*.linkedin.com`**; the LLM base URL must be HTTPS. Other schemes (`file:`, `ftp:`, `data:`, `http:`), unrelated hosts, and embedded credentials are rejected | `url_policy.py`, `config.Settings` |
| Redirects are followed only **same-host** or within the LinkedIn parent domain; a cross-host or non-HTTP redirect fails closed and is never retried | `url_policy.redirect_allowed`, `collectors/base.build_redirect_handler` |
| HTTP **error bodies** are read through the same download ceiling as successful bodies | `collectors/base.UrllibHttpClient` |
| The OpenCode executable is resolved only through an **absolute path or `shutil.which`**; a cwd-relative planted binary is never used | `scoring/opencode.resolve_executable` |
| OpenCode child output is bounded **while the process runs** (Popen + reader threads); overflow or timeout kills the child | `scoring/opencode.run_command` |
| Job text is treated as untrusted data; the LLM is instructed never to follow instructions inside it, and prompt delimiters are neutralized | `scoring/llm.py` |
| The HTML dashboard reads the database with `PRAGMA query_only` and never creates a missing database; it emits no external asset, CDN, or network request, escapes all embedded data, and builds the DOM with `textContent` only | `dashboard.py`, `db.connect_readonly` |
| The OpenCode CLI scorer shells out with an argument array (no `shell=True`), runs `--pure`, bounds time and output, removes configured secrets from the child environment, and redacts/refuses to persist any output containing them | `scoring/opencode.py` |

Operators are responsible for complying with the target site's terms of service
and local law. Do not run live collection in CI.

## Security notes

These notes describe the guarantees the code enforces today, and the risks it
does **not** remove.

### URL and redirect policy

- A configured search or detail URL is validated when `Settings` is built (from
  the environment, `.env`, `--dry-run`, or a library caller). It must be `https`
  on `linkedin.com` or a `*.linkedin.com` host, with no embedded credentials and
  no non-default port. `file://`, `ftp://`, `data://`, plain `http://`,
  lookalike hosts (`linkedin.com.evil.test`, `evil-linkedin.com`), and
  `https://user:pass@host` are all rejected with a configuration error.
- `JOBSCOUT_LLM_BASE_URL` must be `https` with no embedded credentials. Local
  and file schemes (`http://localhost`, `file:`, `data:`, `ftp:`) are refused so
  an API key is never sent in the clear. Offline tests inject a fake transport
  instead of lowering this rule.
- The `urllib` transport replaces the default redirect handling. A redirect is
  followed only when the target host is the same host, or — for LinkedIn — under
  the same `linkedin.com` parent domain (`www.linkedin.com` → `es.linkedin.com`
  still works). A cross-host or non-HTTP redirect raises `UnsafeUrlError`, which
  fails closed and is **never** retried.
- A request to a non-HTTP scheme is refused before any I/O. This closes the
  local-file-disclosure path where `urllib` would return a `file://` target's
  bytes with a `None` status.

### Subprocess bounds (OpenCode CLI)

- Output is captured with `subprocess.Popen` and reader threads that enforce
  `JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES` **as the bytes arrive**. Crossing the
  ceiling kills the child instead of buffering an unbounded response in memory;
  the timeout is enforced by the same supervisor loop. The previous
  `subprocess.run(capture_output=True)` buffered everything before any ceiling
  applied.
- The command is always an argument array with `shell=False`, and the
  configured secret is removed from the child environment and redacted from
  captured output.

### OpenCode tool permissions — residual risk (H2)

`opencode run --help` (v1.14.30) exposes **no reliable tool-denial or sandbox
flag**. The only permission-related switch is
`--dangerously-skip-permissions`, which *widens* access and is never passed.
Tool allow/deny policy lives in OpenCode's own agent/`opencode.json`
configuration, which jobscout does not control or verify.

What jobscout does today:

- runs the CLI with `--pure` by default (`JOBSCOUT_OPENCODE_PURE=true`), which
  disables external plugins — the most restrictive invocation available without
  editing global config;
- runs from a neutral working directory (`JOBSCOUT_OPENCODE_WORKDIR`, default the
  system temp directory) so this repository's agent instructions are not loaded;
- treats the job posting as untrusted data and neutralizes prompt-delimiter
  forgery in the prompt.

What remains: a prompt-injected or misbehaving model could still attempt tool
calls permitted by the operator's local OpenCode configuration. **This is an
accepted residual risk (H2).** If you need a hard guarantee, configure a
restrictive OpenCode agent whose tools are denied in your local OpenCode config
and select it yourself, or do not use the OpenCode scorer for untrusted
postings.

### Private local data

- The candidate profile (`profile.yaml`), the SQLite database and all of its
  sidecars, raw page captures, local reports, and every `.env*` file other than
  `.env.example` are git-ignored. Never commit them.
- Raw payloads are only persisted when they are under
  `JOBSCOUT_RAW_MAX_BYTES` and contain no credential-looking content.
  `is_safe_to_store` additionally refuses LinkedIn session cookies (`li_at`,
  `JSESSIONID`), PEM private-key headers, labelled `*_TOKEN`/`*_SECRET`/
  `password`/`client_secret`/`refresh_token`/`access_token` values, and
  AWS/GitHub/NVIDIA-style tokens. Labelled credentials are detected in plain,
  JSON-quoted (`{"password": "..."}`), and JSON-escaped (`\"password\": \"...\"`)
  forms, so a credential embedded in a model or API payload is still refused.


## Requirements

- Python 3.11+ (`sqlite3`, `urllib`, `argparse` from the standard library)
- `PyYAML` for YAML profiles (JSON profiles work with no third-party packages)
- Optional: the [OpenCode](https://opencode.ai) CLI, already authenticated, only
  if you want to use `score --scorer opencode`

## Install

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on POSIX
pip install -e ".[dev]"
```

The editable install puts the `jobscout` package on `sys.path`, so every
`python -m jobscout ...` command works from the repository root.

Running without installing also works: `pyproject.toml` puts `src/` on
`sys.path` for pytest, and the CLI can be invoked directly by adding `src/` to
`PYTHONPATH`:

```bash
# POSIX shells (bash/zsh)
PYTHONPATH=src python -m jobscout init-db
```

```powershell
# Windows PowerShell
$env:PYTHONPATH = "src"; python -m jobscout init-db
```

Copy the configuration and profile templates:

```bash
cp .env.example .env
cp profile.example.yaml profile.yaml
```

On Windows (`cmd` or PowerShell) use `copy .env.example .env` and
`copy profile.example.yaml profile.yaml` instead.

## Offline quick start (no network, ever)

These commands assume either the editable install above or the `PYTHONPATH=src`
alternative. They run from the repository root and only touch files inside it
(`data/` is git-ignored).

```bash
# 1. Create the SQLite database
python -m jobscout init-db

# 2. Ingest the bundled synthetic search-card fixture (no descriptions)
python -m jobscout ingest-fixture tests/fixtures/linkedin_search_cards_sample.json

# 3. Enrich those cards with full descriptions from an offline detail fixture
python -m jobscout fetch-details --fixture tests/fixtures/linkedin_detail_responses.json

# 4. Look at what was stored
python -m jobscout list --limit 10

# 5. Score everything with the deterministic offline scorer
python -m jobscout score --all --rescore --scorer heuristic --profile profile.example.yaml

# 6. Rank the scored jobs
python -m jobscout rank --top 10

# 7. Write a self-contained HTML report you can open in a browser (no server)
python -m jobscout dashboard --top 10

# Machine-readable output for any command
python -m jobscout rank --top 5 --json
```

Steps 2–3 also work with `tests/fixtures/linkedin_search_sample.json`, which
already contains full descriptions, so `fetch-details` finds nothing to enrich.

If you did not install the package, prefix each command with `PYTHONPATH=src`
(POSIX) or set `$env:PYTHONPATH = "src"` once in PowerShell. The profile flag in
step 4 can also be omitted; the default is `profile.example.yaml`.

## CLI reference

Global options (`--db`, `--profile`, `--json`) may be given before or after the
subcommand.

| Command | Purpose |
|---------|---------|
| `init-db` | Create/verify the schema. Idempotent. |
| `ingest-fixture PATH` | Normalize, deduplicate, and store a JSON fixture (offline). |
| `search-linkedin --keywords K [--location L] [--dry-run \| --confirm-network]` | Bounded public guest search. Disabled unless `--confirm-network` is passed. |
| `fetch-details [--job-id ID… \| --all] [--max-items N] [--dry-run \| --fixture PATH \| --confirm-network]` | Enrich stored jobs with full descriptions. Defaults to jobs that are not yet enriched. |
| `list [--limit N] [--min-score S]` | Recent jobs with their latest score. |
| `rank [--top N] [--min-score S]` | Jobs that have a successful score, best first. |
| `score (--job-id ID… \| --all) [--scorer heuristic\|llm\|opencode] [--rescore]` | Evaluate jobs and print the explainable result. |
| `runs [--limit N]` | Recent search runs and stored counts. |
| `dashboard [--output PATH] [--top N]` | Write a self-contained local HTML report of scored jobs and keyword performance. Read-only and offline. |

Exit codes: `0` success, `1` error, `2` refusal (collector disabled, access
denied, or a request that was intentionally not performed).

### Inspecting a live run without performing one

```bash
python -m jobscout search-linkedin \
  --keywords "senior backend engineer" \
  --location "Buenos Aires, Argentina" \
  --max-pages 2 --dry-run
```

This prints the exact URLs a live run would request and exits `0`. Nothing is
fetched and nothing is recorded.

### Live run (opt-in, conservative defaults)

```bash
python -m jobscout search-linkedin \
  --keywords "senior backend engineer" \
  --location "Buenos Aires, Argentina" \
  --confirm-network
```

Defaults: 2 pages, at most 40 stored items, ~8s between requests (±2s jitter),
20s timeout, 2 retries for transient failures only. Search cards are summaries,
so collected jobs are stored with `content_status = partial`; run
`fetch-details` (below) to enrich them with full descriptions. Each live response
is read at most `JOBSCOUT_RAW_MAX_BYTES` bytes, so an oversized page is never
buffered whole in memory: it is recorded as metadata only and is not parsed.

## Job-detail enrichment (opt-in)

Search cards only carry a summary. `fetch-details` fetches the public guest
detail page for a bounded selection of stored jobs and stores the full
description plus an auditable provenance row per attempt.

```bash
# Select the un-enriched jobs (default) and inspect the plan — no network, no writes
python -m jobscout fetch-details --dry-run

# Offline: replay canned detail responses from a JSON fixture (no network at all)
python -m jobscout fetch-details --fixture tests/fixtures/linkedin_detail_responses.json

# Explicit selection, bounded to 5 requests
python -m jobscout fetch-details --max-items 5 --confirm-network

# Re-fetch every active job, including already-enriched ones
python -m jobscout fetch-details --all --max-items 10 --confirm-network

# Score the enriched jobs (rescore is required for jobs that already have a score)
python -m jobscout score --all --rescore --profile profile.example.yaml
python -m jobscout rank --top 10
```

Safety and limits:

- **Opt-in.** Without `--confirm-network` (and without an offline `--fixture`)
  the command refuses and exits `2`. `--dry-run` prints the exact URLs and
  performs no network request and no database write. It reads an existing
  database if one is present and never creates one: with no database it prints
  an empty plan and a clear note instead of touching the filesystem.
- **Bounded.** At most `JOBSCOUT_DETAIL_MAX_ITEMS` jobs per run (absolute ceiling
  `250`, shared with the search collector), the same request delay, jitter, and
  retry policy, a per-request timeout (`JOBSCOUT_DETAIL_TIMEOUT_SECONDS`), and a
  download ceiling (`JOBSCOUT_DETAIL_MAX_BYTES`). A response over the ceiling is
  recorded as a failure **before** anything is persisted, so its body is never
  parsed or stored. Persistence additionally caps the stored body at
  `min(JOBSCOUT_DETAIL_MAX_BYTES, JOBSCOUT_RAW_MAX_BYTES)`, so a response that
  exceeds either ceiling can never leak into the raw-payload table. When the
  selection returns more jobs than `--max-items`, both `--dry-run` and live runs
  report how many were dropped (`truncated` in `--json`, plus an explicit line in
  the text output).
- **Fail closed.** 401/403/429/999 and unexpected statuses abort the run and
  exit `2`; only transient failures (timeouts, 5xx) are retried. A 404/410 is a
  definitive per-item failure and the run continues.
- **Content preservation.** A successful full description replaces a shorter or
  partial one. A partial or failed result never replaces a previously successful
  full description, so re-running the command is safe.
- **Provenance.** Every attempt writes a `job_detail_fetches` row with the
  status (`ok` / `partial` / `failed` / `skipped`), HTTP status, source URL,
  fetched timestamp, raw document id, and — for failures — a reason. The
  `content_hash` column records the hash of the description *this attempt*
  stored, so a failed or preserved attempt leaves it empty instead of borrowing
  the job's existing hash. Secret-looking or oversized raw bodies are not stored.
- Fixture mode still honours the configured pacing between requests; set
  `JOBSCOUT_REQUEST_DELAY_SECONDS=0` for instant offline replay.

## Scoring

Both scorers return the same structured, explainable shape:

```
overall_score   float 0-100 (weighted across dimensions)
dimensions      [{name, score, weight, rationale, evidence[]}]
blockers        hard disqualifiers (any blocker forces recommendation = skip)
strengths       matched must-have and nice-to-have signals
gaps            missing must-have skills, seniority/location mismatches
unknowns        facts the posting does not state
confidence      0-1, reduced by missing description/date/location
recommendation  apply | consider | skip | need_more_info
rationale       human-readable summary
```

Storage also keeps the scorer name, model, prompt version, profile snapshot id,
an `input_fingerprint`, and the raw response document id, so every score is
auditable after the fact.

### Heuristic scorer (default, offline)

Deterministic and dependency-free. Dimensions: `must_have_skills`, `skills`
(nice-to-have), `seniority`, `location`, `language`, `recency`, and
`compensation`. Weights come from the profile and are renormalized over the
dimensions that apply.

### LLM scorer (optional)

Any OpenAI-compatible `/chat/completions` endpoint works:

```bash
export JOBSCOUT_LLM_BASE_URL=https://api.example.com/v1
export JOBSCOUT_LLM_API_KEY=...
export JOBSCOUT_LLM_MODEL=deepseek-v4.1-flash   # default
python -m jobscout score --all --scorer llm --profile profile.yaml
```

Without `JOBSCOUT_LLM_BASE_URL` and `JOBSCOUT_LLM_API_KEY` the LLM scorer
refuses to run and points back at the heuristic scorer. Tests never need a key.
The model defaults to `deepseek-v4.1-flash` and is always configurable.

LLM responses are parsed defensively: markdown fences and surrounding prose are
stripped, a balanced JSON object is extracted, field aliases are normalized,
scores are clamped, percentages are converted, unknown recommendations fall back
to `need_more_info`, and non-empty blockers force `skip`. Unparseable output is
recorded as an evaluation with `status = parse_error` instead of crashing the
run. The raw response is stored only when it is small and credential-free.

### OpenCode CLI scorer (optional, no API key)

If you already use [OpenCode](https://opencode.ai) locally, jobscout can score
through the installed `opencode` CLI instead of a raw HTTP endpoint:

```bash
# One-time: authenticate OpenCode with the provider that serves the model
opencode auth login

# Score through the CLI; the default model is opencode-go/deepseek-v4.1-flash
python -m jobscout score --all --scorer opencode --profile profile.yaml
```

How it works:

- It runs `opencode run --pure --model opencode-go/deepseek-v4.1-flash
  [--variant minimal] --format json <prompt>` as a subprocess argument array —
  never through a shell. `--pure` (no external plugins) is on by default via
  `JOBSCOUT_OPENCODE_PURE`; `--variant` is only added when
  `JOBSCOUT_OPENCODE_VARIANT` is non-empty (default `minimal`).
- The executable is resolved to an absolute path (an absolute
  `JOBSCOUT_OPENCODE_COMMAND`, or a `PATH` lookup). A relative path is never
  resolved against the current working directory, so a planted `opencode` file
  cannot hijack the invocation.
- Child output is bounded **while the process runs**: output is captured with
  `Popen` plus reader threads and the child is killed when it exceeds
  `JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES` or the timeout. The old
  `subprocess.run(capture_output=True)` buffered the whole response first.
- It reuses the same hardened system/user messages as the HTTP scorer, so the
  posting is framed as untrusted data and delimiter forgery is neutralized. An
  OpenCode-specific output contract is appended to require one compact JSON
  object, no prose, a short rationale, no padded dimensions or lists, and a
  completely closed object — without replacing the schema or security rules.
- It relies entirely on OpenCode's own local authentication. jobscout does not
  use `JOBSCOUT_LLM_API_KEY` to authenticate this path, does not place the key
  in the CLI argument array or in project files, removes configured secret
  values from the child environment, and scrubs captured output against them
  before it could be persisted.
- `--format json` yields raw JSON events; the scorer collects the assistant text
  parts and then extracts exactly one JSON evaluation. Plain text, a single JSON
  object, and JSON wrapped in prose all work too.
- Missing CLI, timeout, non-zero exit, and malformed output become evaluations
  with `status = error`/`parse_error` and a secret-redacted explanation, so a
  batch keeps going.
- Malformed or incomplete output stays `parse_error`: jobscout never invents a
  score or auto-repairs an unterminated JSON object. On the observed live call
  the model emitted a valid JSON prefix, stopped before closing the object
  (`reason=stop` in the CLI event metadata), and the scorer recorded
  `parse_error`. That first call was intentionally **not retried automatically**
  — a silent retry could mask a truncated answer or invite an invented score.
  Re-run the scorer explicitly for another attempt.
- Captured output is bounded to `JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES` while the
  child runs, and the raw output is stored only when it is small, contains no
  configured secret, and contains no credential-looking content.

The CLI runs from `JOBSCOUT_OPENCODE_WORKDIR`, which defaults to the system temp
directory so OpenCode cannot pick up this repository's `AGENTS.md`/`opencode.json`
instructions and derail the strict JSON prompt. Point it at a project directory if
you want those instructions.

## Local HTML dashboard (offline, read-only)

`dashboard` reads the existing SQLite database and writes **one** HTML file. That
file embeds its own CSS, JavaScript, and data: it needs no CDN, no external
asset, no server, and no network request. Open it directly, or double-click it.

```bash
# POSIX (bash/zsh)
python -m jobscout dashboard --output reports/jobscout-dashboard.html --top 25
xdg-open reports/jobscout-dashboard.html    # Linux
open reports/jobscout-dashboard.html        # macOS
```

```powershell
# Windows PowerShell
python -m jobscout dashboard --output reports\jobscout-dashboard.html --top 25
start reports\jobscout-dashboard.html
```

```bat
:: Windows cmd.exe (default output path is used when --output is omitted)
python -m jobscout dashboard --top 25
start "" reports\jobscout-dashboard.html
```

`--output` defaults to `reports/jobscout-dashboard.html` (already git-ignored)
and `--top` to `25`. Forward slashes work on Windows too:
`--output reports/dashboard.html`.

What the report contains, per ranked offer:

- the score and the **model recommendation** (`apply` / `consider` / `skip` /
  `need_more_info`), with the scorer name, model, and confidence;
- title, company, location, posting age/date (falling back to first-seen when the
  posting date is unknown), content status, and the direct LinkedIn URL;
- every **source keyword** that surfaced the job and the **observed result rank**
  for that keyword, alongside the dedupe verdict;
- strengths, gaps, blockers, unknowns, and a bounded rationale.

Client-side controls filter and search the embedded data: free text over title,
company, and location; recommendation; minimum score; keyword; reviewed /
unreviewed state; and sorting by score or freshness. Summary counts are shown at
the top.

A **keyword performance** section is computed from stored search provenance and
the latest successful evaluation: sightings, unique jobs, duplicate sightings,
average score, scored/unscored counts, and apply/consider counts, plus two
top-10 tables (by score quality, and by positive recommendations) that only
include keywords the data can actually rank. Observed rank is shown as
provenance only — LinkedIn result order is *not* treated as a quality signal.

### Model recommendation vs. human feedback

These are two different things, and the report keeps them apart:

| | Model recommendation | Human feedback |
|---|---|---|
| Produced by | `score` (heuristic, LLM, or OpenCode scorer) | you, clicking in the report |
| Stored in | SQLite `evaluations` table, with full provenance | this browser's `localStorage`, keyed by project + job id |
| Visible to `jobscout` | yes | no — the CLI never reads it |
| Portable | yes, it is in the database | only via the **Export local feedback (JSON)** button |

Each offer has local **apply / consider / reject** buttons and an optional reason
box. Feedback survives a page reload in the same browser, and the export button
downloads a `jobscout-feedback.json` document (`jobscout.feedback.v1`) that can
be imported later. Feedback is **local only**: it does not submit an application,
does not touch LinkedIn, and does not write to the SQLite database. There is no
import yet — the export exists so the data can be carried forward.

Browser-local feedback is keyed by a hash of the database path (not the path
itself), so the report never contains an absolute local path and two databases
never share feedback. Some browsers restrict `localStorage` on `file://` pages;
if yours does, the page still renders and filters, but the export button is the
only way to keep your decisions. Serve the file from a loopback static server
(`python -m http.server`) if you need storage to persist there.

### What the report deliberately excludes

The report never embeds the job description, raw payloads, the candidate profile
or any contact data, API keys, or absolute local paths. It reads the database
with `PRAGMA query_only`, so generating it cannot insert, update, or delete a
row; only the output's parent directory is created. A missing database is an
error, while an empty or unscored one still produces a report with a visible
warning explaining why it is empty.

## Configuration

Every knob is a `JOBSCOUT_*` environment variable (see `.env.example`):

| Variable | Default | Notes |
|----------|---------|-------|
| `JOBSCOUT_DB_PATH` | `data/jobscout.sqlite3` | SQLite file |
| `JOBSCOUT_PROFILE_PATH` | `profile.example.yaml` | Candidate profile |
| `JOBSCOUT_HTTP_USER_AGENT` | `jobscout/0.1 (local research tool)` | Honest, non-spoofing |
| `JOBSCOUT_CONTACT_EMAIL` | – | Appended to the user agent |
| `JOBSCOUT_HTTP_TIMEOUT_SECONDS` | `20` | Per request |
| `JOBSCOUT_HTTP_MAX_RETRIES` | `2` | Transient failures only |
| `JOBSCOUT_REQUEST_DELAY_SECONDS` | `8` | Minimum spacing between requests |
| `JOBSCOUT_REQUEST_JITTER_SECONDS` | `2` | Random added spacing |
| `JOBSCOUT_MAX_PAGES` | `2` | Page cap; absolute upper bound `10` |
| `JOBSCOUT_MAX_ITEMS` | `40` | Item cap; absolute upper bound `250` |
| `JOBSCOUT_LINKEDIN_SEARCH_URL` | public guest search endpoint | Override only for testing |
| `JOBSCOUT_RAW_MAX_BYTES` | `262144` | Live search download ceiling and the size above which raw bodies are not stored |
| `JOBSCOUT_LINKEDIN_DETAIL_URL` | public guest detail endpoint | `{job_id}` placeholder; override only for testing |
| `JOBSCOUT_DETAIL_MAX_ITEMS` | `20` | Detail item cap; absolute upper bound `250` |
| `JOBSCOUT_DETAIL_TIMEOUT_SECONDS` | `20` | Per detail request |
| `JOBSCOUT_DETAIL_MAX_BYTES` | `524288` | Downloads above this are rejected, not stored (bodies are also capped by `JOBSCOUT_RAW_MAX_BYTES`) |
| `JOBSCOUT_LLM_BASE_URL` | – | Presence enables the LLM scorer |
| `JOBSCOUT_LLM_API_KEY` | – | Never logged or stored |
| `JOBSCOUT_LLM_MODEL` | `deepseek-v4.1-flash` | Configurable |
| `JOBSCOUT_LLM_TIMEOUT_SECONDS` | `60` | Per LLM request |
| `JOBSCOUT_LLM_MAX_RETRIES` | `2` | Transient failures only |
| `JOBSCOUT_LLM_TEMPERATURE` | `0` | Requested sampling temperature |
| `JOBSCOUT_LLM_MAX_TOKENS` | `1200` | Response token ceiling |
| `JOBSCOUT_LLM_STORE_RAW` | `true` | Raw responses, when safe |
| `JOBSCOUT_LLM_JSON_MODE` | `true` | Sends `response_format: json_object` |
| `JOBSCOUT_OPENCODE_COMMAND` | `opencode` | Absolute path or a name resolved through `PATH`; a relative path is refused |
| `JOBSCOUT_OPENCODE_MODEL` | `opencode-go/deepseek-v4.1-flash` | Passed as `--model` |
| `JOBSCOUT_OPENCODE_VARIANT` | `minimal` | Passed as `--variant`; empty omits the flag |
| `JOBSCOUT_OPENCODE_TIMEOUT_SECONDS` | `180` | Per CLI invocation; the child is killed on timeout |
| `JOBSCOUT_OPENCODE_WORKDIR` | system temp directory | Neutral cwd for the CLI |
| `JOBSCOUT_OPENCODE_MAX_OUTPUT_BYTES` | `1048576` | Child output ceiling, enforced while the process runs |
| `JOBSCOUT_OPENCODE_JSON_EVENTS` | `true` | Adds `--format json` for machine-readable events |
| `JOBSCOUT_OPENCODE_PURE` | `true` | Adds `--pure` (no external plugins) |

`JOBSCOUT_MAX_PAGES`, `JOBSCOUT_MAX_ITEMS`, and `JOBSCOUT_DETAIL_MAX_ITEMS` are
validated against `jobscout.config.HARD_MAX_PAGES` (`10`) and
`HARD_MAX_ITEMS` (`250`). Values above those ceilings — from an environment
variable, a CLI flag, or a library caller — are rejected with a clear error
instead of being silently clamped.

Precedence: CLI flags > environment > `.env` > defaults. A local `.env` is read
only for `JOBSCOUT_*` values and never overrides the real environment.

Note: `jobscout.config.redact_secrets` is a best-effort sanitizer applied to the
model-derived evaluation text that gets persisted and printed — the rationale,
dimension text, and the schema repair/error diagnostics — so a configured secret
or a labelled credential echoed by the model (in plain or JSON-quoted form)
cannot land in the `evaluations` table or the console. Raw payloads are protected
separately by `jobscout.config.is_safe_to_store`, which refuses to store a body
that is too large or credential-looking rather than rewriting it in place.

## Data model

| Table | Contents |
|-------|----------|
| `jobs` | Deduplicated postings: URL, source id, timestamps, content status, content hash, strong `fingerprint`, weak `dedupe_key`, sighting count |
| `search_runs` | One collector invocation: query, params, status, page/item counts, new/duplicate counts, error |
| `search_run_items` | One row per posting observed by a run, with its dedupe verdict |
| `job_detail_fetches` | One row per detail-fetch attempt: status, HTTP status, source URL, fetched timestamp, content hash, raw document id, error reason |
| `raw_documents` | Verbatim payloads with sha256, byte size, fetch/store timestamps, truncation flag |
| `profiles` / `profile_evidence` | Versioned profile snapshot plus one row per structured fact |
| `evaluations` | Score, dimensions, blockers/strengths/gaps/unknowns, confidence, recommendation, provenance |

Deduplication uses two keys: `fingerprint` (source job id → canonical URL →
company/title/location hash) for exact matches, and `dedupe_key` for probable
matches across different URLs or sources. Probable duplicates are merged into the
existing row as additional sightings, and richer content wins over empty content.

Inspect it with plain SQL:

```bash
sqlite3 data/jobscout.sqlite3 "SELECT id, title, company FROM jobs ORDER BY last_seen_at DESC LIMIT 5;"
```

## Tests

```bash
python -m pytest
```

The suite is fully offline: the HTTP transport is always replaced by a test
double (or the offline `--fixture` transport), tests that must prove no network
is reachable block `socket.socket` entirely, and the OpenCode CLI is replaced by
an injected fake runner — no test invokes a model or the real `opencode` binary.
Two process-safety tests do spawn a deterministic local Python child
(`sys.executable`) to prove the output ceiling kills an overflowing process and
that the timeout terminates a hung one; both are offline. Coverage includes
normalization, fingerprinting, deduplication, schema persistence,
rate-limit/backoff decisions, retry and fail-closed behaviour, URL and redirect
policy, bounded error-body reads, fixture ingestion, detail-page parsing, detail
content preservation, heuristic scoring, prompt hardening, malformed LLM output,
executable resolution, subprocess output bounds, and the OpenCode CLI scorer.
Dashboard coverage adds HTML escaping/XSS safety (a hostile job title cannot
close its script tag or reach the DOM as markup), data minimization (no
description, profile, absolute path, or raw payload), generation and read-only
behaviour, filter/feedback surface, keyword aggregation, and the CLI error and
success paths.

Run the CLI smoke test against the fixtures (the `data/` directory is
git-ignored, so the smoke database never gets committed):

```bash
python -m jobscout --db data/smoke.sqlite3 init-db
python -m jobscout --db data/smoke.sqlite3 ingest-fixture tests/fixtures/linkedin_search_cards_sample.json
python -m jobscout --db data/smoke.sqlite3 fetch-details --fixture tests/fixtures/linkedin_detail_responses.json
python -m jobscout --db data/smoke.sqlite3 --profile tests/fixtures/profile_test.yaml score --all --rescore
python -m jobscout --db data/smoke.sqlite3 rank --top 5
python -m jobscout --db data/smoke.sqlite3 dashboard --top 5 --output reports/smoke.html
```

Run a dry run of the LinkedIn plan (still no network):

```bash
python -m jobscout search-linkedin --keywords "senior backend engineer" --max-pages 2 --dry-run
```

## Known limitations / next steps

- Detail fetching only extracts descriptions from known guest-markup containers
  (`show-more-less-html__markup`, `description__text`, and similar). An unknown
  markup revision yields `partial` with an explicit reason rather than a bogus
  description.
- `fetch-details` does not re-score jobs by itself. Run
  `score --all --rescore` after enriching, or score specific jobs with
  `--job-id`.
- The search parser supports two guest-search card shapes: the historical
  identifying `<li>` and the current nested
  `div.base-search-card.job-search-card[data-entity-urn]`. A 200 response with a
  non-empty body but zero cards is recorded as a diagnostic note (with the body
  size), and markup changes never crash the run.
- The OpenCode CLI scorer requires a locally installed, authenticated `opencode`
  binary; jobscout does not manage that authentication. `JOBSCOUT_LLM_API_KEY`
  is not used for authentication, is not passed in argv or project files, is
  removed from the CLI's environment, and is redacted from captured output
  before persistence.
- **Residual risk (H2):** no reliable OpenCode `run` flag denies tools or
  sandboxes execution. jobscout runs the CLI with `--pure` by default, but a
  prompt-injected posting could still attempt tool calls allowed by your local
  OpenCode configuration. See "OpenCode tool permissions — residual risk (H2)"
  above.
- `JOBSCOUT_OPENCODE_COMMAND` must be an absolute path or a name resolvable
  through `PATH`; a relative path is refused so a planted executable in the
  working directory can never be used.
- Salary parsing is heuristic (largest number in the text) and only used when
  the profile defines a minimum.
- The dashboard embeds only the top `--top` offers, but keyword analytics are
  always computed over the whole database. Manual feedback lives in browser
  `localStorage` and there is no import command yet: the export JSON is the
  carry-forward format, and a future work unit can consume it.
- No scheduler, notification, or application automation — by design.
