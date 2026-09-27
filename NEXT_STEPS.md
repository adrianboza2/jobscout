# Next Steps

This document records the next improvements for `jobscout` after the first
working MVP.

## Priority 1 — Turn feedback into durable data

Status: implemented and verified.

- Import the dashboard's exported `jobscout.feedback.v1` JSON into SQLite.
- Store the human decision separately from the model recommendation.
- Store an optional reason and timestamp.
- Keep the import idempotent and validate the export schema before writing.

The dashboard export was imported into SQLite successfully. Future exports should
be imported with `jobscout import-feedback` after reviewing them locally.

## Priority 2 — Optimize search keywords

Status: the catalog, the security coverage, and the per-keyword analytics are
implemented; the optimization loop itself is a manual, multi-cycle habit.

Implemented:

- (done) bounded keyword catalog in `src/jobscout/keywords.py`, exposed offline by
  `jobscout keyword-plan` and selected through `JOBSCOUT_KEYWORD_FAMILIES`;
- (done) Cloud Security, DevSecOps, Azure security, and IAM/security-infrastructure
  families, including junior/entry-level variants;
- (done) per-keyword sightings, unique jobs, duplicate rate, average score,
  top-five/top-ten quality, model apply/consider counts, imported human
  decisions, human-approved application rate, freshness share, and inactive share;
- (done) honest `null`/`sin dato` values wherever the stored data cannot support a
  metric;
- (done) dashboard columns, a third ranking list by human approval, and a warning
  when no feedback has been imported.

Still manual and open:

- run the planned queries across at least three comparable search cycles before
  removing a keyword;
- generate nearby variants from terms that work, but keep human approval before
  activating a new live query (the catalog is the review gate);
- no batch/live fan-out: one explicit `search-linkedin --confirm-network` per
  query, by design.

Current high-value query families include:

- `Junior Cloud Engineer`;
- `Junior DevOps Engineer`;
- `Platform Engineer`;
- Cloud Security and DevSecOps variants, including Azure and junior variants
  (`jobscout keyword-plan --family cloud-security --family devsecops`).

LinkedIn result position is provenance, not quality ground truth.

## Priority 3 — Improve offer lifecycle handling

Status: implemented and verified offline.

- Detect expired, removed, redirected, or duplicate postings.
- Mark stale offers without deleting their historical evaluation.
- Refresh only active/relevant offers.
- Show broken-link status in the dashboard.

Implemented in the lifecycle work units: definitive 404/410 deactivation,
resighting reactivation, safe redirect evidence, bounded refresh selection, and
neutral dashboard evidence. Future work is limited to provider-specific detail
health where a public source exposes a compatible detail contract.

## Priority 4 — Calibrate scoring

Status: implemented as a read-only report; policy tuning remains human-owned.

- Compare model recommendations with imported human decisions using
  `jobscout calibrate --scorer opencode`.
- Identify false positives and false negatives without changing scores.
- Adjust role-fit, seniority, freshness, and dealbreaker handling.
- Preserve evaluation provenance when prompts or profile versions change.

## Priority 5 — Improve daily workflow

Status: the dashboard and notification items are implemented and verified; the
multi-cycle search cadence remains a manual habit.

- Search cadence: run an exhaustive bounded sweep of all approved keyword families
  every 3–4 days, using `--max-pages 2`; do not search every day by default.
- After each sweep, enrich only new or incomplete offers and score only new jobs.
- Review the dashboard and import feedback in the same session.
- Inspect keyword performance after at least three comparable cycles.
- Add optional scheduling only with explicit user control.
- (done) local or opt-in notifications for high-value offers: `jobscout notify`
  previews active offers at or above `JOBSCOUT_NOTIFY_SCORE_THRESHOLD` that have no
  imported human decision. Disabled by default (`JOBSCOUT_NOTIFY_ENABLED=false`);
  while disabled it never opens or creates the database. Read-only, offline, no
  model call, no fan-out, no scheduler, no daemon, and no remote delivery.
- (done) dashboard view for offers without an imported decision: a read-only filter
  over the `job_feedback` table, deliberately kept separate from the browser
  `localStorage` review state and never merged with it.
- Keep all automation read-only with respect to applications.

