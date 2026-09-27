# Detail provenance and scoring determinism

## Objective

Stop persisting foreign detail text into offers of another provider, close the
GetManfred text gap through the collector (not through the scorer), and make a
scoring pass self-describing: which reasoning variant produced it and whether
the input actually changed.

## Problem (five separate defects, in priority order)

1. **Cross-provider detail contamination (writes bad data).** The LinkedIn
   detail command selects offers from every provider and builds a LinkedIn
   guest-API URL out of any digit-only id, so GetManfred and Tecnoempleo ids are
   read inside LinkedIn's id space. 14 active GetManfred offers currently carry
   a description that belongs to a different posting.
2. **GetManfred has no detail adapter.** 128 of 142 active GetManfred offers
   have no text at all (`content_status = partial`, empty description, no
   company, no location, no date, no salary). They are still scored, on the
   title alone.
3. **The reasoning variant is not recorded.** A run with
   `JOBSCOUT_OPENCODE_VARIANT=max` and a run with `minimal` are byte-identical in
   the database (`model`, `prompt_version`), so two passes cannot be told apart
   or compared after the fact.
4. **Re-evaluation ignores whether the input changed.**
   `jobs_needing_evaluation` only asks "does a successful evaluation from this
   scorer exist", never whether it was computed from the same
   `input_fingerprint`.
5. **Investigation: the credential heuristic suppresses almost every Tecnoempleo
   detail body.** 393 of 393 archived `job_detail` documents from Tecnoempleo
   were stored without a body (`notes = "body not stored: payload looks like it
   contains a credential"`), while 295/295 LinkedIn ones were stored. It is
   either a systematic false positive or a real pattern in that markup; today it
   means no offline re-parsing for Tecnoempleo.

## Scope

- Restrict the LinkedIn detail selection and id resolution to LinkedIn offers.
- Add a bounded, robots-checked GetManfred public detail adapter
  (`fetch-getmanfred-details`) reusing the Tecnoempleo detail safety model, or a
  documented decision to keep GetManfred as a title-only inventory.
- Record the configured OpenCode variant in the evaluation provenance.
- Skip re-evaluating an offer whose scoring input is unchanged.
- Investigate the Tecnoempleo body-suppression cause with a single live capture
  and either fix the detector or document it as intended.

## Constraints

- No network in tests, ever. All parser tests run offline against captured
  markup passed directly to the parsing functions.
- **Existing fixtures for detail parsing are synthetic.** Any new or re-baselined
  fixture must be built from markup captured from the live page once. A green
  suite over synthetic fixtures proves nothing about the source.
- Extract detail fields only from recognised markup; never invent a value. The
  GetManfred sitemap carries no company, so an absent company stays `NULL` and
  must not be derived from the slug.
- A partial or failed attempt must never replace a successful full description
  (`ingest_detail_results` already guarantees this; keep it).
- Never weaken the fail-closed behaviour: `robots.txt` re-fetched and checked
  before the first request, host allow-list, bounded items/delay/timeout,
  401/403/429 aborts the run.
- Do not change the 8 s pacing or the item caps to finish faster.
- Additive SQLite migrations only.
- Keep historical rows importable: no silent rewriting of stored feedback,
  scores, or the 14 contaminated descriptions without an explicit operator
  decision.

## Tasks

