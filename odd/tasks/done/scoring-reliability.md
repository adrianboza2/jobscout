# Scoring reliability

## Objective

Make every persisted offer evaluation actionable and structurally valid: no successful evaluation may be stored as “need more information” with a placeholder score or remain indefinitely without a score after a bounded recovery flow.

## Problem

The LLM/OpenCode contracts currently permit `need_more_info` and require a numeric score at the same time. Failed or malformed scorer responses are also separated from the dashboard's latest successful evaluations, which makes them appear as “sin puntaje”.

## Scope

- Tighten scorer output contracts and schema validation.
- Add bounded repair/retry handling for invalid model output.
- Use the deterministic heuristic scorer as the final fallback.
- Preserve failure provenance without presenting it as a valid evaluation.
- Add database/dashboard regression tests.
- Reprocess existing offers and regenerate the dashboard.

## Constraints

- Never invent facts from a job posting.
- Do not retry access, authentication, or rate-limit failures as schema repairs.
- Keep retries bounded and preserve raw/error provenance.
- A score of zero must not be used as an “unknown” placeholder.

## Tasks

- [x] SR-1 Define one canonical valid evaluation invariant and enforce it in schema/scorer adapters.
- [x] SR-2 Add bounded repair attempts for malformed or contract-invalid scorer output.
- [x] SR-3 Add deterministic heuristic fallback with explicit provenance.
- [x] SR-4 Prevent invalid evaluations at persistence and render failed/fallback states honestly.
- [x] SR-5 Add regression tests for invalid combinations, retries, fallback, and existing data.
- [x] SR-6 Reprocess affected offers and regenerate the dashboard.
- [x] SR-7 Score the 14 active offers that had never been evaluated.

## Acceptance criteria

- No `status=ok` evaluation can have a missing score or an unknown recommendation.
- `need_more_info` cannot be persisted as a successful public recommendation.
- A bounded scorer failure ends in a heuristic result or an explicitly visible technical failure, never a misleading `0` placeholder.
- Existing affected offers are re-evaluated and the generated dashboard no longer contains the reported ambiguous state.
- Focused and full test suites pass, or failures are documented with their cause.

## Checks

- `python -m pytest -q`
- Query the local database for invalid successful evaluations.
- Regenerate the dashboard and inspect aggregate counts and representative offers.

## Progress

SR-1 to SR-5 implemented and verified offline. SR-6 executed against the local
database on 2026-09-21 (121 live-affected active offers reprocessed with the
configured OpenCode scorer; dashboard regenerated with no ambiguous state).
SR-7 executed on 2026-09-21 as well: the 14 active offers with no evaluation at
all were scored, so the full 512-offer active corpus is now scored.

### SR-1 — one canonical invariant

- `jobscout.models` defines the single source of truth: `PUBLIC_RECOMMENDATIONS`
  (apply/consider/skip), `is_public_recommendation`, and
  `evaluation_contract_errors`. A `status=ok` evaluation must carry a numeric
  `overall_score` and a public recommendation; `need_more_info` is never a
  successful public recommendation, and `0` is a real score, not a placeholder.
- `jobscout.scoring.schema.success_contract_errors` is the payload-shaped half,
  called by both the LLM and OpenCode adapters, so a structurally valid answer
  that resolves to `need_more_info` becomes a contract-invalid `parse_error`
  instead of a persisted success. The scorer prompts now require a decided
  verdict (apply/consider/skip) and state that 0 is an incompatibility.
- `EvaluationStatus.NEEDS_MORE_INFO` records an unresolved but scored result
  honestly; `latest_evaluations` and `jobs_needing_evaluation` keep treating it
  as not successful.

### SR-2 — bounded repair

- `jobscout.scoring.evaluate_job` retries a contract-invalid response at most
  `MAX_CONTRACT_ATTEMPTS` (2) times. Only `parse_error` / invariant-violating
  output qualifies; transport, access, authentication, and rate-limit failures
  (`status=error`) are never retried and are reported as technical failures.

### SR-3 — deterministic heuristic fallback

- When the bounded repair budget is exhausted, the pipeline re-scores the same
  stored posting with the offline heuristic. It keeps the requested scorer as the
  pipeline identity, marks the producer explicitly (`model="heuristic-v1+fallback"`),
  preserves the original failure in `error`, and notes the fallback in the
  rationale. It never invents facts and never calls the network.
