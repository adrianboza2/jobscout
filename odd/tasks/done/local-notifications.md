# Local notifications

## Objective
Add an explicitly enabled, deterministic, offline notification preview for
high-value active offers that have a stored successful score, pass the optional
preference filter, and have no imported human decision.

## Scope
- Add read-only notification selection and local stdout/system delivery.
- Keep notifications disabled by default and require an explicit delivery flag.
- Reuse stored evaluations and deterministic preference assessment; never call a
  model, access the network, write SQLite, or apply to an offer.
- Add configuration, CLI JSON output, tests, and synchronized English/Spanish
  documentation.

## Constraints
- No scheduler, daemon, cron hook, background service, or automatic execution.
- No new runtime dependencies; stdlib plus the existing PyYAML dependency only.
- Touch only the files authorized for this work unit; do not modify
  `src/jobscout/dashboard.py`.
- Do not commit or push.

## Tasks
- [x] Add deterministic notification selection and local delivery fallback.
- [x] Add disabled-by-default configuration and `.env.example` documentation.
- [x] Add the `notify` CLI command with preview-first and `--json` behavior.
- [x] Add focused tests for selection, human-decision exclusion, disabled default,
  no SQLite writes, and no network.
- [x] Update Priority-5 documentation in both README files.
- [x] Run focused and full test suites plus the CLI help smoke check.
- [ ] Create a local commit; do not push. — deferred: user explicitly said not
  to commit, so this work remains uncommitted and reviewable.

## Progress

Observed results (single writer, no commit):

- `python -m pytest tests/test_notify.py tests/test_config.py tests/test_cli.py -q`
  → **135 passed** (no failures, no skips).
- `python -m pytest -q` → **913 passed** (no failures, no skips).
- `PYTHONPATH=src python -m jobscout notify --help` → exit **0** (usage lists
  `--db`, `--profile`, `--json`, `--limit`, `--deliver`).

Notes on the checks:

- Notifications remain **off by default** (`JOBSCOUT_NOTIFY_ENABLED=false`). The
  disabled CLI path returns success with an honest disabled report and does not
  create or open a database (asserted by tests, and by the absent file).
- No test starts a process or opens a socket: the system-notifier path injects a
  fake sender or patches discovery, and the enabled CLI test uses the
  `conftest.no_network` helper.
- The read-only guarantee is asserted by snapshotting the database bytes and
  directory entries and by a direct write attempt that SQLite refuses on the
  `connect_readonly` connection.
- Feedback exclusion uses one bounded `job_feedback` lookup for the scored
  candidate set, not a per-offer query or model fan-out.
- There is no scheduler, daemon, cron, background service, auto-run, remote
  delivery, or application action; `--deliver` is the only path that can reach a
  local notifier, and it falls back to stdout.
