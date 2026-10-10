# Triage Rule Engine — Predicate & Priority Reference (Phase 1A Tier 6.2)

Authoritative documentation for `scripts/rule_engine.py`, the deterministic 8-K
triage classifier. The engine replaced the hardcoded `ticker -> rejection_label`
dict in `scripts/migrate_week1_baseline.py` (now retired to a golden fixture /
reference). Regression contract: `tests/test_rule_engine_regression.py` (R1: the
engine reproduces all 18 committed Week-1 golden labels; R5: cap-band is reachable
on all three branches).

## What the engine does (and does NOT do)

- **Does:** reject hits that match a known non-partnership **content marker**
  (financing / debt / M&A / SPAC / IP), then apply a **cap-band** filter to the
  survivors. Output: `(is_candidate, rejection_label, cap_resolved)`.
- **Does NOT:** positively detect partnerships. Survivors are surfaced for human
  §0–§8 review — the human is the judgment layer. Per Phase 5 (permanently
  vetoed), there is **no LLM judgment anywhere in the rejection path**.

## Determinism

The same `(body_text, market_cap_millions)` always yields the same output. No
randomness, no wall-clock, no network inside the engine. Market cap is fetched
separately (`scripts/fetch_market_cap.py`) and passed IN, keeping the engine pure.

## Canonical classifier input