Honest limitation: there is **no** "new in the latest cycle" view.
`search_runs.cycle_id` is nullable and free-form, and runs from different platforms
or cycles are not guaranteed to be comparable, so that metric cannot be computed
trustworthily and is deliberately not approximated. The only isolation predicate is
the absence of a `job_feedback` row.

## Priority 7 — Dashboard UI/UX maintenance

Status: current desktop redesign implemented on `master`; keep this area focused
on usability rather than decorative expansion.

- Keep offers visually separated and easy to scan at desktop widths.
- Preserve the distinction between model recommendations and human feedback.
- Keep interactive effects lightweight and respect reduced-motion preferences.
- Keep the report self-contained, offline, dependency-free, and safe to open via
  `file://`.
- Re-run dashboard and security checks after changes to HTML, JavaScript, URLs,
  localStorage, or report rendering.

## Priority 6 — Expand sources

Status: implemented; first bounded public sweep completed.

Add other job platforms through isolated source adapters. Each adapter shares the
same normalized job contract, deduplication rules, provenance model, rate limits,
and fail-closed behavior.

Implemented:

- (done) provider-agnostic ingestion (`provider` is no longer hardcoded to
  LinkedIn), preserving LinkedIn and fixture behavior;
- (done) additive provenance migrations: schema v5 added `search_runs.cycle_id`
  and `search_run_items.position_kind` (nullable, historical rows untouched);
  schema v6 added the provider-variant layer (`job_variants` plus a nullable
  `job_detail_fetches.variant_id`), so one canonical deduplicated offer keeps
  each provider's own external id, listing URL, and availability/detail state
  without overwriting the first provider's identity;
- (done) provider-scoped keyword analytics keyed by `provider|keyword|cycle`, so
  the same term is never merged across platforms or cycles;
- (done) GetManfred public sitemap inventory adapter (`search-getmanfred`): no
  keyword fan-out, `position_kind = none`, fail-closed on unrecognized XML;
- (done) Tecnoempleo public server-rendered search adapter
  (`search-tecnoempleo`): `position_kind = search_rank`, runtime `robots.txt`
  enforcement, fail-closed on unsupported markup;
- (done) GetManfred public detail adapter (`fetch-getmanfred-details`): prior
  written permission confirmed, the static-HTML `JobPosting` JSON-LD gate passed,
  and the adapter reuses the Tecnoempleo detail safety model (`robots.txt` re-read
  and checked per URL, HTTPS `*.getmanfred.com` allow-list, bounded items/pacing,
  fail-closed, recognized JSON-LD only, persistence through
  `ingest_detail_results(provider='getmanfred')`). The one-live-request
  verification is still pending the orchestrator's run.
- (done) InfoJobs blocked at the adapter level with no CLI command.

Still open:

- the unfiltered GetManfred inventory was deleted and replaced by a keyword-
  filtered run with 22 matching offers; unrelated backend/frontend entries are
  no longer present;
- Tecnoempleo's 125 offers were enriched through their public detail pages and
  are now `content_status=ok` where the recognized markup was available;
- GetManfred required a one-off 1 MiB bounded sitemap ceiling because its public
  sitemap exceeds the normal raw-document limit;
- the GetManfred detail adapter is implemented and offline-covered; the single
  confirmed live offer run remains to be executed and inspected by the
  orchestrator before the 128 partial GetManfred offers are re-enriched;
- Tecnoempleo completed 12 of 13 queries; the Junior DevSecOps query failed
  closed on unrecognized live markup and produced no offers;
- InfoJobs requires an official API authorization and explicit app credentials
  before any adapter work. The private `candidate/my-infojobs.xhtml` page must
  never be fetched.

The next comparable sweep should reuse the same `cycle_id` convention only when
the search scope and limits are intentionally comparable; do not interpret raw
offer counts across platforms as quality without human feedback.

## Later features

- Tailored CV generation.
- Cover-letter drafts.
- Interview preparation.
- Company research.
- Salary and compensation analysis.

These features must remain draft-only and must never submit an application
automatically.

## Publication and maintenance

- Keep `profile.yaml`, `.env`, SQLite databases, raw captures, and generated
  reports ignored by Git.
- Run the full offline test suite before each release.
- Re-run the security audit after changes to collectors, subprocesses, URLs,
  secrets, or report rendering.
- Never commit `jobscout-feedback.json`, private profiles, databases, raw captures,
  generated reports, or unrelated local files.
- Keep the private GitHub repository synchronized only after reviewing commits.
