# Next Steps

This document records the next improvements for `jobscout` after the first
working MVP.

## Session 2026-10-20 — OpenCode 2.x migration: scoring restored, docs aligned

The 2026-10-19 sweep scored 0 of 21 offers. Cause: OpenCode 2.0.32 (installed
2026-10-18) removed `--pure` and `--variant`, and the configured
`opencode/muse-spark-1.3-contributor-free` model no longer exists.

Fix: the scorer now calls `opencode run [--standalone] --model <model>#<variant>
--format json`, with `low` as the default variant and
`JOBSCOUT_OPENCODE_PURE=true` meaning environment isolation (`--standalone`, a
private `XDG_CONFIG_HOME` under the database folder, project config/autoupdate/
Claude Code skills disabled) instead of `--pure`. A non-zero exit now reports the
first JSON `error` event as `<type>: <message>` instead of the raw event stream.
`JOBSCOUT_OPENCODE_FALLBACK_MODEL` (optional, accepts `#variant`) switches the
rest of a `score` run after the first `status=error`, and `score --json` reports
it as `fallback_model`. The READMEs are updated.

- **Owner decision:** primary `opencode/deepseek-v4.1-flash-free`, fallback
  `opencode-go/deepseek-v4.1-flash#low`.
- **Owner pending:** renew the OpenCode Console API key (expired 2026-10-20);
  then confirm the free model accepts the `low` variant.
- **Structure:** the empty `openspec/` directory is removed; closed task cards
  now live under `odd/tasks/done/`.
- **Confirmed:** the salary estimator described below is NOT implemented; it
  remains planned (see the 2026-10-18 session).

## Next session (2026-10-18) — roadmap: expandable rows, salary estimator, gates, divergence review

Decisions recorded after reviewing `MadsLorentzen/ai-job-search` against this
repo (clone at `/tmp/opencode/ai-job-search`, read-only; nothing adopted
verbatim). Owner context: collection-only phase until applications start
May–June 2027; no post-application outcome tracking for now.

### Decisions

- **Pipeline stays automatic.** The existing cron sweep already runs
  collect → dedupe → enrich → score unattended (five completed runs audited
  2026-10-16). Nothing in this plan adds a manual step; the workflow section
  below replaces per-offer human review with one weekly session.
- **Posting-deadline/urgency flags: dropped** (owner, not important).
- **Binary apply/reject decisions: out of the weekly workflow**, replaced by
  the divergence view (item 5). The decision UI's final fate (remove vs keep
  dormant) is an open owner decision; the tables stay regardless.
- **Model guidance philosophy:** prompt-first iteration is the owner's
  preferred improvement loop; the defensive parser (`scoring/schema.py`)
  stays as the *measurement instrument* — `ParseOutcome.repairs` per 100
  evaluations is the metric that shows whether a prompt change worked.

### Planned work units (in order; suite green after each)

1. **Expandable offer row** (dashboard render only). `dashboard.py` renders,
   per offer, the data `evaluations` already stores: dimension breakdown,
   strengths, gaps, rationale. No schema, scoring, or LLM change.
2. **Unknown profile-key warnings.** `scoring/profile.py` warns at load time
   using `difflib.get_close_matches` (stdlib): near-miss of a known field →
   `"unknown key 'profile.deal_breakerss' — did you mean 'deal_breakers'?"`;
   far-miss keys → one collapsed summary line. Warnings go to stderr on every
   command that loads the profile and to a badge in the dashboard header.
   Invariants: fingerprint computation unchanged, scoring behavior unchanged,
   warnings never block a run.
