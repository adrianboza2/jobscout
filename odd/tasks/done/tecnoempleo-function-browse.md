# Tecnoempleo function browse

## Objective
Replace the per-keyword Tecnoempleo text searches with one browse by professional
function (Madrid), keep one text query ("prácticas"), and publish the plan through
`keyword-plan --json` so the Hermes sweep reads it instead of hard-coding it.
Source: NEXT_STEPS.md, 2026-10-23 "Next" item 1, scheduled as item 2 on 2026-10-27.

## Owner decisions (2026-10-27)
1. New mode `search-tecnoempleo --browse`, mutually exclusive with `--keywords` and
   with `--location` (`pr=,263,` is already Madrid). Robots handling, pacing,
   retries and fail-closed behaviour unchanged.
2. Provenance label (stored as the run query, shown as a dashboard series):
   `función: sistemas, devops y seguridad (Madrid)`.
3. The 7 Tecnoempleo text searches still active are retired for `tecnoempleo`
   through `RETIRED_SEARCHES`, reason "replaced by the Tecnoempleo function browse",
   date 2026-10-27: `Junior Cloud Engineer`, `Junior DevOps Engineer`,
   `Cloud Security Engineer`, `DevSecOps Engineer`, `Junior DevSecOps Engineer`,
   `Azure Cloud Security`, `Azure DevSecOps`. LinkedIn keeps them. The text query
   `prácticas` stays for Tecnoempleo only (internship programmes filed under other
   functions). This does not break the CP-8 rule "never retire a query that costs a
   good offer": the browse found 12 of the 13 known-good offers and the 13th is the
   "prácticas" case.
4. Pages: sweep 5 pages (30 offers/page → `--max-items 150`). First backfill by hand:
   `--max-pages 10 --max-items 250`; the hard caps (`HARD_MAX_PAGES = 10`,
   `HARD_MAX_ITEMS = 250`, `config.py:99-100`) are **not** raised.

## Evidence
Live pages captured 2026-10-23 in `captures/tecnoempleo-2026-10-23/pages/`
(gitignored, local only; `index.json` maps URL → file). Pushed through
`postings_from_html(..., max_items=250)`: pages 1, 2, 5 and 10 of the browse give 30
cards each, page 11 gives 29, and a later page answers 404. Tests must use synthetic
fixtures (`tests/fixtures/tecnoempleo_search_*.html` can be reused as card pages:
the browse result markup is the same search-results markup).

## Specification

### URL (seam: `build_search_url` / config in `src/jobscout/collectors/tecnoempleo.py`)
- Browse page 1: exactly
  `https://www.tecnoempleo.com/ofertas-trabajo/?pr=,263,&es=,34,43,40,39,8,45,19,`
- Page N > 1: the same plus `&pagina=N`, e.g. page 2
  `https://www.tecnoempleo.com/ofertas-trabajo/?pr=,263,&es=,34,43,40,39,8,45,19,&pagina=2`.
  Commas stay literal (no `%2C`): this is the URL that was verified live.
- Text mode (`--keywords`) unchanged, byte for byte.
- Constants with a short why-comment naming the functions (`es`: DevOps, Técnico
  de Sistemas, Ciberseguridad, Redes, Soporte Técnico, Administrador, Helpdesk;
  `pr=263`: Madrid): browse URL and the label of decision 2.
- `TecnoempleoSearchConfig` gains a browse flag; in browse mode `keywords` is the
  label, so `to_run_params`/`search_runs.query` carry the label and
  `params["planned_urls"]` carries the real URLs. Page caps and validation are the
  same as text mode.

### CLI
- `search-tecnoempleo --browse [--max-pages N] [--max-items N] [--cycle-id ...]
  [--label ...] [--confirm-network | --dry-run]`.
- `--browse` together with `--keywords` or `--location` → argparse/usage error,
  non-zero exit, no network.
- `--dry-run --browse --max-pages 2` lists the two URLs above.

### Keyword plan
- `RETIRED_SEARCHES` gains the 7 entries of decision 3 (14 in total).
- `keyword-plan --json` gains a top-level key (existing keys unchanged):

      "tecnoempleo": {
        "browse": {
          "label": "función: sistemas, devops y seguridad (Madrid)",
          "url": "<browse page 1 URL>",
          "max_pages": 5,
          "max_items": 150
        },
        "text_queries": ["prácticas"]
      }

  The text output of `keyword-plan` prints the same in one or two lines.
- With this change every catalog query shows `tecnoempleo` in `retired_for`, so the
  current sweep logic stops launching per-keyword Tecnoempleo searches by itself.

### Not in this task
- `~/.hermes/scripts/jobscout_sweep.py` (outside the repo): changed once by the
  orchestrator after merging, to run the browse and the `prácticas` query from the
  JSON block.
- No live network call. The first real run is proposed to the owner afterwards.

## Constraints
- Offline tests only (some block `socket.socket`); synthetic fixtures.
- Do not touch LinkedIn or GetManfred, robots handling, pacing, caps or retries.
- Style per AGENTS.md; README.md and README.en.md updated together where they
  describe Tecnoempleo searches, the keyword plan or retired searches.

## Tasks
- [x] Browse URL + config + collector run (pagination, 404 end, provenance label).
- [x] CLI `--browse` with exclusivity errors and dry run.
- [x] `RETIRED_SEARCHES` + `keyword-plan` JSON/text block; update registry tests.
- [x] READMEs (ES + EN).
- [x] Full offline suite green.

## Success command
    .venv/bin/python -m pytest -q tests/test_tecnoempleo_collector.py tests/test_keywords.py tests/test_cli.py tests/test_dashboard.py
    .venv/bin/python -m pytest -q        # acceptance: full suite, 0 failed

## Progress
- Browse URL/config/collector: literal-comma page URL, browse flag, label provenance, pagination/404 covered.
- CLI: `--browse` added with exclusivity errors and dry-run URL listing.
- Registry/keyword-plan: 7 new Tecnoempleo retirements (14 total) and the `tecnoempleo` JSON/text block.
- READMEs: ES and EN updated with matching content.
- 2026-10-27 (orchestrator review): fixed a missing `EXIT_OK` import in the new
  collector test and the stale `retired_for` expectation in `test_keywords.py`
  (every catalog query is now retired for Tecnoempleo). Focused suites green;
  `.venv/bin/python -m pytest -q` → **1198 passed**; `git diff --check` clean;
  `search-tecnoempleo --browse --max-pages 2 --dry-run` prints the two exact URLs.
