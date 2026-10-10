# Ponytail audit application (2026-10-10)

## Objective
Apply the 2026-10-10 whole-tree ponytail audit (~-950 lines target, 0 deps), split into two batches, keeping the offline suite green (baseline: 1104 passed, 1 known env failure `test_resolve_executable_accepts_absolute_path_and_path_lookup`).

## Problem
Duplication and dead code accumulated: repeated CLI command bodies, copied detail-adapter plumbing, unused db/config/scoring symbols, single-implementation wrappers, hand-rolled stdlib equivalents.

## Scope
Batch 1 (safe deletes/folds): dead db imports & functions, dead notify vocab trio, dead to_dict methods, dead scattered symbols, write-only fields, one-implementation wrappers, dead config knobs, __all__ export noise, stdlib substitutions (dict.fromkeys, Template, Z-regex).
Batch 2 (needs care): parameterized CLI command runner (cli.py search/fetch bodies), shared detail-adapter plumbing module (_BlockParser/_VOID_TAGS/_sanitize_final_url/_has_class), _select_*_detail_jobs trio.
Rejected/skipped: config.py:100-102 item (audit text garbled — verify manually before touching); nothing here is needed for correctness.

## Constraints
- Suite stays offline; every batch must pass `python3 -m pytest -q` (expect ~1100 passed, only the 1 known env failure).
- Verify every audit claim (grep all references incl. tests) before deleting; add tests only for changed wiring behavior.
- No new dependencies. English code/comments, conventional commit, no AI attribution.

## Tasks
- [x] P-1 Verify + apply batch 1 (dead symbols, wrappers, config knobs, stdlib substitutions)
- [x] P-2 Run full suite after batch 1, commit
- [ ] P-3 Batch 2: shared detail-adapter plumbing module + _select_*_detail_jobs helper
- [ ] P-4 Batch 2: parameterized CLI search/fetch runner
- [ ] P-5 Full suite after batch 2, commit(s)
- [ ] P-6 Update READMEs if documented behavior changed

## Applied (Batch 1)
- Group A: dropped 5 unused `cli.py` db imports; deleted dead `db.py` functions `ensure_initialized`, `get_raw_document`, `get_job_variant`, `count_active_jobs`, `count_jobs_needing_details`, `get_profile` (no src/test callers).
- Group B: deleted `notify.PREFERENCE_CHOICES`/`DELIVERY_CHOICES`; dead `to_dict` on `PreferenceMatch`, `RetiredSearch`, `LLMResponse` (+ write-only `raw`/`usage`/`request_id`), `OpenCodeResponse`; dead `iter_profile_facts`, `ALLOWED_RECOMMENDATIONS`, `ParseOutcome.failed`/`raw_text`, `FeedbackImportOutcome.written`, `dedupe_batch` (+ test), `HttpResponse.elapsed_ms`/`error` (+ fixture transport arms), `DetailFixture.provider`/`fetched_at`.
- Group C: folded `sync_job_active` → `_sync_job_active`; `_salary_currencies` → `currency.salary_currencies`; inlined `is_public_recommendation`; replaced `TecnoempleoDetailConfig`/`GetManfredDetailConfig` with a validated plain `max_items` param.
- Group D: deleted `retry_on_rate_limit`/`retry_on_access_denied` knobs (+ test), unreachable `max_attempts` guard and `fetch_with_policy(max_attempts=)` param; removed fixture `missing_status`.
- Group E: `list(dict.fromkeys(...))` at preference (2) and heuristic (1); `_ISO_Z_RE` → `endswith(("Z","z"))` in timeutil.
- Group F: removed internal constants from `__all__` in url_policy, scoring/fingerprint, collectors/keyword_match.

## Skipped (claim wrong / out of scope)
- `dashboard.open_readonly_database`: not a one-implementation wrapper — it translates sqlite errors into `DashboardError` and guards `:memory:`; it is the public read-only entry used by `generate_dashboard` and imported by tests. Folding to `connect_readonly` would drop the actionable errors.
- `notify.NotificationSelection`: public return type of `select_notifications`, consumed directly by tests and `build_report`. Inlining to a tuple breaks the public API.
- `pipeline._default_run_params` fallback: reachable when `config is None` (`test_ingest_collector_result_without_config_defaults_to_linkedin`) and for configs without `to_run_params`; not dead.
- `linkedin.page_size` / `tecnoempleo.page_param`: read in src (`build_search_url`) and recorded in run params (`test_provenance`); not dead, so not hardcoded.
- config `NOTIFY_*`: verified all consumed (`DEFAULT_NOTIFY_SCORE_THRESHOLD`, `DEFAULT_NOTIFY_DELIVERY`, `NOTIFY_DELIVERY_CHOICES`, `NOTIFY_PREFERENCE_OUTCOMES` validated; `notify_enabled`/`notify_score_threshold`/`notify_preference_outcome`/`notify_delivery` read by notify.py/cli.py). Nothing to delete.
- `dashboard._render_template`: token delimiter is `__NAME__`, not `$name`; `string.Template` cannot express it without rewriting the whole HTML/CSS/JS template (and `$` collides with CSS/JS). Single-pass security property already holds.
- `robots.path_of`: imported by `tests/test_robots.py`; left importable per rule.

## Verification evidence
- Baseline: 1104 passed, 1 failed (pre-existing env failure).
- After Group A: 1104 passed, 1 failed (known env).
- After Group B: 1103 passed, 1 failed (known env; -1 deleted `dedupe_batch` test).
- After Group C: 1101 passed, 1 failed (known env; -2 deleted detail-config tests).
- After Group D: 1100 passed, 1 failed (known env; -1 deleted rate-limit retry test).
- After Group E: 1100 passed, 1 failed (known env).
- After Group F: 1100 passed, 1 failed (known env).
- Net: 29 files changed, 49 insertions(+), 358 deletions(-). The only failure is the known environmental `test_resolve_executable_accepts_absolute_path_and_path_lookup`.

## Next step
Delegate Batch 2 (P-3/P-4).