- A heuristic result that is itself unresolved is persisted as
  `status=needs_more_info` with its real score, never as `ok`.

### SR-4 — persistence and honest rendering

- `jobscout.db.insert_evaluation` raises `EvaluationContractError` for a
  `status=ok` row that lacks a score or carries a non-public recommendation, so
  the misleading state cannot be persisted.
- `jobscout.calibration` continues to read scored-but-unresolved rows (now
  `ok` or `needs_more_info`) so the unresolved bucket keeps working.
- The dashboard serializes the latest evaluation status of any kind and renders
  "Requiere más información", "Evaluación fallida", "Evaluador sin configurar"
  and "Sin puntaje" distinctly; the stored error text is never embedded. The
  summary breaks scoreless offers into incomplete/failed/never-evaluated counts.

### SR-5 — regression tests

- Schema contract tests (reject `need_more_info`, reject a missing score, accept a
  real `0` with `skip`).
- LLM/OpenCode adapter tests for contract-invalid `need_more_info` output.
- Pipeline tests for bounded retry then success, heuristic fallback with preserved
  provenance, no retry on transport failures, and unresolved results stored as
  `needs_more_info`.
- Persistence tests for refusing `ok` without a score / with `need_more_info` and
  accepting an explicit `needs_more_info` status.
- Dashboard tests for the per-offer `evaluation_status`, the summary state counts,
  honest labels, no error-text leak, and visible fallback provenance.

### Verification

- `python -m pytest -q` → 930 passed.
- Read-only query of `data/jobscout.sqlite3`: 1047 evaluations, 883 `ok`, of which
  **176 are legacy invalid `ok` rows** (`recommendation='need_more_info'`, 36 with
  score 0) plus 156 `parse_error` and 8 `error`. The guard refuses new rows in
  this shape; the legacy corpus still needs SR-6.

### SR-6 — reprocess affected offers and regenerate the dashboard

Executed 2026-09-21 on `data/jobscout.sqlite3`. No historical evaluation row was
overwritten or deleted; every repair is a new row inserted through the pipeline.

- **Selection (read-only, before the run).** 1047 evaluations: 883 `ok`
  (176 legacy invalid `ok` with `recommendation='need_more_info'`, 36 of them
  score 0), 156 `parse_error`, 8 `error`. Because `latest_evaluations` only
  selects `status='ok'`, only the rows that are still the *latest* evaluation of
  an active offer reproduce the reported state: **121 active offers** were live
  affected — 109 whose latest evaluation was a legacy invalid `ok` (25 of those
  with score 0) and 12 whose latest evaluation was a failure. The other 67
  legacy-invalid rows and the failures on the remaining 129 jobs were already
  superseded by a newer valid evaluation and were deliberately **not**
  re-evaluated (their live state was already valid). Active offers with no
  evaluation at all (14) are out of scope: the dashboard already renders them
  honestly as "Sin puntaje".
- **Scorer decision.** The project-configured scorer is OpenCode
  (`opencode/mimo-v2.5-free`, `--pure`, `--variant minimal`). It was probed and
  is available (CLI 1.18.30) and returns a decided public verdict even on thin
  postings (jobs 580/581: `consider`, ~11–24 s/call; job 12: `consider`, ~38 s).
  The deterministic heuristic was measured first (read-only prediction): it
  resolves only 11 of the 121 offers and returns `need_more_info` for 110 of
  them. A `needs_more_info` insert cannot supersede an older legacy-invalid `ok`
  row in `latest_evaluations`, so the heuristic fallback alone would leave the
  reported ambiguous state visible. The configured OpenCode scorer was therefore
  used, not the heuristic fallback.
- **Reprocessing.** `python -m jobscout score --job-id … --scorer opencode --json`
  in one pilot (job 12) plus six batches of 20 ids: **121/121 succeeded**, 0
  failures. Batch wall-clock: 557 s, 510 s, 550 s, 582 s, 581 s, 468 s.
- **New rows (provenance).** `evaluations` grew 1047 → **1168** (+121), ids
  1307–1427, all `scorer=opencode`, `model=opencode/mimo-v2.5-free`,
  `status=ok`; recommendations `consider` 77 / `skip` 44; one real score of 0
  (job 592, `skip`, empty posting — a genuine incompatibility, not a
  placeholder). No `needs_more_info`, `parse_error`, or `error` was produced.
