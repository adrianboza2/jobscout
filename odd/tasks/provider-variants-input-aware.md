# Provider variants and input-aware scoring

## Objective
Apply the corrected P2 prompt from `cc2c690` on `rebuild`: score only changed
inputs and fetch details by pending provider variant.

## Why
Avoid repeating unchanged scoring work and fetching details for variants that
are already complete, without importing the excluded robots plumbing.

## Scope and constraints
- Restore exactly `src/jobscout/db.py`, `src/jobscout/pipeline.py`,
  `tests/test_job_variants.py`, and `tests/test_db.py` from `cc2c690`.
- Do not restore or modify `tests/test_provenance.py`; the corrected prompt
  explains its only source delta requires excluded `robots_text` plumbing.
- Do not change `config.py`, `url_policy.py`, `collectors/*`, or `cli.py`, and do
  not introduce or alter robots behavior. Only the exact CLI missing-argument
  adjustment is permitted if that exact failure occurs.
- TDD is enabled from project testing-capabilities configuration in Engram
  (`sdd/jobscout/testing-capabilities`, observation #1). Runner:
  `PATH="$PWD/.venv/bin:$PATH" python -m pytest -q`.
- The user confirmed execution of this corrected prompt, including its listed
  public smoke command to Tecnoempleo and LinkedIn; no credentials are used.
- Commit and annotated tag are conditional on all verification criteria passing.

## Acceptance criteria and checks
- [x] Restore the two specified test files first and observe RED before copying
      implementation.
- [x] The four specified files match `cc2c690`; no excluded paths changed.
- [x] Full offline suite passes with at least 1000 tests.
- [x] Prompt's application-backed schema check prints `['job_variants'] 6`:
      `.venv/bin/python -m jobscout rank --top 1 >/dev/null 2>&1; .venv/bin/python -c "import sqlite3;c=sqlite3.connect('data/jobscout.sqlite3');print([r[0] for r in c.execute(\"select name from sqlite_master where name like 'job_variant%'\")], c.execute('pragma user_version').fetchone()[0])"`
- [x] Authorized smoke exits successfully and reports `veredicto: OK`.
- [x] Create the requested conventional commit and annotated `p2-ok` tag only
      after all checks pass.

## Tasks
- [x] P2-R1 — Restore `tests/test_job_variants.py` and `tests/test_db.py` first;
      run focused tests and record RED.
- [x] P2-R2 — Restore whole `db.py` and `pipeline.py` from `cc2c690`; stop on any
      out-of-scope or specified CLI failure.
- [x] P2-R3 — Run full offline suite and the application-backed DB migration
      check; run the explicitly authorized public smoke.
- [x] P2-R4 — If all checks pass, create the requested commit and annotated tag.

## Previous attempt
- The earlier prompt included `tests/test_provenance.py`; with that file from
  `cc2c690`, the suite failed twice because `search_linkedin()` rejected the
  out-of-scope `robots_text` keyword. The earlier attempt followed its rollback
  instruction and returned source/tests to P1. The corrected prompt explicitly
  excludes this file and corrects the migration check to initialize the schema
  through the application.

## Progress
- Re-read the corrected prompt and confirmed the branch is clean at P1
  (`53d85ee`). Restored this task document from its Engram mirror because the
  local file from the previous attempt was absent.
- P2-R1 (RED): restored only `tests/test_job_variants.py` and `tests/test_db.py`
  from `cc2c690`. Focused run literal result:
  `tests/test_job_variants.py` collection error
  `ImportError: cannot import name 'active_variants' from 'jobscout.db'`;
  `tests/test_db.py` `5 failed, 27 passed` including
  `TypeError: jobs_needing_evaluation() got an unexpected keyword argument
  'fingerprint_for'` and version/migration-column assertion failures. No CLI
  signature mismatch observed.
- P2-R2: restored whole `src/jobscout/db.py` and `src/jobscout/pipeline.py` from
  `cc2c690`. `git diff cc2c690 --` over the four authorized paths is empty.
- P2-R3: full offline suite
  `PATH="$PWD/.venv/bin:$PATH" python -m pytest -q` -> `1051 passed in 21.12s`,
  no CLI mismatch, so `cli.py` was not touched.
- P2-R3 smoke:
  `JOBSCOUT_REPO=$PWD .venv/bin/python ~/.hermes/scripts/jobscout_sweep.py --smoke`
  -> tecnoempleo OK (2 items), linkedin OK (2 items), `veredicto: OK`, exit 0.
- P2-R3 DB check:
  `.venv/bin/python -m jobscout rank --top 1` then the prompted sqlite query
  -> literal output `['job_variants'] 6`.
- P2-R4: creating commit
  `feat(db): provider variants with input-aware scoring selection` and annotated
  tag `p2-ok` (`P2 verificado`).
