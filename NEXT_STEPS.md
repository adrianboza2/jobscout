# Next Steps

This document records the next improvements for `jobscout` after the first
working MVP.

## Priority 1 — Turn feedback into durable data

- Import the dashboard's exported `jobscout.feedback.v1` JSON into SQLite.
- Store the human decision separately from the model recommendation.
- Store an optional reason and timestamp.
- Keep the import idempotent and validate the export schema before writing.

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

- Detect expired, removed, redirected, or duplicate LinkedIn postings.
- Mark stale offers without deleting their historical evaluation.
- Refresh only active/relevant offers.
- Show broken-link status in the dashboard.

## Priority 4 — Calibrate scoring

Deliberately **not implemented** in the keyword-coverage work: this is the
model-vs-human comparison/calibration feature, and it was explicitly skipped. The
keyword dashboard only reports descriptive per-keyword rates of the imported human
decisions; it never computes agreement, false positives, or false negatives.

- Compare model recommendations with imported human decisions.
- Identify false positives and false negatives.
- Adjust role-fit, seniority, freshness, and dealbreaker handling.
- Preserve evaluation provenance when prompts or profile versions change.

## Priority 5 — Improve daily workflow

- Add optional scheduling with explicit user control.
- Add local or opt-in notifications for high-value offers.
- Add a dashboard view for new and unreviewed offers.
- Keep all automation read-only with respect to applications.

## Priority 6 — Expand sources

Add other job platforms through isolated source adapters. Each adapter should
share the same normalized job contract, deduplication rules, provenance model,
rate limits, and fail-closed behavior.

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
- Keep the private GitHub repository synchronized only after reviewing commits.
