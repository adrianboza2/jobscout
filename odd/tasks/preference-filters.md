# Preference filters

## Objective
Keep the user's explicit exclusions visible and separate from professional-fit
scoring: reject rotating-shift work and operational SOC roles, while leaving
non-operational uses of the term SOC eligible.

## Scope
- Add preference outcomes `PASS`, `FAIL`, and `FLAG`.
- Detect direct and semantically similar rotating-shift language.
- Detect SOC only when the offer describes monitoring, operations, or shift-based
  work; a generic mention of SOC must not fail the offer.
- Preserve the model score and historical evaluation; expose evidence and reason.
- Keep ambiguous matches as `FLAG`, not automatic rejection.

## Constraints
- Local/offline behavior; no remote calls from the filter.
- No automatic applications or deletion of jobs.
- Existing scoring behavior remains unchanged.

## Tasks
- [x] Implement preference detection and evidence model.
- [x] Add tests for positive, negative, and ambiguous SOC/shift cases.
- [x] Surface the result in the dashboard/CLI without changing the score.
- [x] Update documentation and verify the full offline suite.
- [ ] Create a local commit; do not push. — deferred: this run was explicitly
  instructed not to commit, so the work is left uncommitted and reviewable.

## Progress

Verified evidence (run on Windows, Python 3.14):

- **Detection model.** `src/jobscout/preference.py` adds `PreferenceOutcome`
  (`pass`/`fail`/`flag`), `PreferenceMatch`, `PreferenceAssessment`,
  `assess_text`, and `assess_preference`. It is deterministic and offline: pure
  regex/string rules over the already-captured title/description/employment
  fields. No network call, no DB write, and no additional LLM call. A semantic
  "AI analyzes similar phrasings" layer was deliberately **not** wired in: it
  would add one model call per offer (fan-out). If added later it must reuse the
  stored text and stay explicitly opt-in.
- **Rotating shifts.** Clear Spanish/English rotation language fails
  (`turnos rotativos`, `rotación de turnos`, `rotating shifts`, `shift rotation`,
  `24/7 shifts`). Generic `turnos`/`shift work` and a bare `24/7` are `flag`.
  `horario flexible` and `guardias puntuales`/`occasional on-call` are `pass`, so
  they never produce a false `fail`.
- **SOC.** Only operational SOC fails: monitoring/monitorización, operations,
  24/7, shifts, on-call/guardias, continuous surveillance, incident/threat
  monitoring, within a bounded context window of the mention. Generic mentions,
  knowledge requirements, and `SOC 2` compliance stay `pass`; a SOC role name
  without operational evidence is `flag`.
- **Score untouched.** Preference is recomputed when the offer is read and is
  never persisted as a second judgment; the final score, recommendation, and
  stored evaluation are unchanged, and no history is deleted. The dashboard
  reads the description for detection but never serializes it.
- **Surfacing.** Dashboard payload gains a per-offer `preference` object and a
  `summary.preferences` count; the card shows a neutral (hue-free) band for
  `fail`/`flag` with reason and evidence. CLI `list`/`rank` add a
  `[preference=...]` marker and a `preference` object in `--json`; `score` prints
  the verdict beside the evaluation. No general advisory signals or health
  checks were added.
- **Tests.** `tests/test_preference.py` covers Spanish/English rotating shifts
  and variants, flexible/occasional no-false-fail, generic/knowledge/role/
  operational SOC, PASS/FAIL/FLAG shape, evidence/reason, score intactness,
  dashboard and CLI integration, and an explicit no-network guard.
- **Commands.** `python -m pytest tests/test_preference.py -q` → 49 passed.
  `python -m pytest -q` → 744 passed.

