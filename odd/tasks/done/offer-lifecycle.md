# Offer lifecycle

## Objective
Make definitive LinkedIn detail failures (404/410) deactivate offers without
deleting their historical evaluations, while allowing a later re-sighting to
reactivate the offer.

## Problem
The database already stores `jobs.is_active`, but production code never marks a
job inactive. The detail fetcher also keeps 404/410 only in an error string, so
the pipeline cannot apply a reliable lifecycle transition.

## Scope
- Capture machine-readable 404/410 status from detail fetching.
- Deactivate only the affected job after a definitive failed detail fetch.
- Preserve evaluations, feedback, and historical fetch evidence.
- Verify inactive filtering and reactivation through an ordinary re-sighting.

## Constraints
- No schema migration in the OL-1..OL-3 lifecycle slice. OL-4 is the only
  schema change, and it is additive (a single nullable column).
- Do not delete jobs or evaluations.
- Do not treat transient or non-definitive failures as lifecycle transitions.
- Keep the dashboard read-only.

## Tasks
- [x] OL-1 Add a database lifecycle helper that deactivates one job without deleting history.
- [x] OL-2 Propagate 404/410 status from detail fetching and deactivate the job in the ingestion pipeline.
- [x] OL-3 Add regression tests for deactivation, preserved evaluations, inactive listing, and reactivation.
- [x] OL-4 Add a safe schema v4 migration for nullable `job_detail_fetches.final_url`.
- [x] OL-5 Capture and persist an allowed redirect's final URL with defensive sanitization.
- [x] OL-6 Add explicit refresh selection for active offers after the redirect evidence slice is stable.
- [x] OL-7 Surface broken-link and stale evidence in the read-only dashboard.

## Acceptance criteria
- A failed detail result with HTTP 404 or 410 sets `jobs.is_active` to false.
- Other failed detail results do not deactivate a job.
- Existing evaluations and feedback remain queryable.
- `list_jobs()` excludes the inactive job and `list_jobs(include_inactive=True)` includes it.
- A later `store_posting` re-sighting reactivates the job as current behavior specifies.

## Checks
- `py -m pytest tests/test_detail_fetcher.py -q`
- `py -m pytest tests/test_dashboard.py tests/test_db.py -q`
- `py -m pytest -q`

## Progress
Implemented OL-1 through OL-3 in one focused work unit (no commit, no push).

### OL-1 — database lifecycle helper
- `db.deactivate_job(conn, job_id) -> bool` updates `jobs.is_active = 0` for one
  job only, with `WHERE id = ? AND is_active = 1`. It never deletes a job,
  evaluation, feedback row, or detail fetch, and returns `True` only on a real
  active-to-inactive transition (idempotent second call returns `False`).

### OL-2 — machine-readable status and pipeline transition
- `linkedin_details.DEFINITIVE_MISSING_STATUSES = frozenset({404, 410})` plus
  `DetailItem.is_definitive_missing` (True only for `status == FAILED` with a
  404/410 `http_status`).
- The `NotFoundError` branch now sets `DetailItem.http_status` from the
  response (`exc.response.status_code`) instead of only writing the status into
  the error string. `to_dict()` already exposes `http_status`, so the signal is
  machine-readable end to end.
- `pipeline.ingest_detail_results` calls `deactivate_job` after inserting the
  provenance row when `item.is_definitive_missing`. Transient, access-denied,
  oversized, and non-FAILED results never deactivate. Added a `deactivated`
  counter to `DetailIngestOutcome` (JSON output via `to_dict()`).
- No schema migration and no redirect/liveness behavior were introduced.

### OL-3 — regression tests
- `tests/test_db.py`: `test_deactivate_job_hides_without_deleting_history`
  (evaluation + feedback preserved, `list_jobs()` excludes,
  `list_jobs(include_inactive=True)` includes, `all_active_jobs()` excludes),
  `test_resighting_reactivates_a_deactivated_job` (re-sighting through
  `store_posting` sets `is_active = 1`).
- `tests/test_detail_fetcher.py`: pure `is_definitive_missing` guard,
  machine-readable 404/410 propagation from `fetch_linkedin_details`,
  `test_definitive_missing_detail_deactivates_job_and_keeps_history`,
  `test_non_definitive_failure_does_not_deactivate` (500/503/403/None),
  `test_resighting_after_deactivation_reactivates_the_offer`.

### Verification (observed)
- `py -m pytest tests/test_detail_fetcher.py -q` → `83 passed in 1.09s`
- `py -m pytest tests/test_db.py tests/test_dashboard.py tests/test_ingest.py -q` → `110 passed in 4.37s`
- `py -m pytest -q` → `635 passed in 9.03s`
- No environmental failures were observed.

