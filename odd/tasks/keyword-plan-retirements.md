# keyword-plan exposes per-provider retirements

## Objective

`keyword-plan --json` tells its consumers which providers have retired each planned query, so
the Hermes sweep stops launching searches that the collector is certain to refuse.

## Problem and why

Retirements are recorded per `(provider, query)` in `keywords.RETIRED_SEARCHES` and enforced
by each collector (`collectors/tecnoempleo.py`, `collectors/linkedin.py`), but each item in
`keyword-plan --json`'s `planned` list only carries `family/label/group/query/command`. The
sweep (`~/.hermes/scripts/jobscout_sweep.py`, `approved_queries`) therefore runs every query
against both Tecnoempleo and LinkedIn. Tecnoempleo rejects the retired pairs with `invalid
Tecnoempleo options: ... is retired ...` and exits 1, and the digest reports them as collector
failures: six sweeps in a row, 4 dead runs in the 2026-10-13 sweep alone (see `NEXT_STEPS.md`,
2026-10-16 findings, item 1).

## Scope and constraints

- Each `planned` entry gains `retired_for`: a sorted list of the provider names (as written in
  the registry, e.g. `"tecnoempleo"`) for which `retired_search(provider, query)` returns a
  record. Always present; `[]` when nothing is retired. Derived from the existing registry
  and matcher only: no new registry, no change to `retired_search`, to normalization, or to
  the collectors' refusal.
- The text output marks a retired query on its `- <query>` line, e.g.
  `- Platform Engineer (retired for: tecnoempleo)`. Unretired lines are unchanged.
- Top-level payload keys, `families`, `command`, ordering and counts stay as they are; the
  plan still lists retired queries (other providers may still run them).
- Offline, no new dependency, no network in tests.
- Sweep script change (outside the repo) is done by the orchestrator after the repo change,
  and shown to the owner before it is applied: skip `provider:query` when `provider` is in
  `retired_for`, and write one log line per skipped pair. GetManfred's inventory keeps the
  full query list (it has no retirements).

## Acceptance criteria

- `keyword-plan --json`: `Platform Engineer` has `retired_for == ["tecnoempleo"]`; a query
  with no retirement has `[]`; every entry has the key.
- `keyword-plan` text output shows the marker for a retired query and none for an unretired one.
- Existing `tests/test_keywords.py` tests still pass unchanged; full offline suite green.
- `README.md` and `README.en.md` document the `retired_for` field next to `keyword-plan --json`.

## Tasks

- [x] KR-1 RED/GREEN tests for `retired_for` in `planned_queries` / `keyword-plan --json` and
  for the text marker.
- [x] KR-2 Implement in `keywords.planned_queries` and `cli.cmd_keyword_plan`.
- [x] KR-3 Bilingual README note; full suite.
- [ ] KR-4 Sweep script: skip retired provider+query pairs (owner reviews the diff first).

## Progress, verification, and next step

- 2026-10-20: card written; KR-1..KR-3 implemented (delegated, reviewed). Full suite 1116
  passed. `keyword-plan` marks exactly the four Tecnoempleo pairs the 2026-10-13 sweep
  wasted (Platform Engineer, Junior Cloud Security Engineer, Azure Security Engineer,
  Security Infrastructure Engineer).
- Next: KR-4, sweep script change, pending owner review of the diff.
