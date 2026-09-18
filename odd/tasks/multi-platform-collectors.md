# Multi-platform collectors

## Objective
Add safe public job-source adapters while preserving provenance for platform,
keyword, observed position, and comparable search cycle.

## Scope
- Generalize ingestion so the provider is not hardcoded to LinkedIn.
- Add an additive cycle identifier and position-kind provenance.
- Separate keyword analytics by platform and cycle.
- Add GetManfred sitemap inventory mode with no keyword fan-out.
- Add Tecnoempleo bounded public search mode for the approved keyword catalog.
- Keep InfoJobs deferred until an official API and app authorization exist.

## Constraints
- No cookies, private sessions, credential reuse, CAPTCHA or anti-bot evasion.
- Preserve existing LinkedIn behavior and historical rows.
- All network actions require existing explicit confirmation gates.
- Use additive SQLite migrations and fail closed on unsafe/unsupported responses.

## Tasks
- [x] Generalize provider/cycle/position provenance and migrations (schema v5).
- [x] Split analytics by platform, keyword, and cycle.
- [x] Implement GetManfred sitemap adapter and tests.
- [x] Implement Tecnoempleo public search adapter and tests.
- [x] Document InfoJobs API-only deferral (blocked adapter, no live path).
- [x] Run the offline suite and record results.
- [x] Run one bounded public sweep of GetManfred and Tecnoempleo; no InfoJobs
      session or private endpoint was used.
- [x] Create a local commit; do not push.

## Status (verified 2026-09-18)

Offline suite: `python -m pytest -q` -> **834 passed** (was 748 before this work).

Focused suites (also pass with sockets hard-blocked at process level):

- `tests/test_robots.py`, `tests/test_getmanfred_collector.py`,
  `tests/test_tecnoempleo_collector.py`, `tests/test_provenance.py`,
  `tests/test_infojobs.py`, `tests/test_provider_url_policy.py`,
  `tests/test_cli_providers.py` -> **85 passed**; the six provider/provenance
  files -> **53 passed** with a guarded `socket.socket`.
- `tests/test_db.py` includes
  `test_migration_v5_adds_cycle_and_position_without_losing_history`.


Implemented and verified:

- **Provider-agnostic ingestion.** `ingest_postings`/`ingest_collector_result`/
  `ingest_fixture` accept an explicit `provider`; `CollectorResult` moved to
  `collectors/base.py`. A config without a provider still defaults to
  `linkedin`, so existing callers and run params are byte-for-byte compatible.
- **Additive migration v5.** `search_runs.cycle_id` and
  `search_run_items.position_kind` are added nullable; a real pre-v5 database is
  migrated in place and historical rows keep their values (`NULL`), verified by
  `tests/test_db.py::test_migration_v5_adds_cycle_and_position_without_losing_history`.
- **Provider-scoped analytics.** Dashboard keyword series are keyed by
  `provider|keyword|cycle`; the same literal keyword from LinkedIn, GetManfred,
  and Tecnoempleo produces three distinct series (verified end to end). Inventory
  runs (`position_kind = none`) store rank `0` and the UI shows "sin dato".
- **GetManfred adapter.** Public offers sitemap only
  (`https://www.getmanfred.com/sitemap-offers.xml`, advertised in its
  `robots.txt`, which allows `/`). Inventory mode: no keyword query, no rank,
  `position_kind = none`, partial postings whose title is derived from the public
  URL slug. Unsupported XML fails closed.
- **Tecnoempleo adapter.** Public server-rendered search under
  `/ofertas-trabajo/`; fetches `robots.txt` and fails closed if the path is
  disallowed or robots is unreadable. `position_kind = search_rank`. The 200
  body with no recognizable offer link fails closed instead of reporting zero.
- **InfoJobs.** Blocked adapter (`collectors/infojobs.py`) with a hard
  fail-closed refusal; no CLI command is registered.

Observed limitations / still open:

- **Tecnoempleo selectors are unverified.** Its `robots.txt` explicitly
  disallows several automated/AI crawlers, so its search HTML was not fetched
  during this work. The parser uses the stable `rf-` offer path marker and
  configuration-driven settings (`JOBSCOUT_TECNOEMPLEO_OFFER_PATH_MARKER`).
  Confirm the marker against a live page before any `--confirm-network` run.
- **Live sweep observed 2026-09-18.** GetManfred discovered 250 sitemap offers
  (249 new, 1 duplicate) with a one-off bounded 1 MiB raw limit. Tecnoempleo
  completed 12 of 13 queries, adding 125 offers; the Junior DevSecOps query
  failed closed on unrecognized markup and added nothing.
- New-platform offers are partial sitemap/search summaries, not enriched detail
  pages; they were scored with that limitation visible in the dashboard.
- The live run is evidence of transport/markup compatibility for this cycle, not
  a permanent selector guarantee; re-check failures on the next sweep.

## InfoJobs deferral (authorization blocker)

`https://www.infojobs.net/candidate/my-infojobs.xhtml` is a private,
authenticated candidate page and must never be fetched. InfoJobs can only be
added after (1) an official InfoJobs API authorization and (2) explicit
app-scoped credentials, with a reviewed adapter and offline fixtures. This is a
product/authorization decision, not an implementation detail.