### Acceptance criteria coverage
- 404/410 FAILED sets `jobs.is_active = false`: covered by the pipeline test.
- Other failures do not deactivate: covered by the parametrized test.
- Evaluations and feedback remain queryable: covered in `test_db.py`.
- `list_jobs()` excludes / `include_inactive=True` includes: covered in both files.
- Re-sighting reactivates via existing `store_posting` behavior: covered in both files.

### OL-4 — safe schema v4 migration for `job_detail_fetches.final_url`
- `db.SCHEMA_VERSION` is now `4`. Brand-new databases receive `final_url TEXT`
  (nullable) straight from `SCHEMA_SQL`.
- `init_db` now runs additive, version-scoped migrations after
  `CREATE TABLE IF NOT EXISTS`. `_migrate_v4_add_final_url` inspects
  `PRAGMA table_info(job_detail_fetches)` and issues
  `ALTER TABLE ... ADD COLUMN final_url TEXT` only when the column is missing,
  so the migration is idempotent and cannot lose existing rows.
- Persistence base is wired end to end: `JobDetailFetch.final_url`,
  `_row_to_detail_fetch`, and `insert_detail_fetch` all carry the column, so
  insert / `latest_detail_fetch` / `detail_fetches_for_job` round-trip it
  (including the `NULL` default). `to_dict()` exposes it for JSON output.
- No collector, pipeline, CLI, or dashboard code captures redirects yet; that
  remains OL-5. `user_version > SCHEMA_VERSION` is still rejected before any
  write.

### OL-4 verification (observed)
- `py -m pytest tests/test_db.py -q` → `22 passed in 0.24s`
- `py -m pytest tests/test_detail_fetcher.py tests/test_feedback.py tests/test_dashboard.py tests/test_ingest.py tests/test_cli.py -q` → `224 passed in 6.98s`
- `py -m pytest -q` → `638 passed in 9.23s`
- Migration proven on a **copy** of the real `data/jobscout.sqlite3` (v3: 83
  jobs, 103 detail fetches, 165 evaluations, 29 feedback rows): before the
  column was absent and `user_version = 3`; after `init_db` the version was 4,
  `final_url` existed, all four counts were unchanged, a second `init_db`
  returned 4 without altering anything, and all 103 legacy fetches had
  `final_url IS NULL`. The original file was never modified.
- New tests: `test_new_database_has_nullable_final_url_column`,
  `test_migration_v4_adds_final_url_without_losing_history`,
  `test_detail_fetch_final_url_round_trip` in `tests/test_db.py` (the migration
  test builds a genuine pre-v4 `job_detail_fetches` table).

### OL-5 — capture and persist an allowed redirect's final URL
- `linkedin_details.DetailItem.final_url` now carries the URL actually served
  after an allowed redirect. It is populated only in the successful-response
  branch of `fetch_linkedin_details` from `response.url`, i.e. the value the
  transport's redirect handler already vetted. No second request is issued and a
  redirect is never read as an expiry signal.
- `linkedin_details.sanitize_final_url(requested_url, response_url)` is the
  single pure, offline-testable boundary. It returns the response URL only when
  it is a non-empty `http`/`https` URL without embedded credentials and differs
  from the requested URL; otherwise it returns `None`, which persists as `NULL`
  instead of echoing the requested URL as a fake redirect.
- `pipeline.ingest_detail_results` propagates `item.final_url` into
  `JobDetailFetch.final_url`, so the value lands in the existing nullable
  `job_detail_fetches.final_url` column. Host policy, CLI, dashboard, and the
  error/`HTTPError` branches are unchanged.

### OL-5 verification (observed)
- `py -m pytest tests/test_detail_fetcher.py -q` → `96 passed in 1.16s`
- `py -m pytest -q` → `651 passed in 8.28s`
- No environmental failures were observed.
- New tests in `tests/test_detail_fetcher.py`:
  `test_sanitize_final_url_keeps_only_safe_redirects` (identical, empty, and
  missing response URL, embedded credentials, and non-HTTP schemes),
  `test_fetch_captures_allowed_redirect_final_url` (capture with
  `client.call_count == 1`), `test_fetch_records_no_final_url_without_redirect`,
  `test_fetch_drops_unsafe_redirect_final_url` (parametrized),
  `test_ingest_persists_redirect_final_url` (end-to-end persistence; the offer
  stays active, so a redirect is not an expiry),
  `test_ingest_persists_null_final_url_without_redirect`,
  `test_failed_fetch_never_captures_final_url` (404/410 regression plus 500/503
  transient regression), and
  `test_ingest_propagates_item_final_url_to_provenance`.

