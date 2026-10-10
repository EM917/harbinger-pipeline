#!/usr/bin/env python3
"""
run_weekly_scan.py - Phase 1A Tier 6.3 — end-to-end weekly-scan runner (TASKBOARD B1).

Chains the funnel pipeline components into one persistent-record scan:

    edgar_monitor hit retrieval (EDGAR FTS)                        [network]
      -> SIC filter (client-side, config/sic_filters.yaml)
      -> FULL 8-K body fetch per SIC-matched hit                   [network]
      -> market cap via fetch_market_cap (yfinance-only, cached)   [network]
      -> rule_engine.classify(full_body, cap_mm)                   [pure]
      -> EX-99 press-release fetch, CANDIDATES only (TASKBOARD B3) [network]
      -> INSERT raw_hits + triage_decisions  (data/funnel_log.db)
      -> UPSERT weekly_scans summary row  (cap-band ACTIVE, not pass-through)

Why this script exists: after 737d9b5 gitignored the date-stamped digests, a scan
without a runner leaves NO persistent record (TASKBOARD risk R4). The committed
funnel log db is the durable record; human-readable digests remain a separate
edgar_monitor.py artifact if desired.

CLASSIFICATION INPUT (Tier 6.2 P1 lesson): the rule engine classifies from the
FULL cleaned 8-K body (fetched via edgar_monitor.fetch_filing_index +
fetch_document_body, exactly like build_week1_fixtures.py) — NEVER from the
300-char digest excerpt, which hid BNZI's debt substance past the cut and caused
the Tier 2.6 ma_marker misclassification. raw_hits.body_excerpt stores only a
display excerpt (extract_item_section, config.excerpt_max_chars); triage
evidence records the full-body char count actually classified.

EDGAR CACHING BUG: hit retrieval reuses edgar_monitor.fetch_item_hits, which is
the q="Item N.NN" phrase query + client-side post-filter on _source.items. Do
NOT "simplify" to the items= URL param — sequential items= queries return the
first call's cached response (full reproducer in edgar_monitor.py docstring).

ITEMS DEFAULT: --items defaults to "1.01" (NOT config.items_to_monitor). The
weekly funnel triage series is Item 1.01-based: the Week-1 baseline triaged
1.01 only, and the rule-engine denylist is distilled from 1.01 hits (RULES.md).
The 5.02 channel is TASKBOARD B4 (calibration pending); running it through a
1.01-distilled denylist would flood candidate_count. config.items_to_monitor
governs the edgar_monitor digest, not funnel triage. Override explicitly with
--items when B4 calibration work needs it.

REJECTION-LABEL RECONCILE (TASKBOARD B1): this runner emits ONLY the 5 content
markers + 'oversized_marker' + NULL(candidate). The 4 schema labels never
emitted by code are resolved as follows (rationale in data/funnel_schema.sql):
  - 'unparseable'         NOT emitted as a triage label (TASKBOARD B6 did NOT
                          revive it). Two failure modes, neither labeled here:
                          (a) body-fetch failure -> raw_hits row with
                          fetch_status='failed' + fetch_error, triaged as a
                          candidate (is_candidate=1, cap_resolved=0) so a fetch
                          failure never silently drops a potential candidate
                          (mirrors the cap_resolved=0 pattern).
                          (b) FTS metadata-unparseable (no ticker/CIK) -> cannot
                          enter raw_hits (ticker/cik NOT NULL); since TASKBOARD
                          B6 persisted to the meta_unparseable_hits side table
                          via record_unparseable_hit() (accession + full raw
                          _source recoverable) and counted separately in
                          weekly_scans.notes as meta_unparseable=N — no longer
                          silently dropped. See META-UNPARSEABLE PERSISTENCE.
  - 'missing_market_cap'  NOT emitted — superseded by cap_resolved=0 +
                          candidate (Tier 6.2 three-branch cap-band).
  - 'wrong_sic'           NOT emitted — SIC filtering runs client-side BEFORE
                          storage; non-matching hits are counted in the
                          raw_count -> sic_match_count funnel delta but never
                          inserted, so no row exists to carry the label.
  - 'ambiguous_manual_review'  NOT runner-emitted — reserved vocabulary for
                          the human judgment layer (manual_override=1 path).

META-UNPARSEABLE PERSISTENCE (TASKBOARD B6): a SIC-matched hit whose FTS
metadata cannot be parsed into a (ticker, CIK) pair cannot enter raw_hits
(ticker/cik NOT NULL). Pre-B6 it was SILENTLY DROPPED — only a
weekly_scans.notes count survived and the accession was UNRECOVERABLE (recurred
3x in the B2 backfill: W23/W24/W25 each dropped 1). Now each such hit is
persisted to the meta_unparseable_hits side table via record_unparseable_hit():
accession + all available raw metadata (full _source JSON) recoverable,
idempotent by accession (INSERT OR IGNORE). These rows never enter
raw_hits/triage_decisions, so sic_match_count / triaged_count / candidate_count
are unaffected; the count is surfaced separately as meta_unparseable=N.

SIC_MATCH RECONCILE (TASKBOARD B6): the runner's headline sic_match figure (and
therefore the commit message) equals weekly_scans.sic_match_count = the db row
count actually persisted to raw_hits — NOT the per-run pre-parse matched-accession
count (len(matched_adsh)), which on a single-run scan exceeds the persisted count
by exactly that run's meta_unparseable. Pre-B6 the two diverged silently: the
W23/W24/W25 backfill commits each reported the pre-parse count, one higher than
the db column (W23 30 vs 29, W24 20 vs 19, W25 22 vs 21 — each over by its
meta_unparseable=1). meta_unparseable=N is always reported separately.

WEEKLY_SCANS SEMANTICS (runner v1; the 2026-W21 baseline row predates these):
  raw_count        pre-SIC unique accessions retrieved from FTS (post item
                   post-filter + adsh dedupe). Accumulates across --allow-rescan
                   runs (chunked backfill); per-run breakdown appended to notes.
                   (W21 baseline row is SIC-filtered pass-through — its digest
                   source did not preserve the pre-SIC count.)
  sic_match_count  recomputed from db: raw_hits rows for the scan_week. This is
                   the commit-message figure (B6); SIC-matched hits that FAILED
                   metadata parse land in meta_unparseable_hits, not raw_hits, so
                   they are EXCLUDED here and reported as meta_unparseable=N.
  parsed_count     recomputed from db: raw_hits rows with fetch_status !=
                   'failed' (body fetched + cleaned, i.e. classifiable).
  triaged_count    recomputed from db: triage_decisions joined to the week.
  cap_match_count  recomputed from db: triaged_count - oversized_marker count.
                   Cap-band is ACTIVE: an oversized survivor shows up here and
                   in rejection_distribution['oversized_marker'].
  candidate_count  recomputed from db: is_candidate=1 rows for the week.

RERUN / CHUNKING: refuses to touch a scan_week that already has a weekly_scans
row unless --allow-rescan (protects the W21 baseline row; notes are always
APPENDED, never replaced). raw_hits/triage_decisions inserts are idempotent by
accession (already-ingested hits are skipped, so overlapping chunks never
duplicate rows; only raw_count over-counts on an overlapping re-run — see
notes trail). Wide ranges that trip the FTS pagination cap (1000) are flagged
in notes as PAGINATION-CAP-HIT; chunk the range with --start-date/--end-date +
--scan-week + --allow-rescan (TASKBOARD B2 backfill pattern).

EXHIBIT-99 CAPTURE (TASKBOARD B3): hits triaged as CANDIDATES — and only those,
to control request volume — get the filing's Exhibit 99 (press release) text
fetched into raw_hits.exhibit_99_text. Partnership substance often lives in
EX-99, not the 8-K body (BLZE: the CoreWeave MSA press release was EX-99.1).
Locator: the filing index is re-read and every row whose TYPE column starts
with 'EX-99' is fetched + cleaned via fetch_document_body, capped at
EXHIBIT_99_MAX_DOCS docs with '[EX-99.N filename]' headers (overflow logged +
noted in the stored text — no silent cap). Matching is on the index Type
column, NOT filename patterns: the ALAB-calibrated EXHIBIT_PATTERNS in
fetch_sec_public_data.py pulled 0 exhibits for BLZE because its EX-99.1 was
named 'ex991backblazecoreweavepre.htm' (words after the exhibit number defeat
the filename anchors — TASKBOARD R8 lesson); the Type cell said 'EX-99.1' all
along. Failure semantics: triage is decided BEFORE the exhibit fetch, so a
missing EX-99 or a fetch failure can NEVER alter a triage result
(bias-to-human-review unchanged) — exhibit_99_text stays NULL and the reason is
recorded in weekly_scans.notes + the run log. Body-fetch-failure candidates
also get an EX-99 attempt (the index re-read may succeed on retry — human
review then at least has the press release). NO historical backfill: rows
ingested pre-B3 keep NULL (their triage rows exist, so rescans skip them before
the exhibit path runs); the column fills for future scans only. The stored text
is the FULL cleaned exhibit text, never a truncated excerpt (Tier 6.2 P1
lesson). B3 STORES the text only — the rule engine still classifies from the
8-K body alone; feeding EX-99 text to the classifier is a future calibration
decision, out of B3 scope.

CLI:
    python scripts/run_weekly_scan.py --week 2026-W22                     # dry run (plan only)
    python scripts/run_weekly_scan.py --week 2026-W22 --execute           # live scan (ISO Mon..Sun)
    python scripts/run_weekly_scan.py --start-date 2026-05-25 --end-date 2026-05-28 --execute
    python scripts/run_weekly_scan.py --start-date 2026-05-29 --end-date 2026-05-31 \
        --scan-week 2026-W22 --allow-rescan --execute                     # chunk 2 of same week
    python scripts/run_weekly_scan.py --week 2026-W22 --items 1.01,5.02 --execute  # B4 experiments only

Dependencies: requests, pyyaml, beautifulsoup4 (via edgar_monitor); yfinance
(via fetch_market_cap; degrades to cap_resolved=0 candidates if unavailable).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import requests  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

from edgar_monitor import (  # noqa: E402
    NY_TZ,
    PAGINATION_MAX_HITS,
    SEC_RATE_LIMIT_SLEEP,
    _http_get_with_retries,
    extract_item_section,
    fetch_document_body,
    fetch_filing_index,
    fetch_item_hits,
    filter_by_sic,
    load_config,
    parse_display_name,
)
from fetch_market_cap import _load_cache, _save_cache, get_market_cap_mm  # noqa: E402
from rule_engine import classify  # noqa: E402

DEFAULT_DB = REPO_ROOT / "data" / "funnel_log.db"
DEFAULT_CACHE = REPO_ROOT / "data" / "market_cap_cache.json"
DEFAULT_CONFIG = "config/sic_filters.yaml"

TRIAGE_METHOD = "rule_engine_v1"
RUNNER_TAG = "run_weekly_scan v1"
DEFAULT_ITEMS = "1.01"
BASELINE_WEEK = "2026-W21"  # migrated hand-triage baseline; extra rescan caution

# Exhibit-99 capture (TASKBOARD B3): per-candidate request-volume bound. A
# filing rarely carries more than 2-3 EX-99.* docs; overflow is logged and
# noted inside the stored text (no silent cap).
EXHIBIT_99_MAX_DOCS = 3
EX99_NONE_NOTE = "no EX-99 exhibit in filing index"

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


# ---------------------------------------------------------------------------
# Scan-window resolution
# ---------------------------------------------------------------------------

def week_to_range(week: str) -> tuple[str, str]:
    """ISO week label 'YYYY-WNN' -> (monday_iso, sunday_iso).

    Note: the W21 baseline scan used a Sat-Fri window (2026-05-16..22); the
    runner standardizes on ISO Mon..Sun going forward. The 2026-05-23/24 gap
    between the two conventions is a weekend (no EDGAR dissemination).
    """
    m = _WEEK_RE.match(week)
    if not m:
        raise ValueError(f"invalid week label {week!r} (expected YYYY-WNN, e.g. 2026-W22)")
    y, w = int(m.group(1)), int(m.group(2))
    monday = date.fromisocalendar(y, w, 1)  # raises ValueError on bad week number
    sunday = date.fromisocalendar(y, w, 7)
    return monday.isoformat(), sunday.isoformat()


def resolve_window(args) -> tuple[str, str, str]:
    """Returns (scan_week, start_date, end_date). Exits 2 on invalid combos."""
    if args.week and (args.start_date or args.end_date):
        print("ERROR: --week and --start-date/--end-date are mutually exclusive", file=sys.stderr)
        sys.exit(2)
    if args.week:
        try:
            start, end = week_to_range(args.week)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        return args.week, start, end
    if not (args.start_date and args.end_date):
        print("ERROR: provide --week, or --start-date and --end-date together", file=sys.stderr)
        sys.exit(2)
    try:
        d_start = date.fromisoformat(args.start_date)
        d_end = date.fromisoformat(args.end_date)
    except ValueError as e:
        print(f"ERROR: invalid date: {e}", file=sys.stderr)
        sys.exit(2)
    if d_start > d_end:
        print("ERROR: --start-date is after --end-date", file=sys.stderr)
        sys.exit(2)
    sy, sw, _ = d_start.isocalendar()
    ey, ew, _ = d_end.isocalendar()
    if args.scan_week:
        if not _WEEK_RE.match(args.scan_week):
            print(f"ERROR: invalid --scan-week {args.scan_week!r} (expected YYYY-WNN)", file=sys.stderr)
            sys.exit(2)
        return args.scan_week, args.start_date, args.end_date
    if (sy, sw) != (ey, ew):
        print(
            "ERROR: date range spans multiple ISO weeks; pass --scan-week to label the "
            "weekly_scans row explicitly (chunked-backfill pattern)",
            file=sys.stderr,
        )
        sys.exit(2)
    return f"{sy}-W{sw:02d}", args.start_date, args.end_date


# ---------------------------------------------------------------------------
# Logging (edgar_monitor pattern; own filename + logger name)
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Path, run_ts_utc_compact: str) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_weekly_scan_{run_ts_utc_compact}.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    main_log = logging.getLogger("run_weekly_scan")
    main_log.setLevel(logging.INFO)
    main_log.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    main_log.addHandler(console)

    fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    main_log.addHandler(fh)

    return main_log, log_path


# ---------------------------------------------------------------------------
# weekly_scans recompute (db-derived stages are authoritative + dupe-safe)
# ---------------------------------------------------------------------------

def recompute_week(conn: sqlite3.Connection, scan_week: str) -> dict:
    sic_match, parsed = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN fetch_status != 'failed' THEN 1 ELSE 0 END), 0) "
        "FROM raw_hits WHERE scan_week = ?",
        (scan_week,),
    ).fetchone()
    rows = conn.execute(
        """
        SELECT t.rejection_label, t.is_candidate, COUNT(*)
        FROM triage_decisions t
        JOIN raw_hits r ON r.accession_number = t.accession_number
        WHERE r.scan_week = ?
        GROUP BY t.rejection_label, t.is_candidate
        """,
        (scan_week,),
    ).fetchall()
    triaged = sum(c for _, _, c in rows)
    candidates = sum(c for _, isc, c in rows if isc)
    dist: dict[str, int] = {}
    for label, _, c in rows:
        if label:
            dist[label] = dist.get(label, 0) + c
    cap_match = triaged - dist.get("oversized_marker", 0)
    return {
        "sic_match_count": sic_match,
        "parsed_count": parsed,
        "triaged_count": triaged,
        "candidate_count": candidates,
        "cap_match_count": cap_match,
        "rejection_distribution": dist,
    }


# ---------------------------------------------------------------------------
# Meta-unparseable persistence (TASKBOARD B6)
# ---------------------------------------------------------------------------

def record_unparseable_hit(
    conn: sqlite3.Connection,
    scan_week: str,
    scan_date: str,
    adsh: str,
    trigger_item: str,
    src: dict,
    reason: str,
) -> bool:
    """Persist a SIC-matched hit whose FTS metadata could not be parsed into a
    (ticker, CIK) pair — so raw_hits (ticker/cik NOT NULL) cannot hold it.

    Pre-B6 these hits were SILENTLY DROPPED: only a weekly_scans.notes count
    survived and the accession was unrecoverable (recurred 3x in the B2 backfill,
    W23/W24/W25). This writes the accession + every available raw field, plus the
    full FTS _source as JSON, so the hit is fully recoverable. Idempotent by
    accession (INSERT OR IGNORE): overlapping rescan chunks never duplicate rows.

    Does NOT touch raw_hits / triage_decisions / weekly_scans, and does NOT emit
    the DEPRECATED 'unparseable' triage label — this is a separate side table.

    Returns True iff a new row was inserted (False = accession already recorded).
    """
    display = (src.get("display_names") or [""])[0]
    ticker, _company, cik_dn = parse_display_name(display)
    ciks = src.get("ciks", [])
    cik = ciks[0] if ciks else cik_dn
    sics = src.get("sics", [])
    filing_type = src.get("file_type") or (src.get("root_forms") or ["8-K"])[0]
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO meta_unparseable_hits (
            scan_week, scan_date, accession_number, trigger_item, ticker, cik,
            display_name, sic_code, filing_type, filed_date, items,
            raw_source_json, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scan_week, scan_date, adsh, trigger_item, ticker, cik,
            display or None, sics[0] if sics else None, filing_type,
            src.get("file_date") or None, json.dumps(src.get("items", [])),
            json.dumps(src, sort_keys=True), reason,
        ),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Exhibit-99 capture (TASKBOARD B3) — candidates only
# ---------------------------------------------------------------------------

def extract_exhibit99_entries(index_html: bytes | str) -> list[tuple[str, str]]:
    """Parse a filing-index HTML page; return [(type, filename)] for every
    Document Format Files row whose TYPE cell starts with 'EX-99'
    (case-insensitive), in table order.

    TYPE-COLUMN MATCHING (TASKBOARD B3 / R8 lesson): the locator matches the
    index's Type column, NOT filename patterns. fetch_sec_public_data.py's
    filename-based EXHIBIT_PATTERNS (ALAB-calibrated) pulled 0 exhibits for the
    BLZE 8-K (accession 0001628280-26-044804, see that case's inputs/fetch.log)
    because its press release was named 'ex991backblazecoreweavepre.htm' —
    words after the exhibit number defeat the `...\\.htm$` filename anchors —
    while the Type cell said 'EX-99.1' all along. Row shape (>=5 cells,
    Document=cells[2], Type=cells[3]) and the trailing-' iXBRL' strip mirror
    edgar_monitor.fetch_filing_index.
    """
    soup = BeautifulSoup(index_html, "lxml")
    entries: list[tuple[str, str]] = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        ftype = cells[3].strip()
        if not ftype.upper().startswith("EX-99"):
            continue
        fname = re.sub(r"\s*iXBRL\s*$", "", cells[2]).strip()
        if fname:
            entries.append((ftype, fname))
    return entries


def fetch_exhibit_99_text(
    cik: str, adsh: str, session: requests.Session, log: logging.Logger,
) -> tuple[str | None, str]:
    """Fetch + clean the EX-99.* documents of one filing. Returns (text, note);
    text is None on no-EX-99 / total failure, with the reason in note.

    Called ONLY for hits triaged as candidates (request-volume control,
    TASKBOARD B3), and only AFTER the triage decision — so no outcome here can
    alter a triage result. Multiple EX-99 docs are concatenated under
    '[EX-99.N filename]' headers, capped at EXHIBIT_99_MAX_DOCS (overflow
    logged + noted in the stored text). Text is FULL cleaned exhibit text
    (fetch_document_body pipeline), never a truncated excerpt.
    """
    try:
        cik_int = str(int(cik))
    except (ValueError, TypeError):
        return None, f"EX-99 skipped: invalid cik {cik!r}"
    accession_no_dashes = adsh.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
        f"{accession_no_dashes}/{adsh}-index.htm"
    )
    r = _http_get_with_retries(session, url, None, log)
    time.sleep(SEC_RATE_LIMIT_SLEEP)
    if r is None:
        return None, "EX-99 index fetch failed"
    try:
        entries = extract_exhibit99_entries(r.content)
    except Exception as e:
        return None, f"EX-99 index parse failed: {e}"
    if not entries:
        return None, EX99_NONE_NOTE

    dropped = entries[EXHIBIT_99_MAX_DOCS:]
    entries = entries[:EXHIBIT_99_MAX_DOCS]
    if dropped:
        log.warning(
            f"  {adsh}: {len(dropped)} EX-99 doc(s) beyond cap {EXHIBIT_99_MAX_DOCS} "
            f"not fetched: {[f for _, f in dropped]}"
        )
    parts: list[str] = []
    failures: list[str] = []
    for ftype, fname in entries:
        text, err = fetch_document_body(cik, adsh, fname, session, log)
        time.sleep(SEC_RATE_LIMIT_SLEEP)
        if text is None:
            failures.append(f"{ftype} {fname}: {err}")
            log.warning(f"  {adsh}: EX-99 doc fetch failed ({ftype} {fname}: {err})")
        else:
            parts.append(f"[{ftype} {fname}] {text}")
    if not parts:
        return None, f"all {len(entries)} EX-99 fetch(es) failed: {'; '.join(failures)}"
    if dropped:
        parts.append(
            f"[{len(dropped)} additional EX-99 doc(s) beyond cap {EXHIBIT_99_MAX_DOCS} "
            f"not fetched: {', '.join(f for _, f in dropped)}]"
        )
    note = f"fetched {len(parts) - (1 if dropped else 0)}/{len(entries)} EX-99 doc(s)"
    if failures:
        note += f"; failed: {'; '.join(failures)}"
    if dropped:
        note += f"; {len(dropped)} beyond cap skipped"
    return "\n\n".join(parts), note


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(
        description="Tier 6.3 weekly-scan runner: EDGAR fetch -> rule engine -> funnel_log.db (4 tables)."
    )
    ap.add_argument("--week", default=None,
                    help="ISO week label YYYY-WNN; scans Monday..Sunday of that week (US/Eastern dates).")
    ap.add_argument("--start-date", default=None, help="YYYY-MM-DD; pair with --end-date (chunked scans).")
    ap.add_argument("--end-date", default=None, help="YYYY-MM-DD; pair with --start-date.")
    ap.add_argument("--scan-week", default=None,
                    help="Explicit weekly_scans label for --start-date/--end-date runs (required if the "
                         "range spans multiple ISO weeks). Ignored with --week.")
    ap.add_argument("--items", default=DEFAULT_ITEMS,
                    help=f"Comma-separated 8-K Item codes to triage (default: {DEFAULT_ITEMS}; see docstring "
                         "for why this does NOT default to config.items_to_monitor).")
    ap.add_argument("--sic", default=None,
                    help="Comma-separated SIC codes. Default: config.default_sic_codes.")
    ap.add_argument("--config", default=DEFAULT_CONFIG,
                    help=f"Path to SIC/Item config YAML (default: {DEFAULT_CONFIG}).")
    ap.add_argument("--db-path", default=str(DEFAULT_DB))
    ap.add_argument("--cache-path", default=str(DEFAULT_CACHE),
                    help="Market-cap cache JSON (gitignored).")
    ap.add_argument("--refresh-caps", action="store_true",
                    help="Ignore market-cap cache; re-fetch via yfinance.")
    ap.add_argument("--allow-rescan", action="store_true",
                    help="Permit running a scan_week that already has a weekly_scans row "
                         "(chunked backfill / recovery). Notes append; raw_count accumulates.")
    ap.add_argument("--execute", action="store_true",
                    help="Actually fetch + write db. Default: dry run (print plan, no HTTP, no writes).")
    args = ap.parse_args()

    scan_week, start_date, end_date = resolve_window(args)

    config_path_arg = Path(args.config)
    config_path = config_path_arg if config_path_arg.is_absolute() else REPO_ROOT / config_path_arg
    config = load_config(config_path)

    items = [i.strip() for i in args.items.split(",") if i.strip()]
    if args.sic:
        sic_set = {s.strip() for s in args.sic.split(",") if s.strip()}
    else:
        sic_set = {str(s) for s in config["default_sic_codes"]}
    excerpt_max_chars = int(config.get("excerpt_max_chars", 300))

    db_path = Path(args.db_path).resolve()
    cache_path = Path(args.cache_path).resolve()
    scan_date = datetime.now(NY_TZ).date().isoformat()

    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(REPO_ROOT))
        except ValueError:
            return str(p)

    # ---- Plan ----
    print("=" * 72)
    print("Weekly scan runner - plan (Tier 6.3, TASKBOARD B1)")
    print("=" * 72)
    print(f"scan_week:    {scan_week}")
    print(f"Date range:   {start_date} to {end_date} (US/Eastern; scan_date={scan_date})")
    print(f"Items:        {items}  (funnel triage default is 1.01-only; see docstring)")
    print(f"SIC codes:    {sorted(sic_set)}")
    print(f"DB:           {_rel(db_path)}")
    print(f"Cap cache:    {_rel(cache_path)} (refresh={args.refresh_caps})")
    print(f"Triage:       rule_engine.classify() on FULL 8-K body (Tier 6.2 P1 lesson); "
          f"method={TRIAGE_METHOD!r}")
    print(f"Query:        edgar_monitor.fetch_item_hits (q=\"Item N.NN\" phrase + _source.items "
          f"post-filter; items= URL param has an EDGAR caching bug)")
    print(f"Cap-band:     ACTIVE for survivors (oversized_marker at >= $5B; cap_resolved=0 on fetch miss)")
    print(f"Unparseable:  metadata-unparseable hits -> meta_unparseable_hits side table (recoverable), "
          f"reported separately as meta_unparseable=N, NOT in sic_match_count (TASKBOARD B6)")
    print(f"Exhibit 99:   fetched for CANDIDATE hits only (filing-index Type=EX-99* match, "
          f"max {EXHIBIT_99_MAX_DOCS} docs) -> raw_hits.exhibit_99_text; no-EX-99/failure keeps NULL + "
          f"notes record, NEVER alters triage (TASKBOARD B3)")
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT scan_date, raw_count, candidate_count, sic_match_count FROM weekly_scans WHERE scan_week = ?",
            (scan_week,),
        ).fetchone()
        has_unpar_tbl = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta_unparseable_hits'"
        ).fetchone() is not None
        unpar_wk = conn.execute(
            "SELECT COUNT(*) FROM meta_unparseable_hits WHERE scan_week = ?", (scan_week,)
        ).fetchone()[0] if has_unpar_tbl else None
        conn.close()
        if row:
            print(f"weekly_scans: row EXISTS for {scan_week} (scan_date={row[0]}, raw={row[1]}, "
                  f"sic_match={row[3]}, candidates={row[2]}) -> requires --allow-rescan")
        else:
            print(f"weekly_scans: no row for {scan_week} (fresh scan)")
        if not has_unpar_tbl:
            print(f"meta_unparseable_hits: MISSING -> run scripts/migrate_b6_meta_unparseable.py before --execute")
        else:
            print(f"meta_unparseable_hits: present; {unpar_wk} row(s) already recorded for {scan_week}")
    else:
        print(f"WARNING: db not found at {_rel(db_path)} (run scripts/init_funnel_log.py + migrations first)")
    print("=" * 72)

    if not args.execute:
        print("\n[dry run] No HTTP calls, no db writes. Re-run with --execute to scan.")
        return 0

    # ---- Execute ----
    t0 = time.time()
    run_ts_compact = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    log, log_path = setup_logging(REPO_ROOT / "signals" / "cache", run_ts_compact)
    log.info("==== run_weekly_scan started ====")
    log.info(f"scan_week={scan_week} range={start_date}..{end_date} items={items} sic_n={len(sic_set)}")

    if not db_path.exists():
        log.error(f"db not found: {db_path} — run scripts/init_funnel_log.py + migrate scripts first")
        return 1
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")
    tri_cols = {r[1] for r in conn.execute("PRAGMA table_info(triage_decisions)").fetchall()}
    if "cap_resolved" not in tri_cols:
        log.error("triage_decisions.cap_resolved missing — run scripts/migrate_tier62_cap_resolved.py first")
        conn.close()
        return 1
    db_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "meta_unparseable_hits" not in db_tables:
        log.error("meta_unparseable_hits table missing — run scripts/migrate_b6_meta_unparseable.py first (TASKBOARD B6)")
        conn.close()
        return 1

    existing_week = conn.execute(
        "SELECT raw_count, notes FROM weekly_scans WHERE scan_week = ?", (scan_week,)
    ).fetchone()
    if existing_week and not args.allow_rescan:
        log.error(
            f"weekly_scans already has a row for {scan_week}; re-run with --allow-rescan to upsert "
            f"(notes append, raw_count accumulates). The {BASELINE_WEEK} baseline row should "
            f"normally never be rescanned."
        )
        conn.close()
        return 1

    cap_cache = _load_cache(cache_path)
    session = requests.Session()

    # ---- Phase 1: hit retrieval (per item), merge unique accessions ----
    merged: dict[str, dict] = {}  # adsh -> {"hit": hit, "trigger_item": first queried item that matched}
    cap_hit_any = False
    for item in items:
        log.info(f"---- Querying Item {item} ----")
        hits, cap_hit = fetch_item_hits(item, start_date, end_date, session, log)
        cap_hit_any = cap_hit_any or cap_hit
        for h in hits:
            adsh = h.get("_source", {}).get("adsh")
            if adsh and adsh not in merged:
                merged[adsh] = {"hit": h, "trigger_item": item}
        time.sleep(SEC_RATE_LIMIT_SLEEP)
    raw_count_run = len(merged)
    log.info(f"retrieved {raw_count_run} unique accessions across items={items} (pre-SIC)")
    if cap_hit_any:
        log.warning(
            f"PAGINATION CAP HIT ({PAGINATION_MAX_HITS}): result set truncated — raw_count under-counts; "
            f"chunk this week via --start-date/--end-date + --scan-week + --allow-rescan"
        )

    # ---- Phase 2: SIC filter (client-side, pre-storage; see 'wrong_sic' reconcile note) ----
    sic_matched_hits, sic_unmatched_n = filter_by_sic([v["hit"] for v in merged.values()], sic_set)
    matched_adsh = {h["_source"]["adsh"] for h in sic_matched_hits if h.get("_source", {}).get("adsh")}
    log.info(f"SIC filter: {len(matched_adsh)} matched, {sic_unmatched_n} filtered out (counted only, not stored)")

    # ---- Phase 3: per-hit body fetch + market cap + classify + insert ----
    counters = {
        "inserted": 0, "triaged": 0, "candidates": 0, "skipped_existing": 0,
        "meta_unparseable": 0, "unparseable_new": 0, "unparseable_existing": 0,
        "fetch_failed": 0, "recovered_triage": 0,
        "exhibit99_fetched": 0, "exhibit99_none": 0, "exhibit99_failed": 0,
    }
    candidate_tickers: list[str] = []
    exhibit99_failures: list[str] = []

    for adsh, entry in merged.items():
        if adsh not in matched_adsh:
            continue
        src = entry["hit"].get("_source", {})
        trigger_item = entry["trigger_item"]
        display = (src.get("display_names") or [""])[0]
        ticker, company, cik_dn = parse_display_name(display)
        ciks = src.get("ciks", [])
        cik = ciks[0] if ciks else cik_dn
        if not ticker or not cik:
            counters["meta_unparseable"] += 1
            reason = f"unparseable FTS metadata: ticker={ticker!r}, cik={cik!r}"
            newly = record_unparseable_hit(conn, scan_week, scan_date, adsh, trigger_item, src, reason)
            conn.commit()  # per-hit transaction: crash-safe; rerun skips via INSERT OR IGNORE
            if newly:
                counters["unparseable_new"] += 1
                log.warning(
                    f"[unparseable] {adsh}: {reason} (display={display!r}) — cannot enter raw_hits "
                    f"(ticker/cik NOT NULL); persisted to meta_unparseable_hits for recovery (TASKBOARD B6)"
                )
            else:
                counters["unparseable_existing"] += 1
                log.info(f"[unparseable] {adsh}: already recorded in meta_unparseable_hits — skipped")
            continue

        exists_raw = conn.execute(
            "SELECT 1 FROM raw_hits WHERE accession_number = ?", (adsh,)
        ).fetchone() is not None
        exists_tri = conn.execute(
            "SELECT 1 FROM triage_decisions WHERE accession_number = ?", (adsh,)
        ).fetchone() is not None
        if exists_raw and exists_tri:
            counters["skipped_existing"] += 1
            log.info(f"[skip] {adsh} ({ticker}): already ingested + triaged")
            continue
        if exists_raw and not exists_tri:
            counters["recovered_triage"] += 1
            log.warning(f"[recover] {adsh} ({ticker}): raw_hits row exists without triage — re-classifying")

        # Full body fetch (classification input; Tier 6.2 P1 lesson)
        log.info(f"[body] {ticker} {adsh}: fetching filing index + primary 8-K")
        filename, idx_err = fetch_filing_index(cik, adsh, session, log)
        time.sleep(SEC_RATE_LIMIT_SLEEP)
        body: str | None = None
        body_err: str | None = f"index: {idx_err}" if filename is None else None
        if filename is not None:
            body, doc_err = fetch_document_body(cik, adsh, filename, session, log)
            time.sleep(SEC_RATE_LIMIT_SLEEP)
            if body is None:
                body_err = f"doc: {doc_err}"

        # Market cap (yfinance-only, cached; written into raw_hits at insert)
        cap_mm = get_market_cap_mm(
            ticker, cap_cache, refresh=args.refresh_caps,
            log=lambda m: log.warning(m.removeprefix("WARNING: ")),
        )

        if body is not None:
            res = classify(body, cap_mm)
            excerpt = extract_item_section(body, trigger_item, excerpt_max_chars)
            if excerpt.startswith("["):
                fetch_status, fetch_error = "partial", f"display-excerpt extraction: {excerpt}"
            else:
                fetch_status, fetch_error = "success", None
            is_cand = res.is_candidate
            rej_label = res.rejection_label
            cap_resolved = res.cap_resolved
            evidence = (
                f"{res.evidence_prose(TRIAGE_METHOD)} "
                f"[classified from full body, {len(body)} chars; {RUNNER_TAG} scan {scan_week}]"
            )
            for w in res.warnings:
                log.warning(f"[{ticker}] {w}")
        else:
            counters["fetch_failed"] += 1
            excerpt = f"[body unavailable: {body_err}]"
            fetch_status, fetch_error = "failed", body_err
            # Bias-to-human-review: never silently drop on a fetch failure. Content
            # markers precede cap-band, so cap-band is NOT applied to an unread body.
            is_cand, rej_label, cap_resolved = 1, None, 0
            evidence = (
                f"{TRIAGE_METHOD}: body unavailable ({body_err}); bias-to-human-review — surfaced as "
                f"candidate WITHOUT content-marker evaluation (cap-band skipped: content markers "
                f"precede it; cap_resolved=0) [{RUNNER_TAG} scan {scan_week}]"
            )
            log.warning(f"[{ticker}] {adsh}: body fetch failed ({body_err}) — surfacing to human review")

        # Exhibit-99 capture (TASKBOARD B3): CANDIDATES only (request-volume
        # control). Triage (is_cand/rej_label/cap_resolved) is already decided
        # above, so nothing in this block can alter it — any failure keeps
        # exhibit_99_text NULL with the reason in notes + log. exists_tri is
        # always False here (ingested+triaged hits `continue`d earlier), so
        # pre-B3 / prior-week rows are never re-fetched (no historical backfill).
        exhibit_99_text: str | None = None
        if is_cand:
            log.info(f"[ex99] {ticker} {adsh}: locating EX-99 exhibits via filing-index Type column")
            try:
                exhibit_99_text, ex_note = fetch_exhibit_99_text(cik, adsh, session, log)
            except Exception as e:  # defensive: an exhibit bug must never kill the scan loop
                exhibit_99_text, ex_note = None, f"EX-99 fetch raised {type(e).__name__}: {e}"
            if exhibit_99_text is not None:
                counters["exhibit99_fetched"] += 1
                log.info(f"[ex99] {ticker} {adsh}: {ex_note} ({len(exhibit_99_text)} chars)")
            elif ex_note == EX99_NONE_NOTE:
                counters["exhibit99_none"] += 1
                log.info(f"[ex99] {ticker} {adsh}: {ex_note} — exhibit_99_text stays NULL")
            else:
                counters["exhibit99_failed"] += 1
                exhibit99_failures.append(f"{ticker} {adsh}: {ex_note}")
                log.warning(f"[ex99] {ticker} {adsh}: {ex_note} — exhibit_99_text stays NULL (triage unchanged)")

        filing_type = src.get("file_type") or (src.get("root_forms") or ["8-K"])[0]
        sics = src.get("sics", [])

        if not exists_raw:
            conn.execute(
                """
                INSERT INTO raw_hits (
                    scan_week, scan_date, accession_number, cik, ticker, company_name,
                    filing_type, filed_date, items, sic_code, market_cap_millions,
                    body_excerpt, exhibit_99_text, fetch_status, fetch_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_week, scan_date, adsh, cik, ticker, company,
                    filing_type, src.get("file_date", ""), json.dumps(src.get("items", [])),
                    sics[0] if sics else None, cap_mm,
                    excerpt,
                    exhibit_99_text,  # TASKBOARD B3: EX-99 full text for candidates; NULL for rejects / no-EX-99 / fetch failure
                    fetch_status, fetch_error,
                ),
            )
            counters["inserted"] += 1
        elif exhibit_99_text is not None:
            # Recovered-triage crash completion (raw row from an interrupted run
            # of THIS scan, exhibit column still empty): fill only if NULL —
            # never overwrites, and prior-week rows can't reach here (their
            # triage rows exist, so they were skipped above).
            conn.execute(
                "UPDATE raw_hits SET exhibit_99_text = ? "
                "WHERE accession_number = ? AND exhibit_99_text IS NULL",
                (exhibit_99_text, adsh),
            )
        if not exists_tri:
            conn.execute(
                """
                INSERT INTO triage_decisions (
                    accession_number, rejection_label, is_candidate, triage_method,
                    triage_evidence, manual_override, override_reason, cap_resolved
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (adsh, rej_label, is_cand, TRIAGE_METHOD, evidence, 0, None, cap_resolved),
            )
            counters["triaged"] += 1
            if is_cand:
                counters["candidates"] += 1
                candidate_tickers.append(ticker)
                log.info(f"[CANDIDATE] {ticker} {adsh} cap_mm={cap_mm} — surfaced for human §0–§8 review")
            else:
                log.info(f"[reject] {ticker} {adsh}: {rej_label}")
        conn.commit()  # per-hit transaction: crash-safe, rerun skips completed hits

    _save_cache(cache_path, cap_cache)

    # ---- Phase 4: weekly_scans upsert (db-derived stages recomputed; raw accumulates) ----
    agg = recompute_week(conn, scan_week)
    scan_status = "success" if (
        counters["fetch_failed"] == 0 and counters["meta_unparseable"] == 0 and not cap_hit_any
    ) else "partial"
    duration = round(time.time() - t0, 1)
    note_line = (
        f"[{RUNNER_TAG} {scan_date} range {start_date}..{end_date} items={','.join(items)}] "
        f"raw={raw_count_run} sic_match={agg['sic_match_count']} preparse_matched={len(matched_adsh)} "
        f"inserted={counters['inserted']} skipped_existing={counters['skipped_existing']} "
        f"meta_unparseable={counters['meta_unparseable']}(new={counters['unparseable_new']}) "
        f"fetch_failed={counters['fetch_failed']} recovered_triage={counters['recovered_triage']} "
        f"exhibit99_fetched={counters['exhibit99_fetched']} exhibit99_none={counters['exhibit99_none']} "
        f"exhibit99_failed={counters['exhibit99_failed']} "
        f"candidates_new={counters['candidates']}"
        + (f" candidates={candidate_tickers}" if candidate_tickers else "")
        + (f" exhibit99_failures={exhibit99_failures}" if exhibit99_failures else "")
        + (" PAGINATION-CAP-HIT(raw under-counts; chunk the range)" if cap_hit_any else "")
        + "; sic_match=weekly_scans.sic_match_count (raw_hits rows persisted for the week; the commit-message "
          "figure), preparse_matched=this run's SIC-matched accessions before metadata parse; meta_unparseable "
          "persisted to meta_unparseable_hits (recoverable), reported separately and NOT in sic_match_count "
          "(TASKBOARD B6); raw_count=pre-SIC unique accessions, accumulates across rescans (W21 baseline row was "
          "SIC-filtered pass-through); cap_match_count = triaged - oversized_marker (cap-band ACTIVE); "
          "exhibit_99_text populated for NEW candidates only since TASKBOARD B3 (filing-index Type=EX-99* match, "
          "full cleaned text; no-EX-99/failure keeps NULL and never alters triage; no historical backfill)"
    )
    if existing_week:
        raw_total = existing_week[0] + raw_count_run
        notes = (existing_week[1] + " | " if existing_week[1] else "") + note_line
    else:
        raw_total = raw_count_run
        notes = note_line
    conn.execute(
        """
        INSERT OR REPLACE INTO weekly_scans (
            scan_week, scan_date, raw_count, parsed_count, sic_match_count,
            cap_match_count, triaged_count, candidate_count,
            rejection_distribution, scan_status, scan_duration_seconds, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scan_week, scan_date, raw_total, agg["parsed_count"], agg["sic_match_count"],
            agg["cap_match_count"], agg["triaged_count"], agg["candidate_count"],
            json.dumps(agg["rejection_distribution"], sort_keys=True),
            scan_status, duration, notes,
        ),
    )
    conn.commit()
    conn.close()

    log.info("==== run_weekly_scan complete ====")
    print()
    print("Done.")
    print(f"  scan_week:       {scan_week} ({start_date} .. {end_date})")
    print(f"  raw (pre-SIC):   {raw_count_run} this run (weekly_scans.raw_count={raw_total})")
    print(f"  SIC-matched:     {agg['sic_match_count']} persisted to raw_hits "
          f"(= weekly_scans.sic_match_count, the commit-message figure)")
    print(f"                   this run: {len(matched_adsh)} pre-parse matched -> {counters['inserted']} new "
          f"raw_hits, {counters['skipped_existing']} already ingested")
    print(f"  meta-unparseable:{counters['meta_unparseable']} this run "
          f"({counters['unparseable_new']} newly persisted to meta_unparseable_hits, "
          f"{counters['unparseable_existing']} already recorded) — recoverable, separate from sic_match_count (B6)")
    print(f"  triaged:         {counters['triaged']} new decisions (method={TRIAGE_METHOD}; "
          f"{counters['fetch_failed']} body-fetch failures surfaced as candidates)")
    print(f"  candidates:      {agg['candidate_count']} for the week"
          + (f" — this run: {candidate_tickers}" if candidate_tickers else ""))
    print(f"  exhibit-99:      {counters['exhibit99_fetched']} fetched, {counters['exhibit99_none']} no-EX-99, "
          f"{counters['exhibit99_failed']} failed (candidates only; failures keep NULL, triage unchanged; "
          f"TASKBOARD B3)")
    print(f"  rejection dist:  {json.dumps(agg['rejection_distribution'], sort_keys=True)}")
    print(f"  weekly_scans:    upserted {scan_week} (status={scan_status}, {duration}s)")
    # Copy-paste commit summary — sic_match is the db column (TASKBOARD B6 reconcile); meta_unparseable separate.
    print(f"  commit summary:  weekly scan {scan_week}: raw={raw_total}, sic_match={agg['sic_match_count']}, "
          f"triaged={agg['triaged_count']}, candidates={agg['candidate_count']}, "
          f"meta_unparseable={counters['meta_unparseable']}")
    print(f"  Log:             {_rel(log_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