- [x] DP-1 Restrict LinkedIn detail targets to LinkedIn offers:
  - `_select_detail_jobs` (`src/jobscout/cli.py:1097-1147`) resolves every route
    through the provider-variant layer: explicit `--job-id` uses
    `variant_job_for_source(..., "linkedin")`, `--all` uses
    `active_variants(source="linkedin")`, `--refresh` uses
    `variants_needing_refresh(source="linkedin")`, and the default uses
    `variants_needing_details(source="linkedin")`
    (`src/jobscout/db.py:1485`, `src/jobscout/db.py:1548`,
    `src/jobscout/db.py:1693`). A non-LinkedIn id/content is reported and never
    planned.
  - `resolve_source_job_id` (`src/jobscout/collectors/linkedin_details.py:213`)
    returns `None` when the job's `source` is present and is not `linkedin`, and
    when neither the stored id nor the URL resolves as a LinkedIn id/URL.
    `build_detail_url` (same file, `:243`) therefore never substitutes another
    provider's id, and the template comes from `DEFAULT_LINKEDIN_DETAIL_URL`
    (`src/jobscout/config.py:34`).
  - Tests (offline, `tests/test_detail_fetcher.py`): a mixed selection contains
    only the LinkedIn target; foreign ids are dropped on the default, `--all`,
    and `--job-id` routes; a LinkedIn slug URL with a numeric id still resolves;
    no test opens a socket.
  - Verify (human step, not re-run here): `PYTHONPATH=src python -m jobscout
    fetch-details --dry-run` (offline) must show zero non-LinkedIn targets; after
    one `--confirm-network` run, re-run the join query below and expect no new
    rows where `jobs.source <> job_detail_fetches.source`.
- [x] DP-2 Record the reasoning variant in provenance:
  - `OPENCODE_PROMPT_VERSION` (`src/jobscout/scoring/opencode.py:84`) and the four
    construction sites that write `prompt_version` (`src/jobscout/scoring/opencode.py:690`,
    `:710`, `:728`, `:758`), all fed by `effective_prompt_version`
    (`src/jobscout/scoring/opencode.py:206`).
  - Rule: when `settings.opencode_variant` is non-empty, the recorded
    `prompt_version` carries it as a suffix (`<version>+<variant>`); when no
    variant is configured the string stays byte-identical to the historical value,
    so old rows remain comparable. The OpenCode scorer also records its own
    effective-input fingerprint (`input_fingerprint(job, profile, settings)`), so
    the variant is part of the fingerprint too.
  - Tests: `tests/test_opencode.py` (two variants differ in `prompt_version`; no
    variant keeps the bare version). Verify command below remains a human step.
  - Verify (human step, not re-run here): `JOBSCOUT_OPENCODE_VARIANT=max
    PYTHONPATH=src python -m jobscout score --job-id <ID> --scorer opencode
    --rescore` and read the column.
- [x] DP-3 Skip re-evaluation when the input did not change:
  - `jobs_needing_evaluation` (`src/jobscout/db.py:1296`) takes an optional
    `fingerprint_for` callable and selects an offer only when no successful
    evaluation from that scorer carries its *current* effective-input
    fingerprint. That single fingerprint already covers scorer, model,
    prompt/policy version, profile, and job content, so a separate
    `(scorer, model, prompt_version, input_fingerprint)` tuple is not needed.
  - The CLI passes `fingerprint_resolver(...)` for normal selection
    (`src/jobscout/cli.py:1767`); `--rescore` still forces every active job via
    `all_active_jobs`. `README.md` and `README.en.md` document the contract.
  - Tests (offline): `tests/test_cli.py::test_score_is_idempotent_without_rescore`
    and `::test_score_reselects_when_the_profile_changes`; the input-aware
    selection and changed-input-not-masked cases in `tests/test_db.py` and
    `tests/test_job_variants.py`.
  - Verify (human step, not re-run here): run `score --all --scorer heuristic`
    twice on a database copy; the second run must select 0 offers, and
    `--rescore` must still select all active ones.
