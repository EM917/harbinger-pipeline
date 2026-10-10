# Harbinger: EDGAR 8-K triage pipeline

A weekly pipeline that scans SEC EDGAR for material-agreement 8-Ks, rejects
the noise with deterministic rules, and routes whatever survives to human
review. This repo is the public subset of a larger private research project.
The investment write-ups built on top of it are not published here.

This is not a trading system and nothing here is investment advice. It
works only on public data (EDGAR filings and public market-cap data).

## Design rules

- **No LLM in the rejection path.** The rule engine is ordered substring
  rules plus a market-cap band. Every reject is reproducible from
  `RULES.md`.
- **Miss loudly, never silently.** Anything the engine can't parse is routed
  to human review instead of being dropped.
- **No fabricated data.** Downstream analysis has to cite sources verbatim.
  `scripts/verify_claims.py` greps every registered claim against the source
  corpus, and a claim it can't find blocks the report.

## The funnel

`scripts/run_weekly_scan.py` runs once a week:

1. Pulls Item 1.01 8-Ks from EDGAR full-text search and filters by SIC code
   (`config/sic_filters.yaml`).
2. Fetches each filing's full body. The 300-character search excerpt hides
   what a filing is actually about. That was an early bug.
3. Reads EX-99 press releases by the filing index's Type column, not by
   filename patterns. A filename-based matcher missed a real partnership
   press release because of how its exhibit file was named.
4. Hands the text to `scripts/rule_engine.py`, which rejects known
   non-partnership classes (debt deals, financings, M&A, SPAC noise) and
   applies a market-cap band.
5. Logs every hit and every triage decision to SQLite
   (`data/funnel_schema.sql`).

Over the first twelve weekly scans: 260 filings triaged, 68 surfaced for
review, and one genuine partnership 8-K routed to review correctly. That was
the first end-to-end validation.

## Layout

```
scripts/
  run_weekly_scan.py   weekly runner
  edgar_monitor.py     EDGAR fetch + parsing
  rule_engine.py       deterministic reject rules
  fetch_market_cap.py  market-cap lookup for the cap band
  init_funnel_log.py   creates the SQLite triage log
  verify_claims.py     verbatim-source verification gate
config/sic_filters.yaml
data/funnel_schema.sql
data/funnel_log.db     the real triage log from the first twelve scans
RULES.md               every rule, with the reason it exists
tests/                 parser, rule-engine regression, and runner tests
tests/fixtures/        18 committed 8-K bodies the regression tests run on
```

## Running it

```
pip install -r requirements.txt
pytest tests/
python scripts/run_weekly_scan.py --help
```

The tests run offline against the committed fixtures and triage log.
`scripts/init_funnel_log.py` creates a fresh log from the schema; it refuses
to overwrite an existing database, so pass `--db-path` to start a new one
alongside the committed `data/funnel_log.db`.

SEC requires a descriptive User-Agent with contact details on EDGAR
requests. Set yours in `scripts/edgar_monitor.py` before running.
