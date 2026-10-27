# Next Steps

This document records the next improvements for `jobscout` after the first
working MVP.

## Session 2026-10-27 — scorer on OpenCode Go, bottom-half analysis, cleanup plan

- **Scorer model (owner, permanent):** `.env` now uses
  `opencode-go/deepseek-v4.1-flash` (paid OpenCode Go, no shared free quota;
  owner: quality first, token cost does not matter). This closes "choose the
  new scorer" from 2026-10-23. The 38 active offers that had never been scored
  (ids 1117–1170) were scored with it: 38 ok after one retry (#1136,
  `ECONNRESET`); 1 apply, 26 consider, 10 skip, 1 need_more_info. Every active
  offer now has a score; dashboard regenerated.
- **Bottom-half analysis (758 active offers, latest ok score each, scores from
  four models, only 38 from Go):** median 35; deciles 15/18/22/28/35/48/55/58/64.
  Below the median: 383 offers, 375 skip, 8 consider. The model's reason is
  seniority/years of experience in 324 of 383; title words almost only found
  there: senior 118 vs 1 above, architect 19/1, lead 12/0. Language, location
  and clearance are under 10 each. By source, Tecnoempleo has 143 of 179 below
  the median (its text search also matches technologies). LinkedIn
  "IAM Engineer" / "Identity and Access Management Engineer" have 22/23 and
  22/25 below (median 22), but both are already out of the keyword plan; those
  offers are leftovers. LinkedIn queries without "Junior" have a median of
  24–30; the "Junior …" queries 58–62; GetManfred 55.
- **"Discard" means hide, never delete** (project contract): filtered offers
  keep their rows and history and can be shown again.
- **Next (owner, 2026-10-27; replaces the 2026-10-23 list below):**
  1. **Seniority pre-filter** (spec: item 2 of the 2026-10-23 list). Filtered
     offers are not sent to the model, which also saves Go calls.
  2. **Tecnoempleo function browse** (spec: item 1 of the 2026-10-23 list),
     then the Hermes sweep script, changed once after merging.
  3. **Residual query review:** after 1, measure again by query which LinkedIn
     searches still put most offers below the median (candidates: "Azure Cloud
     Security", "Azure Security Engineer", "Cloud Security Engineer",
     "Security Infrastructure Engineer", "Platform Engineer"), and the owner
     decides which ones to retire through `RETIRED_SEARCHES`.
  4. **Full re-score with `opencode-go/deepseek-v4.1-flash`** once 1–3 are
     merged: a one-off manual `score --all` run (not capped at 180), outside the
     sweep window, so every offer is scored by the same model and the
     scores can be compared again. Then regenerate the dashboard and repeat the
     percentile analysis.
  5. Scoring queue (never-scored offers first): less urgent without the free
     quota, but `score --all` still walks by `id` under the 180 cap.
  6. Application-drafts plan (unchanged).

## Session 2026-10-23 — scorer model gone, free quota, scorer permissions, Tecnoempleo search study

- **Scorer model (owner, provisional):** `opencode-go/longcat-2.5-preview-free`
  no longer exists (`provider.no-route: Model unavailable`; not in
  `opencode models`). `.env` now uses `opencode/deepseek-v4.1-flash-free` with no
  fallback, because every free OpenCode model shares one quota.
- **Free quota:** about 55 calls, shared by all free OpenCode (Zen) models, then
  "Free usage limit reached" for hours (still exhausted 1.5 h later; looks
  daily). The 2026-10-22 sweep scored 176: 59 ok (48 of them re-scores of old
  offers, 11 new), 117 failed; 34 offers from that sweep still have no score.
  `score --all` walks offers by `id`, so a model change re-scores old offers
  before new ones reach the cap.
- **Pending: choose the new scorer.** Benchmark the current free models
  (deepseek-v4.1-flash-free, big-pickle, glm-5.3-flash-free, mimo-v2.6-flash-free,
  nemotron-3-ultra-free, nemotron-3.5-super-vl-free, nemotron-3.5-lightning-free,
  flint-40b-free; beam-free was rejected by the owner) with
  `scripts/benchmark/bench.py --quota-wait 600`, one model at a time and never
  in the quota needed by a sweep (~2 models per day). A first parallel attempt
  was invalid (all models hit the shared limit after 54 answers) and was stopped
  on owner request; no result is usable yet.
