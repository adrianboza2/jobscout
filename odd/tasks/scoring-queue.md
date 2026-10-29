# Scoring queue

## Objective
`score --all` stops paying for card-only scores, puts never-scored offers ahead of
re-scores, and stops a run when the provider is clearly down. Source: NEXT_STEPS.md,
2026-10-28 "Next" item 1 a–c (board: SC-01 spec, SC-02/03/04 implementation). Item 1 d
(Hermes sweep script) is SC-05 and is changed once, after merging.

## Owner decisions (2026-10-28)
- No scoring without details; exception after 2 failed detail attempts that are not a
  404/410; `--job-id` always forces.
- Two groups: never scored first, then re-scores; each group keeps
  `last_seen_at DESC, id DESC`. Ordering by heuristic score is dropped.
- Stop after 3 consecutive provider errors and report how far the run got.

## Facts the design rests on (measured 2026-10-29, read-only)
- Selection: `jobs_needing_evaluation` (`db.py:1302-1363`) loads every active offer in
  `last_seen_at DESC, id DESC` and keeps those whose current input fingerprint has no
  `ok` evaluation. Seniority filter and `--limit` are applied after it
  (`cli.py:1611-1620`). `--rescore` uses `all_active_jobs`, `ORDER BY id`.
- Per-offer loop (`cli.py:1630-1694`) never stops; every attempt writes an evaluation
  row, errors included. Exit code is 1 only when nothing succeeded.
- Details predicate: `jobs_needing_details` (`db.py:1383`):
  `content_status != 'ok' OR description IS NULL OR TRIM(description) = ''`.
- Attempts: one row per detail attempt in `job_detail_fetches` (`status`,
  `http_status`, ...). A 404/410 deactivates the offer. A 403/429/timeout aborts the
  run: the offer being fetched gets a `failed` row with `http_status` NULL; the rest
  of the batch gets `skipped` rows (not attempted).
- Active offers waiting for details today: LinkedIn 103, Tecnoempleo 135, GetManfred 0.
  Of those 238, 237 have no failed attempt and 1 has one. The exception is a safety
  net; it changes nothing today.
- The sweep (`jobscout_sweep.py:505-515`) records a non-zero `score` exit as a failure
  and still builds the dashboard and the backup.

## Rule 1 — no scoring without details (SC-02)
One SQL helper in `db.py`, used by `score` and by the dashboard so both agree:

`jobs_awaiting_details(conn) -> set[int]`: active offers that match the
`jobs_needing_details` predicate **and** have fewer than
`DETAIL_ATTEMPTS_BEFORE_SCORING = 2` failed attempts, where a failed attempt is a
`job_detail_fetches` row with `status IN ('failed', 'partial')` and
`http_status` not 404/410 (NULL counts). `skipped` rows do not count: the offer was
never requested.

- `score --all` (with or without `--rescore`) drops those offers before `--limit`,
  like the seniority filter. Text output adds
  `skipped N offer(s) waiting for details`; JSON adds `skipped_pending_details`.
- `--job-id` ignores the rule (as it already ignores seniority).
- An offer that already has a card-only score keeps it and is not re-scored on the card
  when its fingerprint goes stale; the next score after its details arrive replaces it.
- Consequence to accept: when LinkedIn answers 403 at the start of a detail batch, the
  first offer of that batch collects the failed row; after two such runs it is scored on
  its card. That is the "never stays in limbo" exception working as intended.

## Rule 2 — never scored first (SC-03)
In `jobs_needing_evaluation` (input-aware path) split the selected offers, keeping the
existing order inside each group:

1. offers with **no `ok` evaluation from this scorer at all** (error-only rows count as
   never scored);
2. offers whose `ok` evaluations all carry another fingerprint (re-scores).

`--limit` cuts after the concatenation, so a sweep's new offers are never pushed behind
re-scores after a model, prompt or profile change. `--rescore` keeps its own order
(everything is being re-scored anyway). The legacy path without `fingerprint_for` already
returns only never-scored offers and is not touched.

## Rule 3 — stop after 3 consecutive provider errors (SC-04)
- Counter over the **final** status of each offer (after the fallback retry, if a
  fallback model is configured). `status = 'error'` adds one; any other status
  (`ok`, `parse_error`, ...) resets it. `parse_error` is the model's output, not the
  provider, so it never stops a run.
- At 3 the loop stops. The 3 error rows stay written (they were real attempts); the
  remaining offers get no row and are picked up by the next run.
- Text output: `stopped after 3 consecutive provider errors: A of S offer(s) attempted,
  R left for the next run`. JSON adds `stopped_early` (bool) and `not_attempted` (int).
- Exit code 1 when stopped early, even if some offers succeeded, so the sweep log shows
  it as a `score` failure. The dashboard and backup still run (sweep facts above).

## Dashboard (SC-02)
- Payload: per offer `pending_details: bool`, from `jobs_awaiting_details`.
- Client: an offer with no `ok` score and `pending_details` gets the state
  "Pendiente de detalles" instead of "sin puntuar"; it is listed at the end with the
  other unscored offers, and the existing "N oferta(s) ... al final" notice counts it
  separately. Offers with a card-only score are shown as today.
- The HTML stays self-contained and works over `file://`.

## Constraints
- No schema change, no migration: everything is computed from existing tables.
- No network in tests; synthetic fixtures only.
- Seniority filter, `--job-id` forcing, fallback stickiness and the fallback skip of
  already-scored offers keep their current behaviour and tests.
- README ES/EN: document the three rules in the `score --all` notes of the operating
  workflow and the CLI reference. Fix in the same place the stale paragraph that still
  says re-evaluation ignores input changes (README.md:142-147, README.en.md:143-147).

## Tasks
- [x] SC-02: `jobs_awaiting_details` + `score` gate + output fields + dashboard state.
  Tests: offer without description skipped; with 2 failed (non-404) attempts scored;
  `skipped` and 404 rows do not count; `--job-id` forces; gate applied before `--limit`;
  `--rescore` also gated; dashboard payload flag.
- [ ] SC-03: two-group ordering. Tests: never-scored offer seen earlier goes ahead of a
  stale offer seen later; error-only offer counts as never scored; `--limit` keeps the
  first group whole.
- [ ] SC-04: consecutive-error stop. Tests: 3 errors in a row stop the run with exit 1
  and the right counts; error, ok, error, error does not stop; parse_error resets;
  with a fallback, a primary error rescued by the fallback does not count.
- [ ] README ES/EN.

## Success command
    .venv/bin/python -m pytest -q

## Progress
- 2026-10-29: spec written (SC-01), waiting for owner approval.
- 2026-10-29: SC-02 implemented (gate, output fields, dashboard state, README ES/EN); suite 1211 passed.