The engine classifies from the **full cleaned 8-K body**, persisted as committed
fixtures under `tests/fixtures/week1_bodies/{accession}.txt` (built by
`scripts/build_week1_fixtures.py`). The 300-char digest `body_excerpt` (a
Component 3 *display* truncation) is **too short** — it hid BNZI's debt substance
(loan agreement + senior convertible note) past the cut and caused the Tier 2.6
`ma_marker` misclassification (see `EDGAR_MONITOR_CALIBRATION.md` → "Week 1 BNZI
reclassification"). Lesson: classify from the full body, not the display excerpt.

---

## Stage 1 — content markers (ordered; first match wins)

Each marker matches when `any(p in body for p in any_of)` **and**
(`and_any_of` empty **or** `any(p in body for p in and_any_of)`). All phrases are
lowercase substrings; the engine lowercases the body once.

Evaluation order is **load-bearing** (see "Priority order" below).

| # | Marker | Predicate (any_of) | Extra (and_any_of) | Rationale | Evidence accession(s) |
|---|---|---|---|---|---|
| 1 | `spac_marker` | `sponsor` | one of `earnout` / `business combination` / `forfeiture` / `de-spac` | SPAC sponsor share settlement / earnout / forfeiture | NVTS `0001104659-26-065731` |
| 2 | `ip_acquisition_marker` | `intellectual property purchase agreement` | — | IP / asset purchase (not a commercial partnership) | RMXI `0001213900-26-058408` |
| 3 | `ma_marker` | `sale and purchase agreement` / `agreement and plan of merger` / `asset purchase agreement` / `merger agreement` / `membership interest purchase` | — | M&A / divestiture | SKIL `0001437749-26-017991` |
| 4 | `debt_marker` | `senior notes` / `exchangeable senior notes` / `convertible senior notes` / `note purchase agreement` / `notes offering` / `credit agreement` / `credit increase` / `loan agreement` / `indenture` / `delayed draw` | — | debt: notes / credit facility / loan / indenture | ATHR, BDX, BNZI, CRWV, RDNW, SABR, SITM |
| 5 | `financing_marker` | `underwriting agreement` / `securities purchase agreement` / `private placement` / `at-the-market` / `at the market` / `sales agreement` / `stock purchase agreement` / `registered direct` / `placement agent` / `pre-funded warrant` | — | equity / dilutive financing | CODX, EXYN, ICHR, IPWR, MOBX, NXL, QTI, RPID |

(Full evidence accession list for debt/financing is inline in `rule_engine.py`.)

### Excluded phrases (deliberate — prevent Week-1 false positives)

Full-body matching means boilerplate/exhibit text can leak a broad phrase into a
hit of a different class. These phrases are **deliberately NOT** in the predicate
sets:

- **bare `term loan`** — ICHR (an ATM *financing*) mentions "term loan"
  incidentally in capitalization boilerplate. Excluded so ICHR is not mis-rejected
  as debt. CRWV (genuine debt) still matches via `credit agreement` / `delayed draw`.
- **bare `convertible note` / `convertible promissory note` / `senior convertible
  note`** — MOBX (a *financing*: "Securities Purchase Agreement and Convertible
  Note") carries a convertible note. Excluded so MOBX is not mis-rejected as debt.
  BNZI (genuine debt) still matches via `loan agreement`.
- **bare `intellectual property`** — appears in generic reps/warranties of ATHR,
  NXL, SKIL. The IP marker requires the specific `intellectual property purchase
  agreement` (RMXI) instead.

Dropping these broad phrases costs **zero** debt/IP-hit coverage (every debt hit
still matches via a kept phrase) while removing the only Week-1 cross-class leaks.

---

## Priority order (data-constrained)

Order: **spac → ip → ma → debt → financing**.

With **full-body** matching, several Week-1 hits genuinely match more than one
marker, so priority is **data-forced** (this corrects an earlier, 300-char-excerpt
based assessment that found "zero multi-match"):

- **`debt` before `financing`** — forced by BNZI / SABR / SITM, whose bodies carry
  both a debt phrase and a financing phrase but are debt:
  - BNZI: `loan agreement` (debt) + `securities purchase agreement` (the Sept-2024
    debt-to-equity conversion).
  - SABR: `exchangeable senior notes` (debt) + `private placement`.
  - SITM: `convertible senior notes` (debt) + `underwriting agreement`.
- **`ip` before `financing`** — forced by RMXI: `intellectual property purchase
  agreement` (IP) + `private placement`.
- **`spac` and `ma` placed first** — not strictly forced by a Week-1 multi-match,
  but their phrases are distinctive and collide with no other Week-1 hit, so
  front-loading them is safe and principled (most-specific first).

§2-P2 status: **priority IS now data-constrained** by the multi-matches above. The
remaining freedoms (spac/ma vs debt/ip) are unconstrained by Week-1 data; if a
future hit multi-matches across those, revisit this order and record the new
constraint here.

---

## Stage 2 — cap-band filter (survivors only)

Only hits that pass **all** content markers (survivors) reach the cap-band.

- Constant: `CAP_BAND_MAX_MM = 5000` (sub-$5B, strictly less than). Adjustable in
  `rule_engine.py`.
- Source: **yfinance only** (`market_cap / 1e6`). **No moomoo / OpenD / broker
  gateway dependency** at runtime (Tier 6.2 §4). Cached to
  `data/market_cap_cache.json` (gitignored) with a fetch timestamp, written back
  to `raw_hits.market_cap_millions`.
- Branches:
  | market cap | result |
  |---|---|
  | `>= 5000` | `is_candidate=0`, `rejection_label='oversized_marker'`, `cap_resolved=1` |
  | `< 5000` | `is_candidate=1`, `rejection_label=NULL`, `cap_resolved=1` |
  | unavailable (fetch failed) | `is_candidate=1`, `rejection_label=NULL`, `cap_resolved=0` + warning |
- **Bias to human review on failure:** a cap-fetch failure never silently drops a
  potential candidate — it surfaces (`cap_resolved=0`) with a warning.

`oversized_marker` is a **new** label introduced by Tier 6.2 (cap-band stage),
distinct from the 5 content markers.

**Precedence guarantee:** content markers run before cap-band, so for the 18 Week-1
baseline hits (all content-rejected) the backfilled market cap does **not** change
any label — backfill is pure observability and R1 is independent of it. (Several
backfilled caps — BDX, CRWV, SITM, NVTS — exceed $5B, but they are rejected at the
content stage, so cap-band never evaluates them; their `cap_resolved` stays NULL.)

---

## Denylist-leakage note (expected behavior, not a bug)

The 5 content markers are a **rejection denylist** distilled from Week-1. Future
weeks will surface 8-Ks that are **neither** these 5 classes **nor** a partnership
— e.g., officer departures (Item 5.02), guidance updates, routine Item 8.01s. With
no content marker matching, these become **survivors** and flow to human review.
This is **expected**: the human is the judgment layer; the engine only filters the
known-noise classes. Consequence: `weekly_scans.candidate_count` will rise over
time. That is observable by design (the funnel log records per-week candidate
counts) and is **not** fixed in this tier — it is the intended division of labor.
If a recurring survivor class proves to be reliable noise, add a new content marker
(with evidence) in a future calibration tier.

## Known limitations

- **Full-body matching boilerplate-leak risk.** Matching the whole body (rather
  than a tightly-extracted Item 1.01 section) is robust for Week-1 with the
  specific predicates + exclusions above, but a future filing could leak a kept
  phrase from an exhibit/boilerplate into the wrong class. Section-windowed
  matching was rejected for now because `edgar_monitor.extract_item_section`
  terminates BNZI's Item 1.01 early (before its loan substance). Revisit windowing
  if a future leak appears; add the offending phrase to the exclusion list with
  evidence.
- **8-K family variants.** BNZI is `8-K/A`; the engine is filing_type-agnostic
  (classifies on body text), so variants do not break it. If a future hit needs
  variant-specific handling, surface it as a warning rather than guessing.
- **Market cap is current, not as-of-filing-date** (Tier 6.2 §4 explicitly accepts
  current spot). A hit filed when small but now large could be mis-sized; revisit
  only if it causes a real miss.

---

## Tier 6.3 runner integration (`scripts/run_weekly_scan.py`, TASKBOARD B1)

The weekly-scan runner is the engine's production caller. Integration contract:

- **Input**: the FULL cleaned 8-K body (via edgar_monitor `fetch_filing_index` +
  `fetch_document_body`, same path as `build_week1_fixtures.py`), per the
  canonical-classifier-input doctrine above — never the 300-char display
  excerpt. Triage evidence prose records the classified body's char count.
- **Market cap** is passed IN from `fetch_market_cap.get_market_cap_mm`
  (yfinance-only, cached) at classify time; the engine stays pure.
- **Emission vocabulary**: the runner writes `triage_method='rule_engine_v1'`
  rows and emits ONLY the 5 content markers + `oversized_marker` + NULL
  (candidate). The 4 schema labels never emitted by any code path are
  reconciled in `data/funnel_schema.sql` comments: `unparseable` /
  `missing_market_cap` / `wrong_sic` DEPRECATED (superseded by
  bias-to-human-review, `cap_resolved=0`, and pre-storage SIC filtering
  respectively); `ambiguous_manual_review` reserved for the human
  manual-override tier.
- **Body-fetch failure path** extends bias-to-human-review: the hit is inserted
  with `fetch_status='failed'` + `fetch_error`, and triaged as a candidate
  (`is_candidate=1`, `cap_resolved=0`) WITHOUT content-marker or cap-band
  evaluation — content markers precede cap-band, so an unread body must not be
  cap-rejected either. Never a silent drop.
- **Metadata-unparseable persistence (TASKBOARD B6)**: a SIC-matched hit whose
  FTS metadata cannot be parsed into a (ticker, CIK) pair cannot enter
  `raw_hits` (ticker/cik NOT NULL). Pre-B6 it was SILENTLY DROPPED — only a
  `weekly_scans.notes` count survived and the accession was unrecoverable
  (recurred 3x in the B2 backfill: W23/W24/W25 each dropped 1). It is now
  persisted to the additive `meta_unparseable_hits` side table via
  `run_weekly_scan.record_unparseable_hit` (accession + full FTS `_source`
  recoverable; idempotent by accession, INSERT OR IGNORE). This does NOT revive
  the DEPRECATED `unparseable` triage label — the side table is a **separate
  mechanism**, so these rows never enter `raw_hits`/`triage_decisions` and never
  touch `sic_match_count`/`triaged_count`/`candidate_count`, and never reach the
  rule engine.
- **Exhibit-99 capture (TASKBOARD B3)**: hits triaged as candidates — and only
  those, to control request volume — get `raw_hits.exhibit_99_text` populated
  with the FULL cleaned text of the filing's EX-99.* documents (concatenated
  under `[EX-99.N filename]` headers, capped at `EXHIBIT_99_MAX_DOCS=3` with the
  overflow logged + noted in the stored text). Located via the filing-index
  **Type column** (`EX-99` prefix), NOT filename patterns: the ALAB-calibrated
  filename matcher in `fetch_sec_public_data.py` pulled 0 exhibits for BLZE's
  8-K because its EX-99.1 was named `ex991backblazecoreweavepre.htm` (R8
  lesson). The fetch runs AFTER the triage decision, so a missing EX-99 or a
  fetch failure can never alter a triage result (bias-to-human-review
  unchanged) — the column stays NULL with the reason in `weekly_scans.notes` +
  the run log. **Store-only**: the engine still classifies from the 8-K body
  alone; the EX-99 text is NOT fed to the classifier (a future calibration
  decision). No historical backfill: pre-B3 rows keep NULL — their triage rows
  exist, so rescans skip them before the exhibit path runs.
- **sic_match reconcile (TASKBOARD B6)**: the runner's headline `sic_match`
  figure (hence the commit message) is now `weekly_scans.sic_match_count` = the
  db row count persisted to `raw_hits`, NOT the per-run pre-parse matched-accession
  count (`len(matched_adsh)`), which on a single-run scan exceeds the persisted
  count by exactly that run's `meta_unparseable`. Pre-B6 the two diverged
  silently: the W23/W24/W25 backfill commits each reported the pre-parse count,
  one higher than the db column (W23 30 vs 29, W24 20 vs 19, W25 22 vs 21 — each
  over by its `meta_unparseable=1`). `meta_unparseable=N` is always reported
  separately.
- **No marker/exclusion/priority changes** were made for the integration or for
  B6; the engine file is untouched.

### Regression-test interface change (2026-07-05, TASKBOARD B1 — evidence)

`tests/test_rule_engine_regression.py::_load_golden` now scopes its golden
query to `WHERE r.scan_week = '2026-W21'`. Rationale: the golden contract is
the 18 committed Week-1 hand-triage labels; the Tier 6.3 runner appends later
weeks to the same committed db (those rows are engine OUTPUT under
`rule_engine_v1`, not golden), and the previously unscoped join would have
broken the `len(golden) == 18` assertion on the first post-W21 ingest.
Evidence: `python tests/test_rule_engine_regression.py` PASS (exit 0, R1 18/18
+ R5 3/3) immediately BEFORE and AFTER the first live runner ingest
(2026-W22: 173 raw → 12 SIC-matched → 12 triaged, 3 candidates
[ODYY/ITOX/STSS], 1 `oversized_marker` — first real cap-band rejection:
SNPS, ~$83.7B, a survivor whose body matches no content marker).

### Runner hardening (2026-07-12, TASKBOARD B6 — evidence)

Added the additive `meta_unparseable_hits` side table (in
`data/funnel_schema.sql` + `scripts/migrate_b6_meta_unparseable.py`, which is
`CREATE TABLE IF NOT EXISTS` + index and idempotent) and wired
`run_weekly_scan.record_unparseable_hit` into the metadata-unparseable branch,
plus the `sic_match` reconcile above. The rule engine
(`scripts/rule_engine.py`) markers/exclusions/priority are untouched.
Verification:

- `python tests/test_rule_engine_regression.py` PASS (exit 0, R1 18/18 + R5
  3/3) after the migration — the engine is untouched and the W21 golden is
  unaffected (the new table is not in the golden join).
- `python tests/test_runner_unparseable.py` PASS (exit 0) — proves the new path
  on a throwaway db built from `funnel_schema.sql` (no network): U1 accession +
  full FTS `_source` recoverable, U2 idempotent by accession, U3 partial parse
  (CIK-only) preserved, U4 the side table is isolated (raw_hits /
  triage_decisions stay empty, so the funnel stage counts are never inflated).
- Committed `data/funnel_log.db`: after running the migration, `raw_hits` (163),
  `triage_decisions` (163), `weekly_scans` (8) and `sqlite_sequence` are all
  sha256-identical to the pre-migration snapshot; only the new empty
  `meta_unparseable_hits` table was added. W28 (written by the Monday scan) was
  not touched; no existing week was re-run with `--execute` (dry-run + code
  walkthrough only — a `--start-date/--end-date` dry-run on W23 confirms the
  runner refuses to clobber the existing row without `--allow-rescan`).
- The 3 already-lost W23/W24/W25 accessions are NOT recoverable (they predate
  the fix — only `meta_unparseable=1` counts survive in those weeks' notes); B6
  makes all FUTURE metadata-unparseable hits recoverable.

### Exhibit-99 capture (2026-07-12, TASKBOARD B3 — evidence)

Added candidate-only EX-99 (press release) capture to `run_weekly_scan.py`
(`extract_exhibit99_entries` pure index parser + `fetch_exhibit_99_text`
network wrapper reusing `edgar_monitor` retry/cleaning), realizing the
`raw_hits.exhibit_99_text` TODO that stood since the schema was created. The
rule engine (`scripts/rule_engine.py`) markers/exclusions/priority are
untouched — B3 stores the text only. Verification:

- `python tests/test_rule_engine_regression.py` PASS (exit 0, R1 18/18 + R5
  3/3) and `python tests/test_runner_unparseable.py` PASS (exit 0) after the
  change.
- New `python tests/test_runner_exhibit99.py` PASS (exit 0; synthetic
  filing-index HTML, no network): E1 the exact BLZE-style filename
  (`ex991backblazecoreweavepre.htm`) that the filename-based matcher missed IS
  located via the Type column; E2 trailing `iXBRL` suffix stripped; E3 EX-99
  prefix match returns EX-99.1 + EX-99.2 in order while 8-K / EX-10.1 / GRAPHIC
  rows are excluded; E4 a no-EX-99 filing yields an empty list.
- Committed `data/funnel_log.db` byte-identical (no `--execute` run; no
  historical backfill by design). Dry-run on fresh week 2026-W29 exits 0 with
  zero HTTP (the dry-run path returns before `requests.Session()` is even
  constructed); dry-run on existing 2026-W23 still refuses without
  `--allow-rescan`.