- [x] DP-4 GetManfred detail enrichment **(implementation complete: permission
  confirmed, static JSON-LD gate passed, bounded adapter implemented and
  offline-covered; the live enrichment record below is historical and was not
  re-verified in this audit, and the pending live check stays pending)**:
  - Gate: prior **written permission from Manfred** covering automated requests and
    the storage of offer descriptions in this project is confirmed. The static-HTML
    gate also passed: `robots.txt` was re-checked immediately before the capture
    (`User-agent: *` / `Allow: /`), and one offer page was captured exactly once
    (`https://www.getmanfred.com/ofertas-empleo/8476/datcisions-backend-engineer-php-sept26`).
    Its initial HTML carries a static JSON-LD `<script id="jobPostingSchema"
    type="application/ld+json">` whose `@type` is `JobPosting` with `datePosted`,
    `description`, `hiringOrganization.name`, `title`, `jobLocation`,
    `employmentType`, and `baseSalary`, so no JavaScript rendering is required.
  - [x] Mirror `collectors/tecnoempleo_details.py` as
    `collectors/getmanfred_details.py`: `robots.txt` re-fetch and per-URL check
    before the first request, HTTPS `*.getmanfred.com` allow-list, bounded
    items/delay/timeout, fail-closed, recognized JSON-LD only, fields the page does
    not expose stay `NULL` (the company is never derived from the slug), and
    persistence through `ingest_detail_results(provider='getmanfred')`. URL
    resolution only accepts known `/ofertas-empleo/{id}/...` HTTPS URLs.
  - [x] Fixture: `tests/fixtures/getmanfred_detail_page_jobposting.html` is built
    from the real captured JSON-LD script excerpt, not a synthetic page.
  - [x] CLI `fetch-getmanfred-details` with `--dry-run` (no request, read-only),
    `--confirm-network`, `--job-id`/`--all` scoped to the GetManfred provider
    variant, `--max-items`, `--robots`, and `--json`, mirroring the other detail
    commands.
  - [x] Offline tests in `tests/test_getmanfred_details.py` (real fixture parse,
    optional/null fields, URL/host/robots refusal, caps, dry-run, confirmation
    required, fail-closed statuses, redirect handling, provider isolation, and the
    preserve-successful-description invariant). No test opens a socket.
  - Historical record only — **not re-verified in this audit, not a substitute for
    the pending live check below**: a prior orchestrator note reported that
    `fetch-getmanfred-details --dry-run` planned 20 of 128 pending offers under the
    20-item cap, that one confirmed fetch enriched job 937 (`content_status = ok`,
    description length 569, `job_detail_fetches.source = getmanfred`, HTTP 200),
    and that a targeted run reported 128 enrichments in total. This is dated
    historical evidence from that task record; no live call was made and no local
    database was inspected now.
- [x] DP-5 Investigate and fix Tecnoempleo body suppression:
  - One live detail-page capture showed the generic credential detector was
    matching Cloudflare's public `data-cf-beacon` JSON `token` field (32 hex chars).
    The narrow exception neutralizes only that exact recognized attribute/value;
    real token/password/secret fields and JWT-like values remain fail-closed.
  - Offline fixture and regression tests cover the captured markup shape and
    verify that other credential patterns still suppress the body.
- [x] DP-6 Operator decision on the 14 contaminated descriptions:
  **Decision: re-enrich; no purge.** The operator authorized the non-destructive
  path: only historical GetManfred offers whose latest *successful* detail fetch
  came from LinkedIn are re-enriched with the real GetManfred text, and every
  historical `job_detail_fetches`/raw record is preserved — nothing is deleted.
  - Implemented in `ingest_detail_results`/`_merge_detail_description`
    (`src/jobscout/pipeline.py`): a successful fetch may replace an existing
    `ContentStatus.OK` description regardless of length **only** when the item is
    `DetailFetchStatus.OK` with a non-empty recognized description, the fetch's
    `provider` equals the job's `source`, and the latest successful provenance
    source exists and differs from the job's `source`. Same-provider,
    unknown-provenance, partial, failed, and skipped results keep the existing
    preservation policy unchanged. No schema migration.
  - New read-only helper `latest_successful_detail_fetch_source`
    (`src/jobscout/db.py`) returns the `source` of the highest-`id`
    `status='ok'` provenance row, so a later failed/partial attempt never hides
    the provider that produced the stored text.
  - Offline integration tests in `tests/test_getmanfred_details.py` cover the
    foreign-provenance correction, same-provider preservation, partial and failed
    protection, unknown provenance, and the wrong-provider guard.
  - Completed: all 14 identified offers were re-enriched individually through
    `fetch-getmanfred-details --job-id <ID> --max-items 1 --confirm-network`.
    Every run reported `enriched=1`, `changed=1`, and no partial/failed/skipped
    result. A read-only final check found zero active GetManfred offers whose
    latest successful detail source is foreign; all 142 active GetManfred offers
    have `content_status='ok'`. Historical fetch/raw records were preserved.

