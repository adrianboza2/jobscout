# AGENTS.md — jobscout

Private, local-first job-search tool (`collect → enrich → score → dashboard`). No CI, no
publish pipeline. Everything runs from the repository root.

## Who decides

The owner leads this project and wants to understand every change and the reason for it. The
code grew fast with little owner design input, so:

- Propose, explain the why, and let the owner decide anything that changes behaviour, data,
  providers, models or scope. Give a recommendation, not a menu.
- Keep steps small enough to review. No sweeping refactors the owner has not asked for.

## Agent workflow (Claude Code)

The orchestrator's context is the scarce resource. Anything that is mechanical, or that only
adds reading to the orchestrator without adding judgment, goes to DeepSeek and comes back as
an answer only.

- **Delegate by default** with the `deepseek-delegate` skill:
  - `--agent explorador` for read-only questions: locate usages, map a module, summarise
    files, check whether something exists, with `path:line`.
  - `--agent ejecutor` (with `--skill tdd` for logic) for bounded, verifiable implementation
    with an exact success command.
  - The delegate may read and edit `.env` (owner decision, 2026-10-20); key files
    (`auth.json`, `*.pem`, `*.key`) stay blocked.
- **Keep for the orchestrator:** design decisions, briefs, reviewing every delegated diff,
  running the test gate, and talking to the owner. Trivial one-liners are cheaper to do than to
  brief. When something is not delegated, say so and why.
- `.claude/settings.json` injects this rule on every prompt so it is not forgotten.

### Orchestrator and worker sessions

The owner opens one orchestrator session, and every board task runs in its own **worker
session**: a full Claude Code session with `deepseek-flash` as its main model, through the
LiteLLM router (owner, 2026-11-10), not an Agent-tool subagent. The orchestrator launches, watches,
reviews and merges workers with the user-level `orquestador` skill; the owner does not paste
prompts (owner decision, 2026-11-04). What is specific to this repo:

- **Board:** workers run the main checkout's CLI against the main board:
  `python3 /home/adrian/jobscout/.agents/skills/kanvas/kanvas.py <cmd> -b /home/adrian/jobscout/kanvas.json --as claude-code-<id>`.
- **Worktree:** every worker gets its own (`../jobscout-<id>`). Tasks that depend on each other
  run one after another: the next is launched once the previous one is merged. The local
  `.git/hooks/post-checkout` links `.env` and `profile.yaml` into new worktrees; the database
  is not linked, so live checks there use a copy made with sqlite3's backup API. Run the CLI as
  `PYTHONPATH=src /home/adrian/jobscout/.venv/bin/python -m jobscout`, because the shared
  venv's editable install points at the main checkout; pytest already picks the worktree's
  `src` (`/home/adrian/jobscout/.venv/bin/python -m pytest -q`).
- **Merge:** for a task the owner approved on the board, the orchestrator commits, merges and
  pushes `master` without asking once the review step of the `orquestador` skill passes; it asks first only when a
  behaviour, data, provider or scope decision is still open (owner, 2026-11-04). Linear
  history: commit on the branch, fast-forward `master`, rebase the next branch onto it, and
  run the full suite on `master` after the last merge, then `git push origin master`. The main
  checkout is what the crons and the dashboard run, so a merge to `master` is production
  (owner, 2026-11-12).
- **Hermes sweep script** (outside the repo): changed once, after merging, by the orchestrator,
  never by workers.

## Environment

Main machine: Linux (`/home/adrian/jobscout`).

- Project virtualenv: `.venv/bin/python` (created with `python3 -m venv .venv` and
  `pip install -e ".[dev]"`). It is gitignored.
- Without a venv, `PYTHONPATH=src python3 -m jobscout <cmd>` works: the runtime is stdlib
  (`sqlite3`, `urllib.request`, `argparse`) plus `PyYAML`.
- Keep dependencies minimal on purpose. Do not add HTTP clients, test frameworks, linters,
  formatters, CI config, or task runners. No mutation-testing, no coverage gates.

## Commands

    .venv/bin/python -m pytest -q                 # offline suite, ~70 s here, expect ~1100 passed
    .venv/bin/python -m jobscout --help
    .venv/bin/python -m jobscout keyword-plan      # offline, no network
    .venv/bin/python -m jobscout rank --top 10     # reads data/jobscout.sqlite3

The full offline suite is the acceptance gate for any change.

## Outside this repository (part of the system, not versioned here)

- `~/.hermes/scripts/jobscout_sweep.py` — Hermes' scheduler runs the full cycle every three
  days at 07:00: LinkedIn details → searches (LinkedIn per catalog query, skipping
  `retired_for` pairs; Tecnoempleo runs the function browse and its text queries from the
  `tecnoempleo` block of `keyword-plan --json`) → Tecnoempleo/GetManfred details → `score --all` →
  dashboard → DB backup. The sweep budget is 8400 s and scoring takes what is left: its
  `--limit` is `(remaining - 180 s) / 16 s` (about 450 offers), logged as `[scoring: ... cap N]`
  (owner, 2026-10-29). Hermes kills cron scripts after `cron.script_timeout_seconds`
  (`~/.hermes/config.yaml`, raised from the 3600 s default to 9000), so the budget must stay
  below it. Delivery is `local`: the owner wants no Telegram notification. It writes
  `reports/sweep-YYYY-MM-DD.log`; read that log first when something "stopped working".