### OL-6 — explicit refresh selection for active offers
- `db.jobs_needing_refresh(conn, *, cutoff, limit=None, job_ids=None)` and
  `db.count_jobs_needing_refresh(conn, *, cutoff)` are read-only SELECTs. They
  join each job to its single latest detail fetch (`ORDER BY id DESC LIMIT 1`,
  the same attempt `latest_detail_fetch` reports) and accept only three cases:
  1. `ok`/`partial` evidence older than `cutoff` (stale content);
  2. `failed` with `http_status IS NULL` and `raw_document_id IS NULL` (a
     transport failure that never received an HTTP response, so it is
     retryable);
  3. `failed` 404/410 with `jobs.last_seen_at` newer than the fetch, i.e. a
     re-sighting refutes the death signal.
- Excluded by construction: inactive jobs, jobs with no fetch rows, `skipped`
  attempts, and any failure that recorded an HTTP response (including a stored
  raw document without an `http_status`). The predicate lives in one helper
  (`_jobs_needing_refresh_predicate`) so count and list can never drift.
- Ordering is deterministic and intentionally spends a bounded budget on the
  stalest evidence first: `COALESCE(fetched_at, stored_at) ASC, id ASC`. This
  differs from `jobs_needing_details` (`last_seen_at DESC`) on purpose.
- `timeutil.iso_days_ago(days, *, now=None)` is the pure, testable boundary that
  turns `--stale-days` into the absolute cutoff the query compares against.
  `now` makes it deterministic in tests.
- `cli.fetch-details`: `--refresh` joins the mutually exclusive selection group
  (`--job-id` / `--all` / `--refresh`); `--stale-days` (int, default `None`) is
  valid only with `--refresh` and must be `>= 0`. When omitted it resolves to
  `DEFAULT_REFRESH_STALE_DAYS = 0` (no age floor). Rejections happen before any
  database, fixture, or network work and return exit code `1`.
- Output is additive only on the refresh path: JSON gains `selection`,
  `stale_days`, and `cutoff`, and text gains one `refresh selection: ...` line.
  `--job-id`, `--all`, and the default selection keep their exact previous
  shape.
- `--refresh --dry-run` reuses the existing read-only plan path
  (`connect_readonly`, `mode=ro`) so it neither creates nor writes the database.
  A live refresh keeps the same gates as before: `--confirm-network` for the
  network, or `--fixture` for offline responses.
- No schema change, no dashboard change, and no new HTTP status capture in this
  unit.

### OL-6 verification (observed)
- `py -m pytest tests/test_detail_fetcher.py -q` → `105 passed in 1.03s`
- `py -m pytest tests/test_detail_fetcher.py tests/test_db.py tests/test_cli.py tests/test_cli_output.py -q`
  → `153 passed in 2.28s`
- `py -m pytest -q` → `660 passed in 10.29s`
- No environmental failures were observed.
- New tests in `tests/test_detail_fetcher.py`:
  `test_iso_days_ago_is_pure_and_deterministic`;
  `test_jobs_needing_refresh_selects_stale_and_retryable_evidence` (stale vs
  fresh, transient failure, HTTP-recorded failure, raw-document failure,
  skipped, never-fetched);
  `test_jobs_needing_refresh_refutes_only_resighted_404` (refuted vs unrefuted
  404/410 and inactive);
  `test_jobs_needing_refresh_reads_only_the_latest_fetch`;
  `test_jobs_needing_refresh_limit_job_ids_and_order`;
  `test_fetch_details_stale_days_requires_refresh`;
  `test_fetch_details_refresh_dry_run_is_readonly` (bytes and sidecar list
  unchanged);
  `test_fetch_details_existing_selections_are_unchanged` (`--job-id`, `--all`,
  default);
  `test_fetch_details_refresh_live_via_fixture_keeps_history` (refresh appends
  provenance without deleting the previous attempts).

### OL-6 checks and risks
- Focused checks: the test file above, plus the full offline suite; the
  pre-existing `--job-id`, `--all`, and default CLI tests still pass.
- Risk: omitting `--stale-days` means `0` (no age floor), so `--refresh` alone
  selects every already-fetched offer with retryable/stale evidence. This is
  documented in both READMEs and can be revisited if a default window is wanted.
- Risk: a `skipped` latest fetch hides an older stale success because the query
  reads only the latest attempt, matching the task's "excluir skipped" rule.
- Risk: `404/410` refutation depends on `jobs.last_seen_at`, which only
  `store_posting` advances; a manual SQL update would not count as a re-sighting.
- Dashboard surfacing of stale/broken evidence was out of scope here and is
  implemented in OL-7 below.

### OL-7 — link and staleness evidence in the read-only dashboard
- `db.latest_detail_fetches_for_jobs(conn, job_ids) -> dict[int, JobDetailFetch]`
  is the batched, read-only reader. One SELECT joins each job to its single
  highest-id fetch row (`MAX(id) GROUP BY job_id`), the same attempt
  `latest_detail_fetch` reports. Jobs with no attempts are absent from the
  mapping, so "never checked" stays distinct from "checked and failed". It never
  writes and returns `{}` for an empty input.