## Evidence

Reproduce the contamination:

    select j.id, count(*), sum(case when f.status = 'ok' then 1 else 0 end)
      from job_detail_fetches f join jobs j on j.id = f.job_id
     group by j.source, f.source;

Observed: `getmanfred -> linkedin: 22 attempts, 14 ok`,
`tecnoempleo -> linkedin: 10 attempts, 0 ok`,
`linkedin -> linkedin: 300 attempts, 279 ok`,
`tecnoempleo -> tecnoempleo: 393 attempts, 357 ok`.

Foreign text, live examples (title from GetManfred, description from LinkedIn's
id space):

- job 579 `Joko staff platform software engineer jul26` (id 8383) ->
  "Regional Manager - Conferencing. Four-year college degree is preferred..."
- job 581 `Abi global health ia platform engineer jun26` ->
  "Urban Media Network SL is looking for part- or full-time freelance sales
  consultants based in Barcelona..."
- job 582 `Centric software senior data platform engineer may26` ->
  "Position: RA796. Title: Director, Market Research. Salary: 115-140K.
  Location: NY."

13 of the 14 share zero meaningful title tokens with their stored text; the
remaining one shares 1 of 4.

Duplicate scoring evidence (DP-3):

    select job_id, count(*) from evaluations
     where model = 'opencode/muse-spark-1.3-contributor-free'
     group by 1 having count(*) > 1;   -- 139 rows

ids 1615-1753 were produced 2026-09-25T22:38-23:21 and ids 1754-2398 during the
full pass that ended 2026-09-26T00:57. Same `input_fingerprint` in both, ~43
minutes of duplicated compute, and 8 recommendation changes (for example job 784:
`32 skip` then `55 consider`) caused only by resampling.

Coverage evidence (DP-4):

    select source, content_status, count(*) from jobs
     where is_active = 1 group by 1, 2;

Observed: `linkedin ok 279 / partial 2`, `tecnoempleo ok 222`,
`getmanfred ok 14 / partial 128`.

Known good, do not regress: LinkedIn descriptions were re-parsed offline from the
archived HTML and matched the stored text character for character (5/5 samples,
5.9k-10.9k chars), with only the "Show less" button after the extracted block.

## Checks

- [x] `.venv/Scripts/python.exe -m pytest -q` passes offline: **1129 passed** (no network).
- [ ] `fetch-details --dry-run` plans no non-LinkedIn target. (Offline coverage:
      `tests/test_detail_fetcher.py`; the dry-run itself is a human step.)
- [ ] One `score --all --scorer heuristic` run on a database copy followed by a
      second run selects 0 offers; `--rescore` still selects all active ones.
      (Offline coverage: `tests/test_cli.py` idempotency/re-selection tests; the
      database-copy run is a human step.)
- [ ] A `--variant max` evaluation and a `--variant minimal` evaluation of the
      same offer differ in `prompt_version`. (Offline coverage:
      `tests/test_opencode.py`; the live scoring run is a human step.)
- [ ] The join query above shows no row where `jobs.source <> job_detail_fetches.source`
      after the next confirmed run.
- [ ] DP-4 live check (orchestrator): `fetch-getmanfred-details --dry-run` plans only
      GetManfred `/ofertas-empleo/{id}/...` URLs, then ONE offer with
      `--confirm-network` moves `content_status` to `ok` with a non-zero description.
      Pending; the offline adapter work does not substitute for it.
- [x] README.md and README.en.md updated for the DP-3 `--rescore` contract (in the
      working tree; not committed by this change).

## Order and rationale

DP-1 first: it is the only defect currently writing bad data into the database.
DP-2 and DP-3 next: both are small and together they make the planned two-pass
scoring (minimal for the whole corpus, a higher variant for the shortlist)
auditable and cheap. DP-4 last of the code work, and only after its gate. DP-5 is
an investigation with a single-request budget. DP-6 is an explicit operator
decision.