- `~/.hermes/scripts/jobscout_linkedin_details.py` — Hermes job `jobscout-linkedin-details`
  (`0 19 2-31/3 * *`, ~36 h after each sweep, delivery `local`): runs the LinkedIn and
  Tecnoempleo detail batches (`jobscout_sweep.py --linkedin-details`) so the backlogs drain
  without stacking more requests on the sweep's searches; `score` skips offers still waiting
  for details. Pacing stays at 8 s (owner, 2026-10-20).
- `~/.config/systemd/user/jobscout-dashboard.service` — serves `reports/` over Tailscale on
  port 8080.
- OpenCode (`~/.local/bin/opencode`) is updated outside this repo. A CLI update broke scoring
  on 2026-10-18 (removed `--pure`/`--variant`); after any OpenCode update, check the next
  sweep log for `scored N job(s), 0 succeeded`.

## Scoring with OpenCode 2.x

- Invocation: `opencode run --standalone --model <provider/model#variant> --format json
  <prompt>`. There is no `--pure` or `--variant` flag any more.
- `JOBSCOUT_OPENCODE_PURE=true` (default) now means isolation by environment: a private
  config dir next to the database (`data/opencode-config`) plus
  `OPENCODE_DISABLE_PROJECT_CONFIG`, `OPENCODE_DISABLE_AUTOUPDATE` and
  `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS`, so user plugins never run inside the scorer.
- Tools: the private `opencode.json` carries OpenCode 2.x `permissions` rules. The model
  keeps `read`, `grep`, `glob` and `webfetch` (owner, 2026-10-23: they may be useful);
  shell, write, edit, execute, subagent, skill, question, websearch, external_directory,
  doom_loop and reads of secret files are denied. It runs from an empty
  `data/opencode-work`. Verified live: `policies` does not remove tools, and a catch-all
  `*` deny hangs every run; any rule left at "ask" also hangs a non-interactive run.
- Variants depend on the model (`deepseek-v4.1-flash`: `low`/`high`/`max`, no `minimal`).
  Probe a new model once by hand before switching; a missing variant fails with
  `Variant unavailable`.
- `JOBSCOUT_OPENCODE_FALLBACK_MODEL` (optional): after the first provider `error` in a
  `score` run, the rest of that run uses the fallback. Free models can run out; this keeps the
  sweep producing scores. Changing model re-scores offers gradually because the model is part
  of each evaluation's input fingerprint.

## Planning files

- `kanvas.json` — the Kanvas board, the **only** list of pending work (owner, 2026-10-31).
  Change it only through `.agents/skills/kanvas/kanvas.py` (the `kanvas` skill); new work is
  proposed there, never added to `NEXT_STEPS.md`.
- `NEXT_STEPS.md` — session log only, newest first: what was done and why. Its old
  "Pending"/"Next" lists are history; what is still open is on the board.
- `odd/tasks/<topic>.md` — detailed work orders a board card may point to; `odd/tasks/done/` —
  closed ones, kept for history.

## Windows / Codex notes (second machine)

On Windows use `.venv\Scripts\python.exe`. Inside the Codex Windows sandbox, pytest fails in
fixture setup with `PermissionError: [WinError 5] Acceso denegado: ...\pytest-of-adria`:
pytest's `0o700` temp dir becomes an owner-only ACL the sandbox identity cannot reopen.
Measured on the same commit: `592 passed, 408 errors` sandboxed versus `1000 passed` with
host access. Run tests with host access (`.codex/config.toml`) or ask the user to run them;
never "fix" code or tests because of that error.

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
- Posting data comes from detail adapters that fetch once and persist
  (`fetch-*-details` / `fetch-details`). The scorer may use the internet when it improves
  results (owner, 2026-10-23), e.g. `webfetch` or a cached per-company dossier; it never
  replaces the stored posting text.
- A definitive 404/410 deactivates an offer (`is_active = 0`) and keeps its history; it never
  deletes rows. Refreshing only overwrites fields with non-empty values.
- Keep the HTML report self-contained, offline, and safe to open over `file://`.

## Local data — never publish

`data/`, `reports/`, `profile.yaml`, `.env`, `jobscout-feedback.json`, captured pages and
databases are machine-local and gitignored. Do not read them into commits, do not move them,
do not push them. Push only `master`, and only as the orchestrator's last step for an approved
board task, or when the user asks.

## Style

- Code, docstrings, comments and commit messages in English. The technical manual is bilingual
  (`docs/MANUAL.md` in Spanish, `docs/MANUAL.en.md` in English) — update both when documented
  behaviour changes. `README.md` is a short plain-Spanish overview for non-technical readers:
  touch it only when what the tool does, or why, changes.
- Comments explain why, not what. Blank line inside functions to break logical blocks, short
  docstrings, no speculative abstractions.
- Conventional commit subjects, no AI attribution. Code + tests + docs go in the same commit.
  Commit only when asked; an approved board task merged by the orchestrator counts as asked
  (see "Orchestrator and worker sessions").

## Collection policy (deliberate deviations — these override the contract above where they conflict)

- LinkedIn: the adapter must not consult `robots.txt` before searching or before fetching
  details. This is an explicit owner decision (2026-09-28), taken with its cost — the site may
  answer 403/429 and the IP may be blocked. Do not re-add a robots gate to the LinkedIn
  collector, do not make it configurable "for safety", and do not treat its absence as a bug to
  fix: an audit finding is not a reason to change it. LinkedIn requests stay bounded by the same
  pacing and item caps as every other provider.
- Tecnoempleo and GetManfred keep their current robots handling. Do not touch it. (Known fact,
  not a bug to fix without the owner: the GetManfred sitemap inventory relies on a past reading
  of its `robots.txt`; only the detail adapter re-reads it at run time.)
