# Scoring policy v5

## Objective
Align OpenCode/MiMo recommendations with the user's willingness to apply to
junior and stretch roles while still penalizing genuinely senior or leadership
roles and materially incompatible technology stacks.

## Scope
- Version the OpenCode prompt with an explicit seniority policy.
- Treat 5+ years, leadership, and explicit senior titles as strong penalties.
- Treat one or two unfamiliar technologies as learnable gaps, not automatic
  blockers.
- Treat an unfamiliar core stack as a material mismatch.
- Document the repeatable search, scoring, feedback, calibration, and keyword
  analysis workflow in both READMEs.

## Constraints
- Do not change stored historical evaluations.
- Do not run a new live scoring sweep in this change.
- Preserve the existing local profile selection (`profile.yaml`).
- Keep artifacts in English; user-facing explanation may be Spanish.

## Tasks
- [x] Update and version the OpenCode prompt.
- [x] Add prompt regression tests.
- [x] Document the workflow in README.md and README.en.md.
- [x] Run the offline suite.
- [ ] Create a local commit; do not push. (Deliberately not done: the operator
      explicitly requested no commit and no push for this change.)

## Progress (verified)

- Prompt (`src/jobscout/scoring/opencode.py`): `OPENCODE_PROMPT_VERSION` bumped
  from `opencode-v4-balanced-recommendations` to
  `opencode-v5-seniority-aware`. `OPENCODE_OUTPUT_CONTRACT` now encodes the
  seniority/technology policy: strong penalties (5+ years, `senior`/`staff`/
  `principal`/`lead`/`manager`, architectural ownership), one-or-two missing
  technologies as learnable gaps for Cloud/DevOps/DevSecOps/Platform roles, a
  majority-mismatched core stack as a material mismatch, hard requirements kept
  apart from preferences, and dealbreakers plus SOC/rotating-shift exclusions
  kept in force. The output keys and the untrusted-data framing are unchanged.
- Tests (`tests/test_opencode.py`): 4 regressions added — v5 prompt version is
  distinguishable, the new seniority/technology policy is present, security and
  output rules are not removed, and the evaluation persists the v5 version.
- Docs: `README.md` (Spanish) and `README.en.md` (English) gained a recommended
  operating workflow (profile → controlled sweep → enrich → score with MiMo →
  dashboard → review → export/import feedback → calibrate → keyword analysis),
  the v5 seniority/technology criterion, and the note that the local profile is
  `profile.yaml` selected via `.env` while `profile.example.yaml` is only a
  template. A stale "calibration pending" limitation was corrected.

## Verification

- `python -m pytest tests/test_opencode.py -q -k "prompt or version"` → 7 passed
- `python -m pytest tests/test_llm.py tests/test_llm_schema.py tests/test_db.py -q` → 66 passed
- `python -m pytest -q` → 748 passed (baseline: 744)
- No live scoring sweep, model call, or network request was executed.
