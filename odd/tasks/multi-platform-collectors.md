# Multi-platform collectors

## Objective
Add safe public job-source adapters while preserving provenance for platform,
keyword, observed position, and comparable search cycle.

## Scope
- Generalize ingestion so the provider is not hardcoded to LinkedIn.
- Add an additive cycle identifier and position-kind provenance.
- Separate keyword analytics by platform and cycle.
- Add GetManfred sitemap inventory mode with an approved-keyword local filter
  (single fetch, no keyword fan-out, no private endpoint).
- Add Tecnoempleo bounded public search mode for the approved keyword catalog.
- Add a bounded Tecnoempleo public detail adapter and an explicit enrichment
  command, reusing transport, robots/allow-list, limits, pacing, and fail-closed
  behavior.
- Preserve keyword provenance per offer/platform/cycle without mixing terms.
- Keep InfoJobs deferred until an official API and app authorization exist.

## Constraints
- No cookies, private sessions, credential reuse, CAPTCHA or anti-bot evasion.
- Preserve existing LinkedIn behavior and historical rows.
- All network actions require existing explicit confirmation gates.
- Use additive SQLite migrations and fail closed on unsafe/unsupported responses.
- Extract detail fields only from recognized markup; never invent `ok`.

## New flow: search -> detail enrichment -> scoring

1. **Search / discover.** `search-linkedin`, `search-tecnoempleo`, and
   `search-getmanfred` produce partial postings. GetManfred applies the approved
   keyword *locally* to a single sitemap fetch; Tecnoempleo searches the keyword
   directly. Each observation keeps `provider|keyword|cycle` provenance.
2. **Enrich detail.** `fetch-details` (LinkedIn) and
   `fetch-tecnoempleo-details` (Tecnoempleo) fetch the public detail page and
   upgrade a posting to `content_status = ok` only when recognized markup yields
   a real description. Unrecognized/403/truncated pages stay `partial`/`failed`.
3. **Score.** `score --all --scorer opencode` evaluates the enriched corpus;
   `--rescore` is only for a profile/prompt change.

## Tasks
- [x] Generalize provider/cycle/position provenance and migrations (schema v5).
- [x] Split analytics by platform, keyword, and cycle.
- [x] Implement GetManfred sitemap adapter with optional local keyword filter.
- [x] Implement Tecnoempleo public search adapter and tests.
- [x] Add Tecnoempleo public detail adapter + `fetch-tecnoempleo-details`.
- [x] Add local keyword-matching helper preserving per-offer keyword provenance.
- [x] Document InfoJobs API-only deferral (blocked adapter, no live path).
- [x] Run the offline suite and record results.
- [x] Create a local commit; do not push.

## Status (verified 2026-09-18)

Offline suite: `python -m pytest -q` -> **858 passed** (was 834 before this work).

Focused suites (also pass with sockets hard-blocked at process level):

- `tests/test_getmanfred_collector.py`, `tests/test_tecnoempleo_collector.py`,
  `tests/test_tecnoempleo_details.py`, `tests/test_detail_fetcher.py`,
  `tests/test_provenance.py`, `tests/test_cli_providers.py`,
  `tests/test_db.py` -> pass; the LinkedIn detail contract is unchanged.
- `tests/test_db.py` includes
  `test_migration_v5_adds_cycle_and_position_without_losing_history`.


Implemented and verified:

- **Provider-agnostic ingestion.** `ingest_postings`/`ingest_collector_result`/
  `ingest_fixture` accept an explicit `provider`; `CollectorResult` moved to
  `collectors/base.py`. A config without a provider still defaults to
  `linkedin`, so existing callers and run params are byte-for-byte compatible.
- **Additive migration v5.** `search_runs.cycle_id` and
  `search_run_items.position_kind` are added nullable; a real pre-v5 database is
  migrated in place and historical rows keep their values (`NULL`).
- **Provider-scoped analytics.** Dashboard keyword series are keyed by
  `provider|keyword|cycle`; the same literal keyword from LinkedIn, GetManfred,
  and Tecnoempleo produces three distinct series (verified end to end). Inventory
  runs (`position_kind = none`) store rank `0` and the UI shows "sin dato".
- **GetManfred adapter.** Public offers sitemap only, with an **optional local
  keyword filter**. The adapter fetches the sitemap once per run and keeps only offers
  whose normalized title/slug contains every token of the approved keyword
  (`collectors/keyword_match.py`). A backend/frontend offer is dropped when it
  does not match the approved keyword. Each kept offer records
  `extra['matched_keywords']`, so an offer under one or several keywords is not
  mixed. No private endpoint and no per-keyword fan-out. `position_kind = none`,
  rank `0`. Unsupported XML fails closed.
- **Tecnoempleo search adapter.** Public server-rendered search under
  `/ofertas-trabajo/`; fetches `robots.txt` and fails closed if the path is
  disallowed or robots is unreadable. `position_kind = search_rank`. A 200 body
  with no recognizable offer link fails closed instead of reporting zero.
- **Tecnoempleo detail adapter.** `collectors/tecnoempleo_details.py` +
  `fetch-tecnoempleo-details`. Re-fetches `robots.txt` and checks every planned
  detail URL before the first request; no login, cookies, RSS, or API. Extracts
  description, company, location, date (`<time datetime>` preferred), and
  technologies only from recognized `itemprop`/class markup. A recognized page is
  `content_status = ok` with a non-empty description; unrecognized/403/truncated
  pages stay `partial`/`failed` with an honest error. Never invents `ok`.
- **Additive persistence.** `ingest_detail_results` accepts an optional
  `provider` (default `linkedin`) and applies detail-derived
  `company`/`location`/`posted_at`/`technologies` only when non-empty;
  `update_job_content` merges `extra_json` instead of replacing it. The LinkedIn
  path and contract are unchanged.
- **InfoJobs.** Blocked adapter (`collectors/infojobs.py`) with a hard
  fail-closed refusal; no CLI command is registered.

### Why storing multiple keywords per observation was not needed

The current schema represents keyword provenance on `search_run_items` through
the run's single `query` column. GetManfred accepts a comma-separated approved
keyword catalogue, fetches the sitemap once, and stores all matching terms in
`extra['matched_keywords']`. The current run remains one inventory observation;
the terms never broaden the filter to unrelated backend/frontend roles. The
tradeoff is that dashboard per-term series use the matched-keyword evidence for
this inventory run rather than separate network runs; this avoids a 13x sitemap
fan-out while keeping the local filter auditable.

### Live correction run (2026-09-18)

- Deleted the previous unfiltered GetManfred inventory: 249 jobs, 257 evaluations,
  250 provenance items, 2 runs, and 2 raw captures. Other providers were untouched.
- Re-ran GetManfred with the approved catalogue: 22 matching offers, all retained
  with `matched_keywords` evidence.
- Enriched all 125 Tecnoempleo offers through their public detail pages:
  125/125 became `content_status=ok`; no partial or failed detail remained.
- Re-scored the 147 corrected/new offers with the real profile and MiMo.

## InfoJobs deferral (authorization blocker)

`https://www.infojobs.net/candidate/my-infojobs.xhtml` is a private,
authenticated candidate page and must never be fetched. InfoJobs can only be
added after (1) an official InfoJobs API authorization and (2) explicit
app-scoped credentials, with a reviewed adapter and offline fixtures. This is a
product/authorization decision, not an implementation detail.