- `dashboard.build_payload` calls the batched reader once for every shown offer
  and adds a serializable `detail` object to each offer. The reader replaces a
  per-offer `latest_detail_fetch` call, so the report no longer costs one query
  per offer (N+1).
- `dashboard._classify_detail_state` classifies the latest attempt into exactly
  seven honest states using only stored evidence:
  `unchecked` (no attempt), `skipped`, `ok`/`stale` (successful `ok`/`partial`
  evidence inside or past `DETAIL_EVIDENCE_WINDOW_DAYS = 21`),
  `definitive_missing` (404/410), `retryable_failure` (failure with no HTTP
  response and no raw document), and `failed` (any other recorded response).
  `now` is injectable and the exact (unrounded) age is compared, so "exactly 21
  days" is current and one second past it is stale. An unusable timestamp is
  `stale`, never `ok`.
- `DEFINITIVE_MISSING_HTTP_STATUSES = frozenset({404, 410})` mirrors
  `collectors.linkedin_details.DEFINITIVE_MISSING_STATUSES`; the dashboard does
  not import the network collector and a contract test pins the two together.
- `detail.refuted` is True only for a 404/410 whose `checked_at` is older than
  `jobs.last_seen_at`, i.e. a visible offer whose death evidence a later
  re-sighting already contradicted. The rendered copy states that the evidence
  was refuted; it never claims the link is broken now.
- `detail.final_url` is exposed only as safe redirect evidence: a safe http(s)
  URL from a successful attempt (`ok`/`stale`) that differs from the requested
  URL. A failed attempt never exposes a redirect, and an echo of the requested
  URL is not a redirect, so a stored URL can never be read as a broken link.
- The offer card renders a neutral `p.detail-evidence` next to the title, inside
  the card's title column. It is separate from the model axis (`.card-side`) and
  the human axis (`.feedback`), uses only muted/ink/hairline tokens, and adds no
  `--signal`, no `--ember`, no seventh KPI, and no media query. Any redirect href
  goes through the same client-side `safeHttpUrl` guard as the offer link.
- `build_payload`/`render_dashboard_html`/`generate_dashboard` remain read-only:
  no SQLite write, no `localStorage` write, no CLI change, and no schema change.

### OL-7 verification (observed)
- `py -m pytest tests/test_dashboard.py tests/test_db.py -q` → `115 passed in 4.36s`
- `py -m pytest -q` → `672 passed in 12.19s`
- No environmental failures were observed.
- New tests in `tests/test_db.py`:
  `test_latest_detail_fetches_for_jobs_is_batched_and_latest_only` (latest only,
  full history intact, exactly one SELECT via `set_trace_callback`),
  `test_latest_detail_fetches_for_jobs_handles_empty_and_unfetched`.
- New tests in `tests/test_dashboard.py`:
  `test_detail_state_classification_is_honest_for_every_state`,
  `test_detail_state_uses_the_explicit_window_and_injectable_now`,
  `test_dashboard_definitive_statuses_match_the_detail_fetcher`,
  `test_detail_evidence_refutes_resighted_404_and_keeps_redirect_evidence_safe`,
  `test_detail_evidence_for_an_unchecked_offer_is_serializable`,
  `test_payload_detail_reports_only_the_latest_fetch`,
  `test_report_renders_neutral_detail_evidence_near_the_title`,
  `test_detail_evidence_never_writes_to_the_database_or_local_storage`,
  `test_detail_evidence_css_stays_neutral_and_keeps_the_six_kpi_shell`,
  `test_detail_state_labels_cover_every_state_in_neutral_spanish`.
- Empirical payload check over all seven states (fresh/stale success, skipped,
  no fetch, unrefuted 404, refuted 410, 500) produced the expected `detail`
  objects; `node --check` accepted the rendered script.

### OL-7 risks
- The dashboard only embeds the `--top` offers, so the evidence line is shown for
  the offers a reader can actually see; the keyword analytics remain unchanged.
- `definitive_missing` describes the latest *stored* evidence, not a live check.
  A visible offer can carry an unrefuted 404/410 if no re-sighting followed (for
  example a hand-inserted row); the copy states the response, not a current
  conclusion.
- `retryable_failure` vs `failed` mirrors the OL-6 refresh predicate, so a
  transport failure that stored a raw document is `failed` by construction.
- The 21-day evidence window is a separate named constant from
  `FRESH_WINDOW_DAYS`; both are 21 today, and changing one without the other is a
  deliberate, reviewable change.
- Not done here: the dashboard does not trigger refreshes; OL-6's `--refresh`
  remains the only path that acts on this evidence.

## Next step
OL-7 is implemented and verified. No open lifecycle task remains in this file;
follow-up work (e.g. exposing detail evidence in the keyword analytics, or a
configurable evidence window) would be a new change.