- **Pending: scoring queue (needs owner decision).** Score never-scored offers
  first (ordered by the heuristic score), model-only re-scores last, and stop the
  run at the first free-quota error instead of failing the rest of the batch.
- **Scorer permissions (done, merged; suite 1167 passed; one real scoring call
  on a database copy returned `ok` in 8 s):** the private `opencode.json` gets OpenCode 2.x
  `permissions` rules. The model keeps read/grep/glob/webfetch (owner: keep
  them, they may be useful). Denied: shell, write, edit, execute (which reaches
  OpenCode Console admin tools), subagent, skill, question, websearch (HTTP 402),
  external_directory, doom_loop, and reads of `*.env`/`*.pem`/`*.key`/`auth.json`.
  The file is rewritten when it differs (it used to be written only once), and
  the CLI runs from a private empty `data/opencode-work` instead of `/tmp`.
  Verified live: `policies` does not remove tools, and a catch-all `*` deny
  hangs every run.
- **Owner decision:** the scorer and company research may use the internet when
  it improves results; the scorer will read a per-company dossier instead of
  browsing per offer. AGENTS.md still has to record it.
- **Tecnoempleo search study (live, 2026-10-23, Madrid, parsed with
  `postings_from_html`):** its text search matches the offer name, profile,
  position *and technologies*, which is where the Java/Data/Android noise comes
  from. Of the 13 known-good offers (score >= 50) still published: current
  text search (4 of 14 queries, page 1) found 9; quoted phrases 3; the
  professional-function browse (`?pr=,263,&es=,34,43,40,39,8,45,19,`: DevOps,
  Técnico de Sistemas, Ciberseguridad, Redes, Soporte Técnico, Administrador,
  Helpdesk) 12, with no development roles and 265 offers never collected before;
  function browse + experience <= 2 years 2. Results are newest first, so about 5
  pages cover a sweep interval (fewer requests than today's up to 28). The only
  miss is "Dixcover - Programa Prácticas FP Accenture" (another function): keep
  one "prácticas" text query. The title seniority rule hides 154 of 711 scored
  offers with no loss of any offer scored >= 50 or marked apply/consider.
  **Not applied yet** (see Next, item 1). The live pages and the fetch script
  are kept in `captures/tecnoempleo-2026-10-23/` (gitignored, local only).
- **CV:** copied to `cv.md` (gitignored) from the Obsidian vault; source of
  truth for later tailored drafts.
- **Next:**
  1. **Tecnoempleo: replace the 14 text queries with the function browse.**
     Collector: `TecnoempleoSearchConfig`/`build_search_url` gain a browse mode
     that emits `https://www.tecnoempleo.com/ofertas-trabajo/?pr=,263,&es=,34,43,40,39,8,45,19,&pagina=N`
     (today only `te` and `pagina` are supported, `tecnoempleo.py:137-155`);
     5 pages per sweep, 10 for the first backfill (within the hard cap of 10);
     robots handling and pacing unchanged. Keep one text query, "prácticas",
     for internship programmes filed under other functions. Keyword plan and
     the Hermes sweep script then run the browse instead of the per-keyword
     Tecnoempleo searches (sweep script changed once, after merging). Offline
     tests use synthetic fixtures; the live evidence is the capture above.
  2. **Seniority pre-filter (all providers):** titles with senior, sr, lead,
     principal, architect/arquitecto, manager, head, director, staff or jefe,
     unless they also say junior/jr/trainee/beca/becario/prácticas/intern, are
     marked filtered with the reason, never sent to the model and never
     deleted; the dashboard hides them behind a count. Measured: 154 of 711
     scored offers, none scored >= 50 or marked apply/consider; on the
     Tecnoempleo function browse 329 -> 272 with all 12 known-good kept.
  3. **Scoring queue** (pending owner decision, above), needed before the
     Tecnoempleo backfill: 265 new offers at ~55 free calls a day.
  4. **Choose the new scorer** (pending, above).
  5. Then the application-drafts plan: company dossiers with internet (the
     scorer reads them), tailored CV and cover letter in Markdown from
     `cv.md`, interview prep, LaTeX later.

## Session 2026-10-21 (d) — salary estimate in the OpenCode prompt, profile skills

- **Overnight re-score:** all 700 active offers scored 01:03–04:04 (452 primary,
  248 longcat after one primary error, 0 left), dashboard regenerated.
- **Profile (owner, reverses the sep-2026 "no skills" decision):**
  `must_have_skills` Azure, Linux, Python, Git, Networking, GitHub Actions,
  CI/CD; `nice_to_have_skills` Docker, Terraform, Kubernetes, Prometheus,
  Grafana, Bash, PowerShell, Microsoft Sentinel, Microsoft Entra ID;
  `require_all_must_have` stays false; `salary_currency: EUR`.
- **Salary estimate (roadmap item 3, owner redesign):** no fixed table. The
  model estimates low/mid/high gross annual EUR, `employer_tier` (six tiers
  after Manfred's hexamodal distribution) and a one-line basis, guided by
  `SALARY_ESTIMATE_GUIDE` in `scoring/opencode.py`: tier bands, Manfred 2026
  role anchors, Nothiring Aug-2026 medians, stated-pay offers in our own DB,
  the five weighted factors (employer 30, stack 25, English 20, experience 15,
  on-call/onsite 10). Informational only: it never changes the score
  (owner). Prompt version `opencode-v6-salary-estimate`; DB schema v7 adds
  five nullable columns to `evaluations`. Dashboard: stated salary green,
  otherwise `≈ low–high k` grey "estimado"; the body shows the range, the
  tier and the basis.
- **Live probe (DB copy, 10 offers):** Devoteam junior DevOps 22–27k,
  Accenture Azure migration analyst 22–27k, Dempo junior cloud 25–35k (read
  from the description; `jobs.salary` was empty), Mapfre mid platform
  34–43k, Nubosas stated 23–26k respected.
- **Models (owner, permanent):** `.env` now has longcat-2.5-preview-free as
  primary and deepseek-v4.1-flash-free as fallback (quality over speed).
- **Glassdoor/Indeed:** not queried per offer (the scorer never fetches);
  Indeed showed no pay on Madrid junior listings.
- **Re-score with the estimator (12:05–20:32):** 582 offers without stated pay
  (the 118 with a salary, mostly GetManfred, were skipped). 375 longcat until
  its free quota ran out (`provider.quota: Endpoint is unavailable`, ~18:47),
  31 deepseek, 176 minimax after deepseek's first error; 0 left. The sweep
  will re-score the 207 non-longcat ones with longcat over the next cycles.
- **Next:**
  1. Re-run the model benchmark with longcat now that `bench.py` migrates its
     DB copy (the earlier run scored 0/30 for that reason).
  2. LinkedIn detail adapter: extract a salary written in the description
     (e.g. Dempo's "€25,000 – €35,000") into `jobs.salary`. Needs live markup
     captured once and pushed through the parser offline.

## Session 2026-10-21 (c) — dashboard DR-7: closer to the mockup

- **Why:** the owner still found the mockup prettier than DR-6. Cause: the
  tokenised CSS had rounded the mockup's spacing down, `--ink-3` had been
  darkened, and the toolbar overflowed (no wrap, three extra controls, and a
  `filter-keyword` select as wide as GetManfred's 309-character filter list).
- **Owner decisions:** spacing back to the mockup values; `--ink-3`/`--low`
  `#7a766c` (between mockup `#8a867b` and DR-6 `#69655d`); the
  `revisadas/sin revisar` select, the `Solo sin decisión importada` toggle and
  the toolbar `limpiar` are removed (`limpiar` stays in the empty state);
  `Score ≥` is a 0/50/70 select; embedded OFL fonts Charis SIL (serif),
  Inter (sans) and DM Mono (figures, picked from a five-way specimen over
  JetBrains Mono); stated salary in a new `--money` green, with a CSS-only
  `.sal.est` reserved for the estimator; back-to-top floats bottom-right.
- **Keywords:** GetManfred's comma-separated keyword shows as the first two
  terms plus `+N`, full list in the tooltip, in the table and the filter.
- **Verified:** full suite, plus headless Chromium screenshots (scratchpad
  Playwright, not a project dependency). Report grows to ~940 KB.
- **Open:** the page is still not usable at phone width (row grid squeezes);
  the salary estimator was implemented in session (d).

## Session 2026-10-21 (b) — scoring diagnosis, DP card audit

- **Scoring diagnosis:** the 2026-10-19 sweep failed 21/21 because OpenCode
  2.0.32 (installed 2026-10-18) rejects `--pure`/`--variant`; the fix (d1e2798)
  landed 2026-10-20 09:05, after that sweep. The only later evaluations are
  the 2026-10-20 manual probe: primary failed with an expired Zen API key
  (since renewed), fallback succeeded. No `score` run has used the current
  configuration since. The model is part of the input fingerprint, so all 700
  active offers are due for re-scoring, 180 per sweep.
- **DP card:** DP-1..DP-3 were already in the code (afa279a, a4c700b); only
  the checkboxes were open. Verified on a database copy and ticked; no code
  change. Evidence in the card's progress log.
- **Owner decision:** the fallback is `opencode-go/longcat-2.5-preview-free`
  (best quality in the 2026-10-20 benchmark), replacing
  `opencode-go/deepseek-v4.1-flash#low`. Both models are free, so if both
  quotas run out a sweep scores nothing; check the `succeeded` count in
  `reports/sweep-2026-10-22.log`.
- **Archived:** `dashboard-redesign.md`, its mockup,
  `keyword-plan-retirements.md` and, closed, the DP card
  (`detail-provenance-and-scoring-determinism.md`) moved to `odd/tasks/done/`.
  No open task card remains.
- **Lesson:** check `git log -S` for the fix before implementing a card task:
  a card can be older than the code it describes.

## Session 2026-10-21 — dashboard redesign DR-6: speed pass

Done (`odd/tasks/dashboard-redesign.md`, DR-6): the payload ships only what the
page reads. `offer.keywords` (one entry per sighting, 59 % of the page) became
`offer.keyword_keys`; `preference.schema`/`rules` and eight unread offer fields
are gone. Page 2.16 → 0.77 MB; payload `JSON.parse` in node 18.9 → 6.0 ms. All
steps of the redesign card are done.

- **Lesson:** measure bytes per payload field before choosing what to trim: one
  field held 59 % of the page, everything else together was a rounding error.
- **Lesson:** a field rename is checked by replaying the old and new filter over
  the same database copy (28 series, same offers matched), not only by the suite.
- **Open (owner):** `offer.detail` (link evidence, 65 KB) has had no UI since
  DR-3; dropping it removes a documented feature, so it waits for a decision.
- **Open (owner):** first paint in a real browser was not measured;
  `reports/dr6-antes.html` / `dr6-despues.html` are there for it.
- **Next:** move the card to `odd/tasks/done/`, then the next card.

## Session 2026-10-20 (h) — dashboard redesign DR-5: decision + conditional export

Done (`odd/tasks/dashboard-redesign.md`, DR-5): the header shows `Exportar N
decisiones` only while this browser holds a decision (N counts local entries,
legacy included, never imported ones); "Solo sin decisión importada" is a
segmented control; the decision reason is capped at 500 characters; the saved
confirmation fades out. Export tests untouched (the old label lives on in the
button's `title`).

- **Lesson:** the card counted two look-pinning tests for the toggle; a grep
  over all of `tests/` found a third (the CSS hue/media guard read
  `.filter-toggle` rules).
- **Lesson:** a pinned timer line can be kept by adding a second timer rather
  than rewriting it (the fade runs at 2450 ms, the removal stays at 2600 ms).
- **Known:** exporting/importing never clears `localStorage`, so the export
  button stays visible after an import. Clearing it is an open owner decision.
- **Next:** DR-6 (speed pass).

## Session 2026-10-20 (g) — dashboard redesign DR-4: Palabras clave view

Done (`odd/tasks/dashboard-redesign.md`, DR-4): the keyword tab is the mockup's
sortable table (Keyword, Proveedor, Vistas, Activas, Score medio, ≥ 70,
Aplicadas), opened on Score medio descending, with retired pairs greyed in place.
The three Top 10 lists, the methodology text and `top_*` in the payload are
gone; the payload gains `seen_jobs` and `high_score_jobs`.

- **Lesson:** the token test forbids the `font:` shorthand, so mockup rules
  must be split into longhands when ported. A test outside the agreed list
  (`test_legacy_consider_is_displayed_as_historical_and_replaceable`) pinned
  text that only the removed Top 10 list rendered; only the full suite caught it.
- **Lesson:** deleting a test can drop a guarantee another part of it carried
  (`avg_score is None` for an unscored series); keep that half under a new name.
- **Next:** DR-5 (minimal decision + conditional export).

## Session 2026-10-20 (f) — dashboard redesign DR-3: offer row

Done (`odd/tasks/dashboard-redesign.md`, DR-3): offers are one-line `<details>`
rows ported from the mockup, rendered 50 at a time, with the body built on first
open (full rationale, stated salary, dimensions or `sin desglose`, lists only when
present, footer with apply/reject). AA colours from DR-2 applied. Avisos box,
imported-review note, detail evidence and the old cards removed.

- **Finding:** only 5 of 700 offers have dimensions or lists; most rows open to
  the rationale only. Same root as the pending dimensions investigation.
- **Lesson:** the DeepSeek ejecutor ran out of steps on the first pass, and its
  shell was denied even for the exact pytest command, so it never ran a test.
  The explorer's map missed `tests/test_preference.py`, which also pins the
  dashboard.
- **Next:** DR-4 (Palabras clave view).

## Session 2026-10-20 (e) — dashboard redesign DR-2: shell and visual system

Done (`odd/tasks/dashboard-redesign.md`, DR-2): new tokens, sticky header
with a status line built from `payload.status`, Ofertas/Palabras clave tabs,
one-row toolbar (with "Solo nuevas"), export in the header. Rail, KPIs,
explanation boxes and footer removed. Offer cards and the keyword view are
unchanged until DR-3/DR-4.

- **Pending owner decision:** three tokens fail AA (`--ink-3`, `--signal`
  and `--mid` on their washes); darker values proposed.
- **Next:** DR-3 (offer row).

## Session 2026-10-20 (d) — dashboard redesign DR-1: payload

Done (`odd/tasks/dashboard-redesign.md`, DR-1): the dashboard payload gains
`offer.is_new` (first seen at or after the latest non-smoke `cycle_id`'s
first run), `offer.salary`, `offer.dimensions` and `payload.status` (latest
sweep, new and new-scored counts, pending details per source, failed runs).
Unused fields dropped; `detail.http_status` stays until DR-3. UI unchanged.

- **Finding, not investigated:** the 2026-10-19 sweep scored 0 of 21
  (`scored 21 job(s), 0 succeeded, 21 failed`), so the 18 new offers are
  unscored. Check the next sweep log (the OpenCode key was renewed).
- **Next:** DR-2 (shell and visual system) in a new session.

## Session 2026-10-20 (c) — dashboard redesign: scope and design agreed

No code changed. The owner approved the redesign card
`odd/tasks/dashboard-redesign.md` and its static mockup
`odd/tasks/dashboard-redesign-mockup.html` (synthetic data).

- **Owner decisions:** desktop only; light theme only (tokens leave room for
  dark mode later); apply/reject kept minimal; the visual system is
  written into the card (inspired by design-taste and Ramp's refero
  DESIGN.md, no package or web font adopted).
- **Finding, not investigated yet (owner):** only 5 of 700 active offers
  have a non-empty `dimensions_json` in their latest evaluation; none was
  written after 2026-10-01.
- **Finding:** no heuristic evaluations are stored (all 3,207 are
  `opencode`), so the divergence view (item 5) needs its backend first.
- **Next:** DR-1 (payload only) in a new session.

## Session 2026-10-20 (b) — sweep fixes: retirements, LinkedIn pagination, detail backlog

Done:

- `keyword-plan --json` exposes `retired_for`; the sweep skips retired
  provider+query pairs (`odd/tasks/keyword-plan-retirements.md`): 22 → 18
  search commands per sweep, no more false Tecnoempleo failures.
- **LinkedIn pagination fixed.** The collector stepped `start` by 25, but every
  stored live page (266 of them) holds exactly 10 cards, so results 10-24 of
  every search were never seen. `DEFAULT_PAGE_SIZE` is now 10: same number of
  requests, contiguous top-20 results. Live check on the next sweep: two-page
  LinkedIn runs should still report 20 items (no in-run duplicates).
- **LinkedIn detail backlog (113 pending variants, 69 active offers with no
  enriched variant).** Outside the repo, see AGENTS.md: the sweep now fetches
  LinkedIn details *before* its searches (the 10-13/10-16 detail runs died on
  429 after the searches), and a new Hermes job runs only that 20-item batch
  ~36 h after each sweep, doubling the drain without touching pacing. The
  sweep's "no human decision" block and its Telegram delivery are removed.
  Watch: the pending count in the next sweep logs should fall.

Owner decisions: the OpenCode Console API key is renewed (2026-10-20); the
roadmap's "unknown profile-key warnings" (2026-10-18 item 2) is dropped:
`profile.yaml` is only ever edited by an AI agent working from
`profile.example.yaml`, so typos are unlikely. Its only unknown key, `source`
(provenance note, never used by the scorer), was moved into the file's header
comment while every offer is already pending re-score after the model change.
Keep the 8 s pacing; the `*/3` day-of-month cron quirk (day 31
and day 1 run 24 h apart) is accepted; no Telegram notification from the sweep.

Pending, owner to decide (not started):

1. **GetManfred inventory filter is effectively dead.** Eight sweeps in a row
   (2026-09-29 → 10-19) returned the same 23 matches and 0 new, while the
   sitemap gained ~22 offers. The filter requires every token of a query in the
   URL slug, so only `Platform Engineer` ever matches (the others carry
   `Junior`/`Azure`), and most of the 23 matches date from 2022-2025 because the
   sitemap (1675 offers) keeps old offers. Relevant slugs are missed
   (`fcm-sre-devops-engineer-jul26`, `valsea-technologies-devops-engineer-jun26`,
   `todo-en-cloud-sre-devops-ago26`; ~178 slugs match looser terms). Separate
   runs per query would not help: it is one sitemap download filtered locally.
   Proposal: GetManfred-specific short terms (devops, sre, cloud, security,
   platform…) plus a `lastmod` recency cut; still one request. Side effect to
   keep in mind: the run's `query` is the whole comma-joined list, so
   dashboard/prune see one `getmanfred|a,b,c` series, not per-keyword series.
2. **Detail cap mismatch in the sweep.** The script passes up to 40 ids
   (`JOBSCOUT_SWEEP_DETAIL_CAP`) but `fetch-details` processes 20
   (`detail_max_items`) and drops the rest ("20 selected job(s) omitted by
   --max-items"). Harmless but misleading; align the script cap with the
   command's.

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
- **Owner pending:** ~~renew the OpenCode Console API key~~ (done 2026-10-20).
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
2. **(Dropped 2026-10-20, see the session above.) Unknown profile-key warnings.** `scoring/profile.py` warns at load time
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
   agreed with the owner before any code; agreed 2026-10-20, see
   `odd/tasks/dashboard-redesign.md` (steps DR-1..DR-6).

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

A title-based seniority pre-filter was tried in CP-9, removed in CP-10, and
**reinstated by the owner on 2026-10-27** (CP-10 reversed). The current rule,
its junior exceptions, and the score/dashboard behaviour are specified in
`odd/tasks/done/seniority-prefilter.md`.

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