3. **Salary estimator (grounded, bruto anual only).** New
   `data/salary_reference.json` (gitignored, machine-local) holding tables
   extracted once from two public sources, versioned with
   `source`/`edition`/`captured`: Manfred *Guía Salarial 2026* (role ×
   experience tables, e.g. Security Engineer 2-5y €31-40K, 5-10y €41-55K,
   >10y €56-80K; SRE/DevOps 5-10y €46-60K; onsite +20% premium) and Nothiring
   *Informe mercado tech* (P25/mediana/P90 by role; secondary — its own N=241
   caveat applies). The LLM only classifies the offer (role, experience
   bracket, modality); the number comes from a deterministic table lookup.
   Output: always a bruto-anual **range** with per-row provenance ("stated by
   offer" / source+bracket), 12 vs 14 pagas normalized via the existing
   `_salary_period` logic; no stated salary and no table row → `unknown`,
   never a guess. Currency policy unchanged (`scoring/currency.py`: USD/EUR,
   no FX, hard-reject otherwise). Fills the "Later features: Salary and
   compensation analysis" slot. Refresh cadence: Nothiring monthly, Manfred
   each January (2027 guide lands ~5 months before applications start).
   **Acceptance (owner, 2026-10-20):** the classification step changes the
   prompt, so re-run `scripts/benchmark/` on the same offers and models before
   keeping the scoring model, and compare with the 2026-10-20 table in its
   README. Scoring quality (separation, scores of known-good offers, stability)
   decides, not speed.
4. **Gates: FAIL / FLAG.** FAIL fires only on an explicit profile
   deal-breaker or zero overlap with the profile; everything questionable
   becomes FLAG (⚠, stays ranked, quoted posting evidence persisted). The
   dashboard gains a veto-audit list; the owner promotes FLAG→FAIL weekly,
   writing the reason (a rulings log accumulates the criteria). Strictness is
   earned gradually after zero false-fails are observed.
5. **Divergence view (replaces per-offer review).** New dashboard view:
   offers where the heuristic and the LLM scorer disagree, plus a small
   random control sample of agreements. `calibration.py` extends with a
   scorer-vs-scorer comparison (read-only, same evidence-report policy).
   Human cost target: one ~10-minute weekly session; no offer-by-offer
   evaluation.
6. **Redo the dashboard HTML UI (owner request, 2026-10-20).** Redesign the
   generated report (`dashboard.py`, ~2,900 lines) rather than adding views on
   top of the current one; items 1 and 5 should land in the new UI, not the
   old one. Contract unchanged: self-contained, offline, safe over `file://`,
   feedback still exported through `import-feedback`. Scope and design to be
   agreed with the owner before any code.

### Carried-over priorities (unchanged, still first if a sweep session comes)

The 2026-10-16 sweep findings remain the immediate fixes: expose keyword
retirements in `keyword-plan --json` so the sweep skips dead
provider+query pairs (repo side done 2026-10-20: `retired_for` per planned
entry, see `odd/tasks/keyword-plan-retirements.md`; the sweep script change is
KR-4), and the LinkedIn 429 watch item (widen keyword spacing, never touch
pacing).

## Next session (2026-10-16) — sweep findings

Audit of the 2026-10-01 → 2026-10-13 sweeps (`reports/sweep-*.log` plus the cron
digests). All five runs completed and were delivered — nothing was lost — but
three root causes are behind "the sweep looks dead", in priority order:

1. **`keyword-plan` hides per-provider retirements, so the sweep fires dead
   queries.** `keywords.retired_search(provider, query)` knows a pair is
   retired (the dashboard already consumes it), but `keyword-plan --json` only
   exposes `family/label/group/query/command`, with no provider dimension. So
   the sweep runs every planned query against both providers, Tecnoempleo
   rejects the retired pairs with `invalid Tecnoempleo options: ... is retired
   ...` and exits 1, and the digest reports it as a collector failure — six
   sweeps in a row, in the same spot.
   Measured 2026-10-13: 4 of that sweep's runs were retired pairs — Azure
   Security Engineer, Junior Cloud Security Engineer, Platform Engineer and
   Security Infrastructure Engineer (retired 2026-09-29/30). Each costs a
   useless command and a false alarm.
   Fix: expose the retirement in `keyword-plan --json` (e.g. a
   `retired_for: ["tecnoempleo"]` field) and make the sweep skip that provider
   for those queries. Note: the digest labels a failure group with one query
   of the group, so "tecnoempleo:Platform Engineer x4" was really four
   *different* dead queries.

2. **LinkedIn throttling is now the bottleneck, and it costs offers.** HTTP 429
   per sweep: 0 (10-04), 0 (10-07), 1 (10-10), **4 (10-13)**. In the last one
   both the search (page 2, `start=25`) and the detail run hit it; the detail
   run aborts partway (`failed_closed`, ~9 of 20 selected enriched). Variants
   without a full description: 63 (10-07) → 83 (10-10) → 88 (10-13), all
   LinkedIn. **53 active offers have no enriched variant at all**, and the
   scorer reads stored text only, so they can never surface as candidates.
   This is the one that actually starves the output.
   Standing policy applies (AGENTS.md: no retries, no pacing changes; the
   2026-10-10 watch item): widen keyword spacing / cut the number of LinkedIn
   queries per sweep and let the backlog drain — do not touch pacing.

3. **The "no human decision" block repeats the same five offers every sweep.**
   `notify` is stateless by design ("no schema or notification state of its
   own"), so the head of the high-value selection is identical every 72 h:
   #977 Mapfre 88, #90 METRICA 85, #661 Nubosas 85, #689 IGNIS 85, #868 GMV 85
   — all undecided since 2026-09-18…25. The digest therefore reads as "no new
   offers" even in sweeps that found 33 new ones.
   Fix is in the cron script, not the repo: keep a state file of what has
   already been reported and show only what is new since the previous sweep.
   Cheaper stopgap: mark a human decision on those five (they leave the
   selection and the next ones appear).

## Next session (2026-10-10)

Directions decided after the 2026-10-10 ponytail-audit refactor
(dead code removed, shared `collectors/htmlutil.py`, parameterized CLI runners;
suite green at 1100 passed). In order:

1. **GetManfred live confirmation + partial-offer re-enrichment.** The detail
   adapter is implemented and offline-covered, but needs one real
   `--confirm-network` run before re-enriching the ~128 partial GetManfred
   offers. This is the most concrete payoff: it turns partial offers into
   fully scored ones.
2. **Auto re-score on profile/input change.** Input-aware scoring selection
   already detects the fingerprint in the database; the missing piece is
   marking offers "needs rescore" (with a placeholder status/flag exposed in
   `list`/`dashboard`) instead of leaving stale scores until a manual
   `--rescore`. Verify in the same sweep as step 1: with the GetManfred
   enrichment changing inputs, refresh should trip the flag.
3. **Expose OpenCode model/effort as CLI flags** (`--scorer-model/--effort`).
   Small and self-contained; the env-driven variant is documented under
   "Scoring effort and model selection" below. Makes A/B scoring comparisons
   practical without env fiddling, and sizes the per-offer timeout for the
   chosen effort.
4. **Keep the manual keyword habit** (deliberate design, no fan-out):
   continue the 3–4 day sweep cadence and prune the worst quartile as it
   re-emerges. Watch item: LinkedIn failed closed on HTTP 429 for one query in
   the 2026-10-10 sweep — correct behavior, but if it repeats across multiple
   queries, widen keyword spacing rather than touching pacing.

Not in scope without re-decision: InfoJobs (needs official API authorization),
splitting large files (`dashboard.py`/`db.py` are large but monolithic by
design), further keyword fan-out (see "Keyword coverage").

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

- generate nearby variants from terms that work, but keep human approval before
  activating a new live query (the catalog is the review gate);
- no batch/live fan-out: one explicit `search-linkedin --confirm-network` per
  query, by design.
- (done) drop the cycle date from the keyword identity and filter the smoke cycles out
  of the analytics. `_source_key()` keys on `provider|keyword` (provider and term are the
  identity, the cycle is only provenance), and every analytics query excludes
  `LOWER(TRIM(COALESCE(r.cycle_id, ''))) NOT LIKE 'smoke%'` so probe runs no longer
  inject unsearched terms into the table. Per-offer provenance still carries the real
  `cycle_id`.
- (done) prune the bottom of the catalog with thresholds read from the metrics that
  already exist: `jobscout prune` (offline, read-only preview) deactivates the
  worst-scoring `floor(25%)` of active scored offers on explicit `--apply`, and reports
  the worst `floor(25%)` of eligible keyword series **per provider** by ascending
  `avg_score`. Keyword candidates are a report only; the catalog is never mutated, the
  same literal query on another provider is never cut, and no cycle-count gate is
  required. The other existing metrics (`top5_avg_score`, `top10_avg_score`,
  `duplicate_rate`, `human_apply_rate`) are shown as context, with missing human data
  left unavailable rather than zero.
- (done) never propose a keyword cut that would cost a good offer: each candidate
  carries `unique_offers` (active offers no other query of the same provider surfaced)
  and `unique_good_offers` (`overall_score >= 45`); a worst-quarter series with a good
  offer moves to `held_back_keywords` instead of being proposed.

Current high-value query families include:

- `Junior Cloud Engineer`;
- `Junior DevOps Engineer`;
- `Platform Engineer`;
- Cloud Security and DevSecOps variants, including Azure and junior variants
  (`jobscout keyword-plan --family cloud-security --family devsecops`).

On 2026-09-30 the owner set the retirement rule: a `(provider, query)` pair is
retired only when cutting it would cost no good offer (`overall_score >= 45`). Seven
pairs are retired (Tecnoempleo: `Platform Engineer`, `Azure Security Engineer`, `IAM
Engineer`, `Junior Cloud Security Engineer`, `Security Infrastructure Engineer`;
LinkedIn: `Identity and Access Management Engineer`, `IAM Engineer`). They are
recorded in `RETIRED_SEARCHES` and refused by each provider's search-config
constructor; the same literals stay valid on unlisted providers and their history
stays in the dashboard analytics. `Cloud Security Engineer|linkedin` and `Platform
Engineer|linkedin` were restored, and `Cloud Security Engineer|tecnoempleo`,
`Identity and Access Management Engineer|tecnoempleo`, and `devops|tecnoempleo` stay
active because each costs good offers. The dashboard now defaults to every active
offer, with an explicit `--top N` still available. Retired series are excluded from
the keyword table and the three top-keyword lists by default; they stay in the payload
marked `retired` (date and reason included), `summary.keywords` still counts every
analyzed series, and the summary note reports the retired count.

A title-based senior/lead filter (`dashboard.is_senior_noise` over
`SENIOR_NOISE_MARKERS`) was tried in CP-9 and **deliberately removed in CP-10**:
the owner found it produced both false negatives (real noise slipped through)
and false positives (good offers were hidden), so a fuzzy title heuristic is the
wrong tool. Do not re-add a title heuristic for this purpose; seniority judgement
stays in the evaluator's prompt.

LinkedIn result position is provenance, not quality ground truth. Measured on
2026-09-30: good offers are spread across result positions (one scored 62 at ranks
49-56, two scored 55 and 58 at ranks 22 and 30, and one query had zero good offers in
its first ten), so a page or position cutoff would discard good offers and keep noise.
Do not reintroduce position-based filtering.

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
- Inspect keyword performance after each sweep; `jobscout prune` reports the
  worst quarter per provider without requiring a fixed number of cycles.
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
- (done) additive schema v5 provenance: `search_runs.cycle_id` and
  `search_run_items.position_kind` (nullable, historical rows untouched);
- (done) provider-scoped keyword analytics keyed by `provider|keyword`, so the same
  term is never merged across platforms, and `cycle_id` stays provenance only;
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

## Scoring effort and model selection

Status: verified locally on 2026-09-30; still environment-driven, no CLI flag.

- The best 100 active offers were re-scored with
  `JOBSCOUT_OPENCODE_MODEL=opencode/muse-spark-1.3-contributor-free` and
  `JOBSCOUT_OPENCODE_VARIANT=max`: 100/100 `status='ok'`, new mean 65.39, 99 of them
  at or above 45, in about 67 minutes (~40 s per offer).
- The paid `opencode/muse-spark-1.3` answers HTTP 402 "Insufficient account funds";
  only the contributor-free variant responds.
- `score --all --limit N` orders by `id` (`db.all_active_jobs`), so it re-scores the
  *oldest* offers. To re-score the best ones, pass explicit `--job-id` values taken
  from `jobscout rank --top N --json`.
- The effort is recorded in `evaluations.prompt_version` (for example
  `opencode-v5-seniority-aware+max`), which is how a pass can be proven afterwards.
- Serialise scoring runs: two concurrent scorers on one database mix models across the
  same offers and contend for the same SQLite write lock.
- Open: expose model and effort as CLI flags, and size the per-offer timeout
  (`JOBSCOUT_OPENCODE_TIMEOUT_SECONDS`, 180 s) for the chosen effort.

## Keyword coverage

Status: deferred by the owner on 2026-09-30; nothing was added.

Wider term coverage is still wanted, but it is a deliberate trade rather than a
default: the catalog caps a family at `HARD_MAX_QUERIES_PER_FAMILY` (3) and there is
no keyword fan-out, because every added term is one more live request against a
provider that answers 403/429. If coverage grows, decide first whether the cap moves,
which providers are queried per family, and what the review gate is before any term is
added.

## Publication and maintenance

- Keep `profile.yaml`, `.env`, SQLite databases, raw captures, and generated
  reports ignored by Git.
- Run the full offline test suite before each release.
- Re-run the security audit after changes to collectors, subprocesses, URLs,
  secrets, or report rendering.
- Never commit `jobscout-feedback.json`, private profiles, databases, raw captures,
  generated reports, or unrelated local files.
- Keep the private GitHub repository synchronized only after reviewing commits.
