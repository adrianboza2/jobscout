# Score calibration

## Objective
Provide an offline, explainable comparison between the latest model evaluations
and imported human decisions without changing scores automatically.

## Scope
- Add a CLI report for model-vs-human agreement.
- Compare `apply`, `consider`, and `skip` recommendations with human
  `apply`, `consider`, and `reject` decisions.
- Keep `need_more_info` visible but separate from the three-way agreement rate.
- Report confusion counts and score distributions by human decision.
- Do not mutate evaluations, prompts, profile data, or feedback during reporting.

## Checks
- Focused calibration tests.
- Full offline pytest suite.
- Run the report against the local database after importing feedback.

## Progress
- [x] Import the existing dashboard export idempotently.
- [x] Implement the calibration report.
- [x] Verify and review the final diff.
- [ ] Create a local commit; do not push.

## Implementation

### SC-1 — calibration report module (`src/jobscout/calibration.py`)
- `build_report(conn, *, scorer=None)` reads every stored human decision and
  joins it to that job's latest **valid** evaluation (`status = 'ok'`) with a
  correlated `LEFT JOIN ... ON e.id = (SELECT MAX(x.id) ...)`. Pass a `scorer` to
  restrict the lookup to one backend; omit it to compare each job's latest valid
  evaluation regardless of scorer.
- The only bridge between the two vocabularies is the explicit
  `MODEL_TO_HUMAN = {apply: apply, consider: consider, skip: reject}`.
  `equivalent_human_decision()` returns `None` for anything outside that map.
- `need_more_info` is never listed in the map: it is counted in `unresolved`
  (keyed by the raw recommendation) and `unresolved_total`, excluded from the
  three-way agreement and from the confusion matrix. It can never become `skip`.
- The report exposes: feedback total, matched, `without_evaluation`, comparable
  samples, `agreement` (`exact` / `comparable` / `rate`), unresolved counts, a
  3×3 confusion matrix (`apply`/`consider`/`skip` × `apply`/`consider`/`reject`),
  and the `overall_score` distribution by human decision (`count`, `with_score`,
  `min`, `max`, `mean`, ten-point buckets). Per-job samples are included as
  evidence. `to_text()` and `to_dict()` are the two renderings.
- The module issues only `SELECT` statements and requires no schema change.

### SC-2 — CLI command
- New `calibrate [--scorer heuristic|llm|opencode]` subcommand registered in
  `build_parser`, handled by `cmd_calibrate`.
- It opens the database through `connect_readonly` (`mode=ro` + `PRAGMA
  query_only`), never calls `init_db`, and writes nothing. A missing database is
  a clear `EXIT_ERROR`; an initialized but empty database still renders a zero
  report. A file without the jobscout schema raises `CalibrationError`, reported
  as `calibration error: ...`.

### SC-3 — tests and docs
- `tests/test_calibration.py`: mapping, latest-valid selection, failed-evaluation
  exclusion, three-way confusion, `need_more_info` never becoming `skip`, scorer
  filtering, scorer with no evaluations, no feedback, feedback without a valid
  evaluation, score distribution, read-only guarantee (`conn.total_changes`),
  missing schema, and CLI text/JSON/missing/empty/uninitialized/read-only cases.
- `README.md` and `README.en.md`: CLI reference row plus a basic-usage section
  (mapping table, read-only/offline statement, unresolved-category note).

## Verification (observed)
- `py -m pytest tests/test_calibration.py -q` → `23 passed in 0.63s`
- `py -m pytest tests/test_calibration.py tests/test_cli.py tests/test_feedback.py tests/test_db.py -q` → `97 passed in 2.90s`
- `py -m pytest -q` → `695 passed in 9.73s`
- `py -m jobscout calibrate --db <copy of data/jobscout.sqlite3>` (snapshot copy,
  read-only) → `29 feedback row(s), 29 comparable, 0 unresolved, 0 without a
  valid evaluation; agreement 3/29 exact (10.3%)`. The original `data/` file was
  never modified: no `-wal`/`-shm`/`-journal` sidecar appeared and its mtime was
  unchanged. No feedback was re-imported.
- The local corpus contained no `need_more_info` recommendations, so the
  unresolved path is proven by `tests/test_calibration.py`
  (`test_need_more_info_is_unresolved_and_never_becomes_skip`,
  `test_need_more_info_is_excluded_from_the_three_way_rate`).
- No commit and no push were performed; the working-tree changes remain local.

## Next step
- Run `calibrate` after the next scoring + feedback round and use the confusion
  matrix as evidence before changing prompts or weights (the report never does).
