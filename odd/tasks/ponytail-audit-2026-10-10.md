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
- [x] P-3 Batch 2: shared detail-adapter plumbing module + _select_*_detail_jobs helper
- [x] P-4 Batch 2: parameterized CLI search/fetch runner
- [x] P-5 Full suite after batch 2, commit(s)
- [x] P-6 Update READMEs if documented behavior changed — command names, flags and help strings are unchanged, so no README edit was needed.

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

## Applied (Batch 2)
- Commit 1 `refactor(collectors): extract shared detail-adapter helpers` (400de90):
  - New `src/jobscout/collectors/htmlutil.py` with the one genuinely identical `VOID_TAGS` set (was copied verbatim in linkedin.py, linkedin_details.py, tecnoempleo.py, tecnoempleo_details.py) and a parameterized `sanitize_redirect(requested, response, *, allow_host, require_https)`.
  - `_sanitize_final_url` in tecnoempleo_details.py and getmanfred_details.py, and the public `sanitize_final_url` in linkedin_details.py, now delegate to `sanitize_redirect` with their own host/https policy, preserving each provider's exact edge cases (GetManfred adds `require_https=True`; Tecnoempleo adds `allow_host=is_tecnoempleo_host`; LinkedIn adds neither).
  - `_select_tecnoempleo_detail_jobs` / `_select_getmanfred_detail_jobs` collapsed into one-parameter wrappers over `_select_detail_jobs(source, provider_label, limit, refresh_cutoff)`; LinkedIn keeps the `--refresh` branch and stays the default (`source="linkedin"`), so its existing tests call it unchanged.
  - Net: 7 files changed, 137 insertions, 179 deletions.
- Commit 2 `refactor(cli): parameterize repeated search/fetch command bodies`:
  - One `_run_search(args, settings, config, plan_fn, refusal_message, collect_fn, label)` skeleton for the three `cmd_search_*` bodies (dry-run → confirm gate → collect/ingest → emit → exit code). Each `cmd_search_*` now builds only its config and refusal text.
  - One generic `_run_detail_dry_run(max_items, select_fn, plan_fn, unavailable_label, refresh_cutoff)` plus `_run_detail_fetch(...)` and `_finish_detail_run(...)` cover all three `cmd_fetch_*` bodies. Provider specifics (fixture transport vs `robots_text`, provider slug, `--refresh` fields, refusal wording, skipped-id wording) are passed in.
  - Command names, flags, help strings, exit codes, JSON payload shape and error wording are byte-identical; README untouched.
  - Net: 1 file changed, 235 insertions, 372 deletions.

## Skipped (Batch 2 claim wrong / forced)
- `_has_class` / `_extract_field` "duplicated across tecnoempleo.py/getmanfred.py/getmanfred_details.py": false. `_has_class`/`_extract_field` live only in tecnoempleo_details.py; `tecnoempleo.py` uses a different subset check (`set(CARD_CLASS_TOKENS) <= set(classes.split())`) and GetManfred parses JSON-LD, not BEM classes. Nothing identical to extract.
- `_BlockParser` / `_BlockCaptureParser` "merged scaffolding": NOT identical. `_BlockCaptureParser` seeds `_parts` with the container's own start tag and captures raw HTML strings (cards need the container `onclick` URL); `_BlockParser` drops the container tag and captures `(text, raw_html)` tuples. Unifying would change parsed output, so both stayed.
- `resolve_detail_url` host checks (tecnoempleo_details/getmanfred_details) look like `_sanitize_final_url` but are selection-time URL validators (path prefix + job-id extraction), not redirect sanitizers; no shared core.

## Verification evidence (Batch 2)
- Baseline before batch 2: 1100 passed, 1 failed (known env).
- After Task 1 (commit 1): 1100 passed, 1 failed (known env).
- After Task 2 (commit 2): 1100 passed, 1 failed (known env).
- No tests added or deleted in batch 2. The only failure remains `tests/test_opencode.py::test_resolve_executable_accepts_absolute_path_and_path_lookup`.
- Diff stats: Task 1 `7 files changed, 137 insertions(+), 179 deletions(-)`; Task 2 `1 file changed, 235 insertions(+), 372 deletions(-)`.

## Byte-level risk notes (Batch 2)
- `sanitize_redirect` was written to reproduce each caller's exact check order. GetManfred checks https before credentials; Tecnoempleo and LinkedIn do not check https. Passing `require_https`/`allow_host` per caller preserves the old return values for every branch; the shared function's unit test (`test_sanitize_final_url_keeps_only_safe_redirects`) still passes.
- `_run_search` now passes `label=None` explicitly for LinkedIn search (previously omitted). `ingest_collector_result` treats an omitted label and `label=None` identically (`if label is None:` branch), verified by the unchanged CLI tests.
- `_run_detail_dry_run`/`_finish_detail_run` add `**_refresh_output_fields(args, refresh_cutoff)` for the Tecnoempleo/GetManfred paths; those namespaces have no `refresh` attribute, so it returns `{}` and the JSON output is unchanged.
- `_run_detail_fetch` passes `provider=None` for LinkedIn, which `ingest_detail_results` coerces to `"linkedin"` — same value as before.

## Next step
Batch 2 complete; both commits are local and unpushed.