- **Dashboard.** `python -m jobscout dashboard --json` → `reports/jobscout-dashboard.html`
  (25 offers, 25 scored, 59 keywords, 196 316 bytes, **0 warnings**).
- **Verification.**
  - Full active payload (`build_payload(top=1000)`): 512 offers, 498 scored,
    `legacy_ambiguous=0`, `need_more_info` with a numeric score = 0,
    `need_more_info` with score 0 = 0; the 14 scoreless offers are all
    "never evaluated".
  - Embedded top-25 payload: 25 scored, 0 ambiguous,
    `apply` 5 / `consider` 20 / `skip` 0 / `need_more_info` 0.
  - The 176 legacy invalid `ok` rows (36 with score 0) still exist in
    `evaluations` as history; they are no longer the latest evaluation of any
    active offer, so the dashboard no longer renders the ambiguous state.
  - `python -m pytest -q` → **930 passed** in 13.59 s.

### SR-7 — score the never-evaluated active offers

Executed 2026-09-21 on `data/jobscout.sqlite3`. No historical evaluation row was
overwritten or deleted; every new evaluation is a new row inserted through the
pipeline.

- **Selection (read-only, before the run).** Read-only query
  (`jobscout.db.connect_readonly`): 512 active offers, of which **14 had no
  evaluation row at all** (`NOT EXISTS (SELECT 1 FROM evaluations …)`) and the
  same 14 were the only active offers without a `status='ok'` evaluation. All 14
  were `source=linkedin`, `content_status=partial`: jobs **688, 689, 691, 692,
  693, 694, 695, 696, 698, 699, 700, 701, 702, 703**. The remaining scoreless
  active count with a `latest_evaluations` join was 14, all with latest state
  `none` ("never evaluated"), matching the SR-6 verification.
- **Scorer.** The project-configured scorer (OpenCode, `opencode/mimo-v2.5-free`,
  `--pure`, `--variant minimal`; the SR-2/SR-3 retry and heuristic fallback stay
  active) was used, as required. No fallback was triggered.
- **Scoring.** `PYTHONPATH=src python -m jobscout score --job-id … --scorer
  opencode --json` in one pilot (job 688) plus three batches (5 + 5 + 3 ids):
  **14/14 succeeded, 0 failures**. Wall-clock per batch was under the 400 s
  timeout; the pilot returned in one call.
- **New rows (provenance).** `evaluations` grew 1168 → **1182** (+14), ids
  **1428–1441**, all `scorer=opencode`, `model=opencode/mimo-v2.5-free`,
  `status=ok`. Recommendations: `apply` 1 (job 688, score 78.0) / `consider` 13.
  Scores: 78, 65, 58, 72, 62, 55, 50, 50, 42, 60, 72, 55, 45, 42.
- **Dashboard.** `PYTHONPATH=src python -m jobscout dashboard --json` →
  `reports/jobscout-dashboard.html` (25 offers — the embedded `DEFAULT_TOP`, 25
  scored, 59 keywords, 189 793 bytes, **0 warnings**).
- **Verification.**
  - Full active payload (`build_payload(top=1000)`): 512 active, 512 shown,
    **512 scored, 0 unscored**; `evaluation_states = {"ok": 512}`;
    `recommendations = apply 6 / consider 179 / skip 327 / need_more_info 0`.
  - `need_more_info` with a numeric score = **0**; `need_more_info` with score 0
    = **0**; `legacy_ambiguous` (scored offer with a non-public recommendation)
    = **0**.
  - The only score 0 in the payload is job 592 (`skip`), the genuine
    incompatibility recorded in SR-6 — not a placeholder.
  - The 176 legacy invalid `ok` rows (36 with score 0) still exist in
    `evaluations` as history, but none is the latest evaluation of an active
    offer.
  - `PYTHONPATH=src python -m pytest -q` → **930 passed** in 12.69 s.

## Next step

SR-6 and SR-7 are complete: every one of the 512 active offers has a successful
public evaluation, and no ambiguous evaluation state remains. No further scoring
work is required. Optional housekeeping only: checkpoint the SQLite WAL sidecar
and, when the user authorizes it, commit the SR-1..SR-7 source/test changes and
this task doc.

