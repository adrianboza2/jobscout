# Seniority pre-filter

## Objective
Offers whose title is clearly senior are marked filtered, with the reason, are never
sent to the scoring model, and are hidden in the dashboard behind a visible count
with a toggle. Nothing is deleted. Source: NEXT_STEPS.md, 2026-10-23 "Next" item 2,
scheduled as item 1 on 2026-10-27.

## Owner decisions (2026-10-27)
- Design A: the filter is a pure function computed at read time (like
  `preference.py`), not stored in SQLite. No schema change, no migration.
- **Reverses CP-10** (`odd/tasks/done/catalog-pruning.md`, 2026-09-30): the owner
  withdraws the rule "the dashboard shows every active offer" and the ban on a
  title seniority heuristic. The CP-10 regression test, the README sections
  "Sin filtro de seniority por título" / "No title-based seniority filter", and
  the warning in NEXT_STEPS.md (Priority 2 area) are replaced, not kept.
- Why it is acceptable now (measured 2026-10-27, read-only, 758 active offers,
  latest ok score): filtered LinkedIn 93/460, Tecnoempleo 55/179, GetManfred
  17/119; **0** filtered offers score >= 50 or are marked apply/consider. CP-9
  failed on GetManfred (26 good hidden) because it had no junior exceptions and
  matched `sr` inside `SRE`.

## Rule (seam: `src/jobscout/seniority.py`)
`seniority_filter_reason(title: str | None) -> str | None`

- Case-insensitive, whole-word match (word boundaries, accents tolerated where
  listed). Senior markers: `senior`, `sr`, `lead`, `principal`, `architect`,
  `arquitecto`, `arquitecta`, `manager`, `head`, `director`, `staff`, `jefe`.
- Exceptions (any of them in the title cancels the filter): `junior`, `jr`,
  `trainee`, `beca`, `becario`, `becaria`, `prácticas`/`practicas`, `intern`,
  `internship`.
- Returns `"title: <first senior marker found, lowercased as written in the
  list>"`, or `None` when not filtered. `None`/empty title → `None`.
- `sr.` with a dot still counts (`Sr. Cloud Engineer`); `SRE`, `Srv`, `Leader`
  inside other words, `Headquarters`, `Staffing` do not.

Expected values (from the spec, computed by hand):

| Title | Result |
|---|---|
| `Senior Platform Engineer` | `title: senior` |
| `Sr. Cloud Engineer` | `title: sr` |
| `SRE Engineer` | `None` |
| `Cloud Security Architect` | `title: architect` |
| `Arquitecto Cloud Azure` | `title: arquitecto` |
| `Jefe de Proyecto IT` | `title: jefe` |
| `Engineering Manager` | `title: manager` |
| `Staff Engineer` | `title: staff` |
| `Junior Lead Developer` | `None` |
| `Beca Arquitecto de Sistemas` | `None` |
| `Prácticas Senior Analyst` | `None` |
| `Senior Intern Program` | `None` |
| `Técnico de Sistemas` | `None` |
| `Staffing Coordinator` | `None` |
| `Headquarters IT Support` | `None` |
| `Team Leader Helpdesk` | `None` (`leader` is not `lead`) |
| `` / `None` | `None` |

## Scoring (`jobscout score`)
- Without `--job-id`, both selections (`jobs_needing_evaluation` and
  `--rescore` → `all_active_jobs`) leave filtered offers out **before** `--limit`
  is applied: `--limit N` returns up to N offers that will be sent. Reason: the
  Hermes sweep runs `score --all --limit 180`; filtered offers must not use up
  that cap.
- The command prints one line `skipped N offer(s) filtered by seniority` (text
  output) and a `skipped_seniority` count in `--json` output when N > 0.
- `--job-id` scores the listed offers even when they are filtered (explicit
  owner request).
- Existing evaluations of filtered offers are kept untouched.

## Dashboard
- Every active offer stays in the payload (`jobs_shown == len(offers)` still
  holds); each offer carries `seniority_filter`: the reason string or `null`.
- `summary.seniority_filtered`: number of offers with a reason.
- The client hides filtered offers by default. The rail shows the count
  ("N ocultas por seniority") and a keyboard-accessible toggle button
  (`#filter-show-seniority`, `aria-pressed`) to show them; the reset action hides
  them again. A shown filtered offer displays its reason in its row.
- Spanish UI text, same visual system as the existing rail filters. The report
  stays self-contained and works over `file://`.

## Constraints
- No network, no schema change, no change to collectors, pacing, caps or the
  scorer prompt. No rows deleted or deactivated.
- Style per AGENTS.md: comments say why, short docstrings, no new dependencies.

## Tasks
- [x] `seniority.py` with the rule and the table above as tests (`tests/test_seniority.py`).
- [x] Score selection skips filtered offers before the limit; `--job-id` forces; skipped count printed.
- [x] Dashboard payload field, summary count, hidden-by-default toggle; replace
      `test_dashboard_has_no_senior_noise_filter` with tests for the new behaviour.
- [x] README.md and README.en.md: replace the "no title filter" section with the
      new behaviour (both languages); update the scoring section; NEXT_STEPS warning
      replaced by a pointer to this card.
- [x] Full offline suite green.

## Success command
    .venv/bin/python -m pytest -q tests/test_seniority.py tests/test_cli.py tests/test_db.py tests/test_dashboard.py
    .venv/bin/python -m pytest -q        # acceptance: full suite, 0 failed

## Progress
- 2026-10-27: Added `src/jobscout/seniority.py` and `tests/test_seniority.py`
  (spec table, `None`/empty, casing, accentless `practicas`).
- 2026-10-27: `score --all` and `--rescore` filter senior titles before `--limit`;
  `--job-id` forces scoring; text prints `skipped N offer(s) filtered by
  seniority` and JSON carries `skipped_seniority`. New CLI tests cover both
  selections, the text line and the `--job-id` override.
- 2026-10-27: Dashboard payload carries `seniority_filter` per offer and
  `summary.seniority_filtered`; the rail has `#filter-show-seniority` with
  `aria-pressed`, hidden by default and reset to hidden. Replaced
  `test_dashboard_has_no_senior_noise_filter` with payload/HTML tests.
- 2026-10-27: README.md, README.en.md and the NEXT_STEPS warning were updated.
  Updated existing CLI/output expectations from 4 to 3 scored offers because the
  shared fixture's "Senior Backend Engineer" is now filtered.
- 2026-10-27: `.venv/bin/python -m pytest -q tests/test_seniority.py tests/test_cli.py tests/test_db.py tests/test_dashboard.py`
  and `.venv/bin/python -m pytest -q` were attempted but the execution
  environment denied the shell command (`Permission denied: shell`), so no test
  counts are available and task 5 stays unchecked.
- 2026-10-27 (orchestrator review): `tests/test_detail_fetcher.py` smoke test
  updated for the same reason (its LinkedIn fixture has "Senior Backend
  Engineer": 2 scored, 1 skipped, rank shows 2). Focused command → 236 passed;
  `.venv/bin/python -m pytest -q` → **1192 passed**; `git diff --check` clean.
