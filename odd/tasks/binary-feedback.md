# Binary human feedback

## Objective
Make the dashboard's active human decision a binary choice: apply or discard,
while keeping historical `consider` rows importable, visible and countable.

## Scope
- Render only `Aplicar` and `Descartar` buttons.
- Keep legacy `consider` rows importable and visible as historical data, but do
  not offer `Considerar` for new decisions.
- Preserve export schema compatibility and calibration of legacy feedback.
- Document the binary workflow.

## Constraints
- Do not silently rewrite historical feedback.
- Keep model recommendations separate from human decisions.
- No automatic application submission.

## Tasks
- [x] Update dashboard controls and labels.
- [x] Preserve legacy import/calibration compatibility.
- [x] Add tests for the binary UI, the export shape, and legacy preservation.
- [x] Update README ES/EN and this task file.
- [ ] Create a local commit; do not push. (Deferred: the operator requested no
  commit or push in this run.)

## Status (verified 2026-09-18)

Offline suite: `python -m pytest -q` -> **867 passed** (was 860 before this work).

Focused suites:

- `tests/test_dashboard.py`, `tests/test_feedback.py`,
  `tests/test_calibration.py` -> **154 passed**.

Implemented and verified:

- **Binary active decision.** `models.ACTIVE_FEEDBACK_DECISIONS` is the single
  canonical vocabulary (`apply`, `reject`); `feedback.ACTIVE_DECISIONS` and
  `dashboard.DASHBOARD_FEEDBACK_DECISIONS` re-export it. The report renders only
  the two buttons (`FEEDBACK_DECISIONS = ["apply", "reject"]`) with labels
  `Aplicar` / `Descartar`.
- **Legacy `consider` is display-only.** A stored `consider` is shown as
  `Decisión histórica (legacy): Considerar (histórico). Puede reemplazarla con
  Aplicar o Descartar.` The legacy marker uses shape/weight, never the model
  accent or the ember. Clicking an active button replaces the legacy value; the
  old value is never converted silently.
- **Importer compatibility.** `feedback.DECISIONS` still contains `consider`, so a
  pre-binary export imports verbatim; `feedback.LEGACY_DECISIONS == ("consider",)`
  names the historical value explicitly.
- **Export schema unchanged.** `jobscout.feedback.v1` and the `feedback` object
  are shipped as-is, so a legacy entry is exported without a rewrite. New marks
  can only be `apply` or `reject`.
- **Calibration counts legacy.** `calibration.LEGACY_HUMAN_DECISIONS` flags
  `consider`; `CalibrationReport.legacy_total`, `"legacy": true` per sample, and
  the JSON/text output keep counting it instead of dropping it.
- **Model axis untouched.** The model recommendation keeps its own
  `apply`/`consider`/`skip`/`need_more_info` vocabulary, including the
  *Considerar* filter option; only the human feedback axis changed.

## GetManfred keyword catalog (documented, not changed)

The GetManfred adapter applies `--keywords` **locally** with exact token
matching: every token of an approved term must appear as a whole token in the
normalized title/slug. There are no synonyms, aliases or family variants, so
`DevOps` does not match `DevSecOps` and `Cloud` does not match `Cloud Security`.

Candidate variants observed in the public sitemap: **DevOps, DevSecOps, Cloud
Engineer, Cloud Security, Azure**, and possibly **SRE**. Adding them to the
approved catalog is a **separate decision** (a human catalog review), not
automatic behavior of this adapter, so no catalog change is included here.
