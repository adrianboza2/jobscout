# AGENTS.md — jobscout

Private, local-first job-search tool (`collect → enrich → score → dashboard`). No CI, no
publish pipeline, no service. Everything runs from the repository root.

## Environment (Windows)

- Use the project virtualenv: `.venv\Scripts\python.exe` (created with `python -m venv .venv`
  and `pip install -e ".[dev]"`). It is gitignored.
- Without a venv, `PYTHONPATH=src python -m jobscout <cmd>` works: the runtime is stdlib
  (`sqlite3`, `urllib.request`, `argparse`) plus `PyYAML`.
- Keep dependencies minimal on purpose. Do not add HTTP clients, test frameworks, linters,
  formatters, CI config, or task runners. No mutation-testing, no coverage gates.

## Commands

    .venv\Scripts\python.exe -m pytest -q          # offline suite, ~15-20 s, expect ~1100 passed
    .venv\Scripts\python.exe -m jobscout --help
    .venv\Scripts\python.exe -m jobscout keyword-plan          # offline, no network
    .venv\Scripts\python.exe -m jobscout rank --top 10         # reads data/jobscout.sqlite3

The full offline suite is the acceptance gate for any change.

## Windows: Codex sandbox vs pytest (read this before reporting failures)

Inside the Codex Windows sandbox, pytest errors out in fixture setup:

    PermissionError: [WinError 5] Acceso denegado:
    'C:\Users\adria\AppData\Local\Temp\pytest-of-adria'

Cause: pytest creates its base temp directory with mode `0o700`, which becomes an
owner-only Windows ACL; the sandbox runs as a separate identity
(`CodexSandboxOnline`/`CodexSandboxUsers`), so it cannot reopen or scan it. Fixing the ACL
does not stick — pytest recreates it — and moving it into the repo with `--basetemp` only
moves the problem and leaves directories the real user cannot read afterwards.

- Measured: same command, same commit — sandboxed run `592 passed, 408 errors`; with host
  access (`--sandbox danger-full-access`) `1000 passed in 17.97s`.
- Therefore: when tests must actually run, use Codex with host access, or ask the user to run
  them. Never treat that PermissionError as a defect of this repository and never "fix" tests
  or code because of it.

## Project contract (do not break)

- The suite is offline by design: no network (some tests block `socket.socket`), no real model
  calls, no real `opencode` binary. Never add a test that touches the network.
- Network lives only in the collectors: `robots.txt` is re-read before the first request,
  pacing is 8 s + jitter, failures are fail-closed, and `--confirm-network` is the human gate.
  Do not lower pacing or item/page caps, do not add keyword fan-out, do not retry loops on
  429/403 — report how far the run got instead.
- Fixtures are synthetic. A green suite proves nothing about a live provider; any claim about
  a collector must be backed by live markup captured once and pushed through the project's own
  parsing function, offline.
- Never touch authenticated/private provider pages (for example InfoJobs `candidate/...`):
  those adapters are blocked on purpose.
- The scorer reads stored text only; it never fetches a URL. New data comes from detail
  adapters that fetch once and persist (`fetch-*-details` / `fetch-details`).
- A definitive 404/410 deactivates an offer (`is_active = 0`) and keeps its history; it never
  deletes rows. Refreshing only overwrites fields with non-empty values.
- Keep the HTML report self-contained, offline, and safe to open over `file://`.

## Local data — never publish

`data/`, `reports/`, `profile.yaml`, `.env`, `jobscout-feedback.json`, captured pages and
databases are machine-local and gitignored. Do not read them into commits, do not move them,
do not push them. Do not push at all unless the user asks.

## Style

- Code, docstrings, comments and commit messages in English; the READMEs are bilingual
  (`README.md` in Spanish, `README.en.md` in English) — update both when documented behaviour
  changes.
- Comments explain why, not what. Blank line inside functions to break logical blocks, short
  docstrings, no speculative abstractions.
- Conventional commit subjects, no AI attribution. Code + tests + docs go in the same commit.
  Do not commit unless asked.

## Collection policy (deliberate deviations — these override the contract above where they conflict)

- LinkedIn: the adapter must not consult `robots.txt` before searching or before fetching
  details. This is an explicit owner decision (2026-09-28), taken with its cost — the site may
  answer 403/429 and the IP may be blocked. Do not re-add a robots gate to the LinkedIn
  collector, do not make it configurable "for safety", and do not treat its absence as a bug to
  fix: an audit finding is not a reason to change it. LinkedIn requests stay bounded by the same
  pacing and item caps as every other provider.
- Tecnoempleo and GetManfred keep their current robots handling. Do not touch it.
