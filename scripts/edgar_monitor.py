#!/usr/bin/env python3
"""
edgar_monitor.py - Tier 4 Lite Component 1.

Pulls recent 8-K filings from the EDGAR full-text search API, filters
hits client-side by SIC code (from `_source.sics` in the response;
no separate CIK-to-SIC lookup needed), and renders a per-Item markdown
digest of candidate signals to `signals/edgar_{YYYY-MM-DD}.md`.

API: https://efts.sec.gov/LATEST/search-index

QUERY STRATEGY: `q="Item N.NN"` phrase query + client-side post-filter
on `_source.items`. This is the spec's "fallback strategy 2", chosen
over the cleaner `items=N.NN` URL param after discovering an EDGAR-side
caching bug:

  Bug (verified 2026-05-22 plan-phase, reproducible via system `curl`):
  Sequential queries against efts.sec.gov with `items=` URL param return
  the FIRST call's response for all subsequent calls within a short
  window (~minutes), regardless of items= value. e.g., requesting
  items=1.01, items=5.02, items=8.01 in sequence returns items=1.01's
  hits THREE times. Confirmed not a Python `requests` session issue:
  reproduces from system curl in fresh shells and across processes.
  Cache-Control: no-cache, Connection: close, cache-busting query
  params (`_=<timestamp>`) all fail to bust it.

  Workaround: `q="Item N.NN"` phrase query goes through a different
  EDGAR code path and returns distinct results across sequential calls.
  Slight false-positive risk (8-K body text mentioning "Item N.NN" when
  referring to prior filings); mitigated by client-side post-filter
  requiring the requested item to be present in `_source.items` (which
  is the filing's authoritative item list).

Timezone: all date math in US/Eastern (EDGAR canonical timezone). If
script runs at 23:00 PT, "today" is the next ET date.

CLI:
    python scripts/edgar_monitor.py                                        # dry run, prints plan
    python scripts/edgar_monitor.py --execute                              # live fetch + write digest (excerpts ON by default)
    python scripts/edgar_monitor.py --no-excerpts --execute                # metadata-only digest (faster, no body fetches)
    python scripts/edgar_monitor.py --lookback-days 14 --execute
    python scripts/edgar_monitor.py --start-date 2026-05-15 --end-date 2026-05-22 --execute
    python scripts/edgar_monitor.py --items 1.01,5.02 --sic 3674 --execute

Body excerpt fetching (Component 3, added 2026-05-23): for each matched hit,
fetch filing index + primary 8-K body, extract per-Item section text (first-
match + content-length heuristic for cross-reference disambiguation), truncate
to config.excerpt_max_chars (default 300; or first sentence boundary in
[150, max_chars] whichever yields cleaner break), and surface as
"**Item N.NN excerpt:**" line in each per-hit digest entry. Per-run body
cache deduplicates multi-item 8-Ks (fetch each accession only once).
Disabled via --no-excerpts for metadata-only mode.

Output:
    signals/edgar_{YYYY-MM-DD}.md  (ET date; per-Item markdown digest)
    signals/cache/edgar_monitor_{UTC-timestamp}.log  (run log; gitignored)

Engineering preservations from scripts/fetch_sec_public_data.py (canonical):
- EDGAR_USER_AGENT verbatim
- SEC_RATE_LIMIT_SLEEP = 0.15 between paginated calls
- Accept-Encoding: gzip, deflate
- sys.stdout.reconfigure(encoding="utf-8", errors="replace") in main()
- ZoneInfo("America/New_York") for date math (US/Eastern alias)
- Vanilla `requests` for SEC EDGAR endpoints (no curl_cffi needed for
  efts.sec.gov; curl_cffi is only used in fetch_sec_public_data.py for
  IR-page Cloudflare bypass)

Dependencies: requests, pyyaml. On Windows: tzdata (for zoneinfo).
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Modern 8-K filings embed inline XBRL (iXBRL); bs4 emits XMLParsedAsHTMLWarning
# when parsing them with the lxml HTML parser, which is what we want for text
# extraction. Suppress the noise.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

# ---------------------------------------------------------------------------
# Constants (preserved from fetch_sec_public_data.py where applicable)
# ---------------------------------------------------------------------------

EDGAR_FTS_URL = "https://efts.sec.gov/LATEST/search-index"

# SEC fair-access policy: User-Agent must include contact info.
EDGAR_USER_AGENT = "event-driven-research-lab/0.1 (william.mei0917@gmail.com)"

# Politeness sleep between paginated calls (SEC publishes 10 req/sec ceiling).
SEC_RATE_LIMIT_SLEEP = 0.15

# Pagination: EDGAR FTS default page size is 100. Safety cap at 1000 hits per
# Item query to prevent runaway scans on wide date ranges.
PAGINATION_PAGE_SIZE = 100
PAGINATION_MAX_HITS = 1000

NY_TZ = ZoneInfo("America/New_York")

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    """Load + validate config/sic_filters.yaml. Exits with code 1 on
    unrecoverable error."""
    if not path.exists():
        print(f"ERROR: config file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        loc = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        print(f"ERROR: YAML parse failed{loc}: {path}\n  {getattr(e, 'problem', str(e))}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"ERROR: config root must be a mapping, got {type(data).__name__}", file=sys.stderr)
        sys.exit(1)
    for key in ("default_sic_codes", "items_to_monitor", "item_descriptions"):
        if key not in data:
            print(f"ERROR: config missing required key `{key}`", file=sys.stderr)
            sys.exit(1)
    data.setdefault("default_lookback_days", 7)
    return data


# ---------------------------------------------------------------------------
# Effective-parameter resolution (CLI overrides + config defaults)
# ---------------------------------------------------------------------------

def resolve_effective_params(args, config: dict) -> dict:
    """Merge CLI args over config; resolve date range in US/Eastern."""
    now_et = datetime.now(NY_TZ)
    today_et = now_et.date()

    # Items
    if args.items:
        items = [i.strip() for i in args.items.split(",") if i.strip()]
    else:
        items = [str(i) for i in config["items_to_monitor"]]

    # SIC set (strings; EDGAR returns strings in _source.sics)
    if args.sic:
        sic_set = {s.strip() for s in args.sic.split(",") if s.strip()}
    else:
        sic_set = {str(s) for s in config["default_sic_codes"]}

    # Date range
    if args.start_date and args.end_date:
        start_date = args.start_date
        end_date = args.end_date
    elif args.start_date or args.end_date:
        print("ERROR: --start-date and --end-date must be provided together", file=sys.stderr)
        sys.exit(1)
    else:
        lookback = args.lookback_days if args.lookback_days is not None else config.get("default_lookback_days", 7)
        end_date = today_et.isoformat()
        start_date = (today_et - timedelta(days=lookback)).isoformat()

    # Output path (defaults to signals/edgar_{today_ET}.md)
    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = REPO_ROOT / output_path
    else:
        output_path = REPO_ROOT / "signals" / f"edgar_{today_et.isoformat()}.md"

    # Excerpt config (Component 3)
    if args.with_excerpts is None:
        with_excerpts = bool(config.get("excerpt_fetch_enabled_default", True))
    else:
        with_excerpts = bool(args.with_excerpts)
    excerpt_max_chars = int(config.get("excerpt_max_chars", 300))

    return {
        "items": items,
        "sic_set": sic_set,
        "start_date": start_date,
        "end_date": end_date,
        "output_path": output_path,
        "today_et": today_et.isoformat(),
        "run_ts_utc": datetime.now(timezone.utc).isoformat(),
        "with_excerpts": with_excerpts,
        "excerpt_max_chars": excerpt_max_chars,
    }


# ---------------------------------------------------------------------------
# Logging (mirrors fetch_sec_public_data.py L106-130 pattern)
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Path, run_ts_utc_compact: str) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"edgar_monitor_{run_ts_utc_compact}.log"

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    main_log = logging.getLogger("edgar_monitor")
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
# Display-name parsing
# ---------------------------------------------------------------------------

_CIK_TAIL_RE = re.compile(r"\(CIK\s+(\d+)\)\s*$")
# Multi-ticker pairing support (Component 1.1 fix): EDGAR display_names
# frequently group common-share + warrant tickers as "(SKIL, SKILW)" or
# common + multiple warrant tranches. Capture the FIRST (primary common-
# share) ticker; ignore subsequent comma-separated entries. Single-ticker
# format remains supported because the trailing group is zero-or-more.
# Known limitation (not handled here): semicolon-separated pairings
# "(TICK1; TICK2)" -- not observed in EDGAR sample as of 2026-05-23.
_TICKER_TAIL_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]*)(?:,\s*[A-Z][A-Z0-9.\-]*)*\)\s*$")


def parse_display_name(s: str) -> tuple[str | None, str, str | None]:
    """Parse an EDGAR FTS `display_names[0]` string into (ticker, company, cik).

    Examples:
      "REALLOYS INC.  (ALOY)  (CIK 0001567900)" -> ("ALOY", "REALLOYS INC.", "0001567900")
      "CNL Strategic Residential Credit, Inc.  (CIK 0002066337)" -> (None, "CNL ...", "0002066337")

    No-ticker case (trusts, certain foreign filers): ticker = None.
    Unparseable input: ticker = None, company = original string, cik = None.
    """
    cik_match = _CIK_TAIL_RE.search(s)
    if cik_match:
        cik: str | None = cik_match.group(1)
        s_no_cik = s[: cik_match.start()].rstrip()
    else:
        cik = None
        s_no_cik = s

    ticker_match = _TICKER_TAIL_RE.search(s_no_cik)
    if ticker_match:
        ticker: str | None = ticker_match.group(1)
        company = s_no_cik[: ticker_match.start()].rstrip()
    else:
        ticker = None
        company = s_no_cik.rstrip()

    return ticker, company, cik


# ---------------------------------------------------------------------------
# Filing URL construction (per spec section "EDGAR filing URL")
# ---------------------------------------------------------------------------

def build_filing_url(cik: str, accession_with_dashes: str) -> str:
    """Construct the canonical filing index URL.

    Args:
      cik: CIK in any form (zero-padded or not); converted to integer for URL.
      accession_with_dashes: e.g., '0001045810-26-000123'

    Returns:
      https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{accession_with_dashes}-index.htm
    """
    cik_int = str(int(cik))  # strip leading zeros
    accession_no_dashes = accession_with_dashes.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
        f"{accession_no_dashes}/{accession_with_dashes}-index.htm"
    )


# ---------------------------------------------------------------------------
# HTTP with retries (handles 429/403 + transient {message: ...} responses)
# ---------------------------------------------------------------------------

def _http_get_with_retries(
    session: requests.Session,
    url: str,
    params: dict | None,
    log: logging.Logger,
    max_attempts: int = 4,
    extra_headers: dict | None = None,
) -> requests.Response | None:
    """Generic GET with exponential-backoff retry on network errors and 429/403.

    Returns Response on success (200), None on permanent failure (logged).
    Caller parses body (JSON or HTML) and handles content-type validation.
    """
    headers = {
        "User-Agent": EDGAR_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }
    if extra_headers:
        headers.update(extra_headers)
    backoff = 1
    for attempt in range(1, max_attempts + 1):
        try:
            r = session.get(url, params=params, headers=headers, timeout=30)
        except Exception as e:
            log.warning(f"  network error (attempt {attempt}/{max_attempts}): {e}")
            if attempt == max_attempts:
                log.error(f"  exhausted retries; giving up on {url}")
                return None
            time.sleep(backoff)
            backoff *= 2
            continue

        if r.status_code in (429, 403):
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = int(retry_after)
            else:
                wait = backoff
            log.warning(f"  HTTP {r.status_code} (attempt {attempt}/{max_attempts}); waiting {wait}s")
            if attempt == max_attempts:
                log.error(f"  rate-limited; giving up after {max_attempts} attempts")
                return None
            time.sleep(wait)
            backoff *= 2
            continue

        if r.status_code != 200:
            log.error(f"  HTTP {r.status_code}: {r.text[:200]!r}")
            return None

        return r

    return None  # safety net


def _http_get_json_with_retries(
    session: requests.Session,
    url: str,
    params: dict,
    log: logging.Logger,
    max_attempts: int = 4,
) -> dict | None:
    """GET JSON via _http_get_with_retries + handle the EDGAR transient
    {"message": "..."} edge case (HTTP 200 but no hits key)."""
    backoff = 1
    for attempt in range(1, max_attempts + 1):
        r = _http_get_with_retries(session, url, params, log, max_attempts=1)
        if r is None:
            if attempt == max_attempts:
                return None
            time.sleep(backoff)
            backoff *= 2
            continue
        try:
            data = r.json()
        except Exception as e:
            log.error(f"  JSON parse failed: {e}; body={r.text[:200]!r}")
            return None

        # EDGAR sometimes returns {"message": "..."} on transient errors with HTTP 200.
        if "hits" not in data and "message" in data:
            log.warning(f"  transient API message (attempt {attempt}/{max_attempts}): {data.get('message')!r}")
            if attempt == max_attempts:
                log.error("  exhausted retries on transient API message")
                return None
            time.sleep(backoff)
            backoff *= 2
            continue

        return data

    return None  # safety net


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def fetch_paginated_hits(
    base_params: dict,
    session: requests.Session,
    log: logging.Logger,
) -> tuple[list[dict], bool]:
    """Fetch all hits via `from=` offset pagination. Returns (hits, cap_hit).

    cap_hit = True iff total reported > PAGINATION_MAX_HITS (i.e., result set
    was genuinely truncated, not just exactly at the cap by coincidence).
    """
    all_hits: list[dict] = []
    offset = 0
    total_known: int | None = None
    pages_fetched = 0

    while offset < PAGINATION_MAX_HITS:
        params = {**base_params, "from": offset}
        log.info(f"GET {EDGAR_FTS_URL} {params}")
        data = _http_get_json_with_retries(session, EDGAR_FTS_URL, params, log)
        if data is None:
            log.error("  pagination aborted due to upstream failure")
            return all_hits, False

        hits_block = data.get("hits", {})
        hits = hits_block.get("hits", [])
        if total_known is None:
            total_known = hits_block.get("total", {}).get("value", 0)
            log.info(f"  total hits available for this query: {total_known}")

        log.info(f"  page returned {len(hits)} hits (offset={offset}, page={pages_fetched + 1})")
        all_hits.extend(hits)
        pages_fetched += 1

        if len(hits) < PAGINATION_PAGE_SIZE:
            log.info(f"  pagination complete (last page); pages={pages_fetched}, total_retrieved={len(all_hits)}")
            return all_hits, False

        offset += PAGINATION_PAGE_SIZE
        if offset < PAGINATION_MAX_HITS:
            time.sleep(SEC_RATE_LIMIT_SLEEP)

    cap_hit = (total_known or 0) > PAGINATION_MAX_HITS
    if cap_hit:
        log.warning(
            f"Pagination safety cap hit ({PAGINATION_MAX_HITS}); "
            f"total={total_known}, retrieved={len(all_hits)} -- consider narrower date range or SIC filter"
        )
    else:
        log.info(f"  pagination complete at cap boundary; pages={pages_fetched}, total_retrieved={len(all_hits)}")
    return all_hits, cap_hit


# ---------------------------------------------------------------------------
# Per-Item query
# ---------------------------------------------------------------------------

def fetch_item_hits(
    item: str,
    start_date: str,
    end_date: str,
    session: requests.Session,
    log: logging.Logger,
) -> tuple[list[dict], bool]:
    """Fetch all 8-K hits reporting `item` in [start_date, end_date].

    Uses `q="Item N.NN"` phrase query (NOT `items=` URL param) due to
    EDGAR-side caching bug; see module docstring. Post-filters response
    to keep only hits where the requested item is present in
    `_source.items` (eliminates phrase-match false positives where 8-K
    body text mentions "Item N.NN" referring to prior filings).
    """
    params = {
        "q": f'"Item {item}"',
        "forms": "8-K",
        "dateRange": "custom",
        "startdt": start_date,
        "enddt": end_date,
    }
    raw_hits, cap_hit = fetch_paginated_hits(params, session, log)
    # Post-filter: keep hits where item ∈ _source.items
    items_filtered: list[dict] = []
    false_positives = 0
    for hit in raw_hits:
        hit_items = hit.get("_source", {}).get("items", [])
        if item in hit_items:
            items_filtered.append(hit)
        else:
            false_positives += 1
    # Dedupe by accession (Component 1.1 fix): EDGAR FTS indexes the same
    # accession multiple times when a filing has multiple documents (8-K
    # body + exhibits each get their own `_id` like `{adsh}:{filename}`).
    # All same-adsh entries carry identical _source metadata (items,
    # display_names, file_date, sics) for our purposes, so safely keep
    # first occurrence and discard the rest.
    deduped: list[dict] = []
    seen_adsh: set[str] = set()
    dup_count = 0
    for hit in items_filtered:
        adsh = hit.get("_source", {}).get("adsh")
        if adsh and adsh in seen_adsh:
            dup_count += 1
            continue
        if adsh:
            seen_adsh.add(adsh)
        deduped.append(hit)
    log.info(
        f"  post-filter on _source.items: kept {len(items_filtered)} / {len(raw_hits)} "
        f"({false_positives} phrase-match false positives discarded); "
        f"dedup by adsh: kept {len(deduped)} / {len(items_filtered)} "
        f"({dup_count} duplicate-accession entries discarded)"
    )
    return deduped, cap_hit


# ---------------------------------------------------------------------------
# SIC filter (client-side from _source.sics; no HTTP)
# ---------------------------------------------------------------------------

def filter_by_sic(hits: list[dict], sic_set: set[str]) -> tuple[list[dict], int]:
    """Split hits into (matched, unmatched_count) based on _source.sics ∩ sic_set."""
    matched: list[dict] = []
    unmatched = 0
    for hit in hits:
        hit_sics = hit.get("_source", {}).get("sics", [])
        if not hit_sics:
            unmatched += 1
            continue
        if any(s in sic_set for s in hit_sics):
            matched.append(hit)
        else:
            unmatched += 1
    return matched, unmatched


# ---------------------------------------------------------------------------
# Body excerpt fetching (Tier 4 Lite Component 3)
# ---------------------------------------------------------------------------

# Regex for Item N.NN headers. Intentionally LOOSE (no word-boundary `\b`)
# because EDGAR 8-K front matter often renders checkbox chars (`o`, `☐`, `¨`)
# inline-adjacent to the next "Item" header, producing patterns like
# "oItem 1.01" after whitespace collapse. Verified across 3 filer agents
# (Toppan Merrill, ticker-date self-file) during plan-phase verification.
_ITEM_HEADER_RE = re.compile(r"Item\s+(\d+\.\d+)\.?\s*", re.IGNORECASE)

# Min content length to accept a candidate as the real Item section (not a
# cross-reference). 50 chars chosen empirically: cross-refs like "see Item
# 1.01" yield ~20-30 char sections; real Item content always exceeds this.
_MIN_SECTION_CONTENT = 50


def fetch_filing_index(
    cik: str, accession: str, session: requests.Session, log: logging.Logger,
) -> tuple[str | None, str | None]:
    """Fetch the filing-index HTML page and return (primary_8k_filename, error).
    On any failure, returns (None, error_reason_string)."""
    try:
        cik_int = str(int(cik))
    except (ValueError, TypeError):
        return None, f"invalid cik {cik!r}"
    accession_no_dashes = accession.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
        f"{accession_no_dashes}/{accession}-index.htm"
    )
    r = _http_get_with_retries(session, url, None, log)
    if r is None:
        return None, "index fetch failed"
    try:
        soup = BeautifulSoup(r.content, "lxml")
    except Exception as e:
        return None, f"index parse failed: {e}"

    # Find Document Format Files table: row with Type cell in the 8-K family.
    # Two-pass strategy per Component 3.1 fix (Week 1 BNZI 8-K/A failure case):
    #   Pass 1 (primary): exact "8-K" match. Covers original 8-K filings — verified
    #     across NVTS / ICHR / CRWV plan-phase samples and 16 of 17 working Week 1 cases.
    #   Pass 2 (fallback): any Type starting with "8-K" — covers amendments (8-K/A)
    #     and rare variants (8-K12B fiscal-year-change, 8-K12G3 registration-change,
    #     8-K15D5 termination). EDGAR FTS `forms=8-K` query returns these alongside
    #     original 8-Ks, but the index page Type cell preserves the variant suffix.
    #     Failure case 2026-05-23 Week 1: BNZI (Banzai International, accession
    #     0001193125-26-227353) Type cell = "8-K/A".
    # Explicit two-pass (vs single widened regex) preserves regression safety: any
    # filing with BOTH a "8-K" row AND an "8-K/A" row would still return the original
    # 8-K filename, not the amendment, because Pass 1 wins.
    def _extract_doc_from_row(cells: list[str]) -> str | None:
        doc_cell = cells[2]
        # Strip trailing " iXBRL" suffix added by index page (verified across 3 samples)
        doc_filename = re.sub(r"\s*iXBRL\s*$", "", doc_cell).strip()
        return doc_filename or None

    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) >= 5:
            rows.append(cells)

    # Pass 1: exact "8-K"
    for cells in rows:
        if cells[3].strip() == "8-K":
            doc_filename = _extract_doc_from_row(cells)
            if doc_filename:
                return doc_filename, None

    # Pass 2: 8-K family variants
    for cells in rows:
        if cells[3].strip().startswith("8-K"):
            doc_filename = _extract_doc_from_row(cells)
            if doc_filename:
                return doc_filename, None

    return None, "no row with Type=8-K family found in filing index"


def fetch_document_body(
    cik: str, accession: str, filename: str, session: requests.Session, log: logging.Logger,
) -> tuple[str | None, str | None]:
    """Fetch the primary 8-K document, return (cleaned_text, error).
    Cleaning pipeline: bs4 no-separator + whitespace collapse (handles
    `<b>I</b>tem` split issue verified on CRWV sample plan-phase)."""
    try:
        cik_int = str(int(cik))
    except (ValueError, TypeError):
        return None, f"invalid cik {cik!r}"
    accession_no_dashes = accession.replace("-", "")
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{filename}"
    r = _http_get_with_retries(session, url, None, log)
    if r is None:
        return None, "doc fetch failed"

    ctype = r.headers.get("Content-Type", "").lower()
    if "html" not in ctype and "xml" not in ctype:
        # XBRL inline docs typically report text/html; xml content-type is also
        # acceptable since lxml handles both. Hard reject only for binary/pdf.
        return None, f"non-HTML content-type {ctype!r}"

    try:
        soup = BeautifulSoup(r.content, "lxml")
    except Exception as e:
        return None, f"doc parse failed: {e}"
    for tag in soup(["script", "style"]):
        tag.decompose()
    raw_text = soup.get_text("", strip=False)
    raw_text = html.unescape(raw_text)
    cleaned = re.sub(r"\s+", " ", raw_text).strip()
    return cleaned, None


def truncate_excerpt(text: str, max_chars: int = 300) -> str:
    """Truncate text at max_chars OR first sentence boundary in [150, max_chars]
    (avoid cutting mid-clause). Append `...` if truncated."""
    if len(text) <= max_chars:
        return text
    search_start = min(150, max_chars)
    search_end = min(max_chars, len(text))
    for i in range(search_start, search_end):
        if text[i] in ".!?":
            # End-of-sentence: next char is whitespace or end-of-text
            if i + 1 >= len(text) or text[i + 1] in " \t\n":
                return text[: i + 1].rstrip() + "..."
    return text[:max_chars].rstrip() + "..."


def extract_item_section(clean_text: str, item_code: str, max_chars: int) -> str:
    """Extract the Item {item_code} section from the cleaned 8-K body text.
    Returns truncated excerpt or a `[bracketed error reason]` placeholder.

    Strategy: find all Item N.NN matches. For target code, take the FIRST
    match that yields a section with at least _MIN_SECTION_CONTENT chars
    before the next DIFFERENT-code Item header. Falls back to subsequent
    same-code matches if first is too short.

    Known limitation: if a cross-reference to {item_code} appears BEFORE
    the real Item {item_code} header in the document (rare but possible),
    first-match heuristic may extract cross-ref context. Detected via
    spot-check failure in EDGAR_MONITOR_CALIBRATION.md sample comparison;
    fix on observation.
    """
    if not clean_text:
        return "[empty document body]"
    all_matches = list(_ITEM_HEADER_RE.finditer(clean_text))
    if not all_matches:
        return f"[no Item N.NN headers found in body]"
    same_code_matches = [m for m in all_matches if m.group(1) == item_code]
    if not same_code_matches:
        return f"[item {item_code} header not found in body]"
    # Known limitation: if a cross-reference to {item_code} appears
    # BEFORE the real Item {item_code} header in the document (rare
    # but possible), first-match heuristic may extract cross-ref
    # context. Detected via spot-check failure in EDGAR_MONITOR_
    # CALIBRATION.md sample comparison; fix on observation.
    for candidate in same_code_matches:
        # Section spans from candidate.end() to start of next DIFFERENT-code Item
        section_end = len(clean_text)
        for m in all_matches:
            if m.start() <= candidate.start():
                continue
            if m.group(1) != item_code:
                section_end = m.start()
                break
        body = clean_text[candidate.end():section_end].strip()
        if len(body) >= _MIN_SECTION_CONTENT:
            return truncate_excerpt(body, max_chars)
    return "[empty section -- all matches were cross-references or trivial]"


def fetch_excerpts_for_hits(
    per_item_matched: dict[str, list[dict]],
    session: requests.Session,
    log: logging.Logger,
    max_chars: int,
) -> dict[str, dict[str, str]]:
    """Top-level orchestrator. Returns nested dict {adsh: {item_code: excerpt}}.

    Per-run body cache: multi-item 8-Ks (same accession across multiple Item
    queries) fetch the body once and reuse across all item extractions.
    """
    body_cache: dict[str, tuple[str | None, str | None]] = {}  # adsh -> (clean_text, error)
    excerpt_map: dict[str, dict[str, str]] = {}

    start_ts = time.time()
    n_accessions = 0
    n_extractions_ok = 0
    n_extractions_fail = 0
    n_fetch_failed = 0
    n_header_not_found = 0
    n_cache_hits = 0

    # Build deduped set of (adsh, item, cik, doc_filename_placeholder) work items
    work_items: list[tuple[str, str, str]] = []  # (adsh, item_code, cik)
    seen_pairs: set[tuple[str, str]] = set()
    for item, hits in per_item_matched.items():
        for hit in hits:
            src = hit.get("_source", {})
            adsh = src.get("adsh")
            ciks = src.get("ciks", [])
            cik = ciks[0] if ciks else ""
            if not adsh or not cik:
                continue
            pair = (adsh, item)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            work_items.append((adsh, item, cik))

    for adsh, item_code, cik in work_items:
        # Fetch body once per accession
        if adsh in body_cache:
            n_cache_hits += 1
            clean_text, body_err = body_cache[adsh]
        else:
            n_accessions += 1
            log.info(f"[excerpt] fetching {adsh} (cik={cik})")
            filename, idx_err = fetch_filing_index(cik, adsh, session, log)
            time.sleep(SEC_RATE_LIMIT_SLEEP)
            if filename is None:
                log.warning(f"[excerpt] {adsh}: index fetch failed ({idx_err})")
                body_cache[adsh] = (None, f"index: {idx_err}")
                clean_text, body_err = None, f"index: {idx_err}"
                n_fetch_failed += 1
            else:
                log.info(f"[excerpt] {adsh}: primary doc = {filename}")
                clean_text, body_err = fetch_document_body(cik, adsh, filename, session, log)
                time.sleep(SEC_RATE_LIMIT_SLEEP)
                body_cache[adsh] = (clean_text, body_err)
                if clean_text is None:
                    log.warning(f"[excerpt] {adsh}: body fetch failed ({body_err})")
                    n_fetch_failed += 1
                else:
                    log.info(f"[excerpt] {adsh}: body OK ({len(clean_text)} chars)")

        # Per-item extraction
        if clean_text is None:
            excerpt = f"[fetch failed: {body_err}]"
            n_extractions_fail += 1
        else:
            excerpt = extract_item_section(clean_text, item_code, max_chars)
            if excerpt.startswith("[item ") and "header not found" in excerpt:
                n_header_not_found += 1
            elif excerpt.startswith("["):
                n_extractions_fail += 1
            else:
                n_extractions_ok += 1

        excerpt_map.setdefault(adsh, {})[item_code] = excerpt
        # Log first 50 chars for spot-check visibility
        log_excerpt = excerpt[:50].replace("\n", " ")
        log.info(f"[excerpt] extract Item {item_code} from {adsh}: {log_excerpt}{'...' if len(excerpt) > 50 else ''}")

    elapsed = time.time() - start_ts
    log.info(
        f"[excerpt] phase complete: {n_accessions} bodies fetched, {n_cache_hits} reused, "
        f"{n_extractions_ok}/{len(work_items)} extractions OK, "
        f"{n_fetch_failed} fetch failures, {n_header_not_found} header-not-found, "
        f"{n_extractions_fail - n_header_not_found} other extraction failures, "
        f"wall_time={elapsed:.1f}s"
    )

    return excerpt_map


# ---------------------------------------------------------------------------
# Digest rendering
# ---------------------------------------------------------------------------

def render_digest(
    params: dict,
    per_item_matched: dict[str, list[dict]],
    per_item_unmatched: dict[str, int],
    per_item_cap_hit: dict[str, bool],
    item_descriptions: dict[str, str],
    excerpt_map: dict[str, dict[str, str]] | None = None,
) -> str:
    today_et = params["today_et"]
    items = params["items"]
    sic_set_sorted = sorted(params["sic_set"], key=lambda x: (len(x), x))

    # Unique accession count across all Item lists (multi-item 8-Ks counted once)
    unique_accessions: set[str] = set()
    for hit_list in per_item_matched.values():
        for hit in hit_list:
            adsh = hit.get("_source", {}).get("adsh")
            if adsh:
                unique_accessions.add(adsh)
    total_unique = len(unique_accessions)

    # Total unmatched across queries (may double-count multi-item 8-Ks that
    # are unmatched against SIC; we surface the count as-is per spec).
    total_unmatched = sum(per_item_unmatched.values())

    lines: list[str] = []
    lines.append(f"# EDGAR Monitor - {today_et} ET")
    lines.append("")
    lines.append(f"**Filter:** SIC codes {sic_set_sorted}, 8-K Items {items}")
    lines.append(f"**Date range:** {params['start_date']} to {params['end_date']} (US/Eastern)")
    lines.append(f"**Total unique accessions matched (within SIC filter):** {total_unique}")
    lines.append(f"**Unmatched SIC (count only, filtered out):** {total_unmatched}")
    lines.append(f"**Run timestamp:** {params['run_ts_utc']}")
    lines.append("")
    if total_unique == 0:
        lines.append("_No matching filings in this period._")
        lines.append("")

    for item in items:
        desc = item_descriptions.get(item, "(no description)")
        matched = per_item_matched.get(item, [])
        cap_hit = per_item_cap_hit.get(item, False)

        lines.append(f"## Item {item} - {desc} ({len(matched)})")
        lines.append("")

        if cap_hit:
            lines.append(
                f"> **Note:** Result set truncated at {PAGINATION_MAX_HITS} hits for Item {item}; "
                f"consider narrower date range or SIC filter."
            )
            lines.append("")

        if not matched:
            lines.append("_(no hits matching SIC filter)_")
            lines.append("")
            continue

        # Sort by file_date desc, then by adsh for deterministic ordering
        matched_sorted = sorted(
            matched,
            key=lambda h: (h["_source"].get("file_date", ""), h["_source"].get("adsh", "")),
            reverse=True,
        )

        for hit in matched_sorted:
            src = hit["_source"]
            adsh = src.get("adsh", "")
            file_date = src.get("file_date", "")
            ciks = src.get("ciks", [])
            cik = ciks[0] if ciks else ""
            sics = src.get("sics", [])
            sic = sics[0] if sics else "?"
            display_names = src.get("display_names", [])
            display_name = display_names[0] if display_names else ""
            all_items = src.get("items", [])

            ticker, company, _ = parse_display_name(display_name)
            ticker_display = ticker if ticker else "[no ticker]"
            url = build_filing_url(cik, adsh) if cik and adsh else "(URL unavailable)"
            items_str = ", ".join(all_items) if all_items else "(none)"

            lines.append(f"### {ticker_display} ({company}, SIC {sic}) - {file_date}")
            lines.append(f"- **Accession:** {adsh}")
            lines.append(f"- **All items in 8-K:** {items_str}")
            if excerpt_map is not None and adsh in excerpt_map:
                excerpt = excerpt_map[adsh].get(item, "[no excerpt available]")
                # Squash any leftover newlines/control whitespace so the line stays single-line
                excerpt_oneline = re.sub(r"\s+", " ", excerpt).strip()
                lines.append(f"- **Item {item} excerpt:** {excerpt_oneline}")
            lines.append(f"- **EDGAR:** {url}")
            lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="EDGAR 8-K Item monitor: SIC-filtered markdown digest of recent material agreements / officer changes / other events."
    )
    parser.add_argument(
        "--config", default="config/sic_filters.yaml",
        help="Path to SIC + Item config YAML (default: config/sic_filters.yaml)",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=None,
        help="Days back from today (US/Eastern). Overrides config.default_lookback_days. Ignored if --start-date/--end-date both given.",
    )
    parser.add_argument(
        "--start-date", default=None,
        help="YYYY-MM-DD (US/Eastern). Must be paired with --end-date.",
    )
    parser.add_argument(
        "--end-date", default=None,
        help="YYYY-MM-DD (US/Eastern). Must be paired with --start-date.",
    )
    parser.add_argument(
        "--items", default=None,
        help="Comma-separated Item codes, e.g., 1.01,5.02,8.01. Overrides config.items_to_monitor.",
    )
    parser.add_argument(
        "--sic", default=None,
        help="Comma-separated SIC codes, e.g., 3674,3812. Overrides config.default_sic_codes.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Override output digest path. Default: signals/edgar_{today_ET}.md",
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually call EDGAR API. Default: dry run (print query plan, no HTTP).",
    )
    parser.add_argument(
        "--with-excerpts", dest="with_excerpts", action="store_true", default=None,
        help="Enable 8-K body excerpt fetching. Overrides config.excerpt_fetch_enabled_default.",
    )
    parser.add_argument(
        "--no-excerpts", dest="with_excerpts", action="store_false",
        help="Disable body excerpt fetching (metadata-only digest, faster).",
    )
    args = parser.parse_args()

    config_path_arg = Path(args.config)
    config_path = config_path_arg if config_path_arg.is_absolute() else REPO_ROOT / config_path_arg
    config = load_config(config_path)
    params = resolve_effective_params(args, config)
    item_descriptions = config.get("item_descriptions", {})

    # Plan output
    def _rel(p: Path) -> str:
        try:
            return str(p.relative_to(REPO_ROOT))
        except ValueError:
            return str(p)

    print("=" * 72)
    print("EDGAR Monitor - plan")
    print("=" * 72)
    print(f"Config:       {_rel(config_path)}")
    print(f"Date range:   {params['start_date']} to {params['end_date']} (US/Eastern; today_ET={params['today_et']})")
    print(f"Items:        {params['items']}")
    print(f"SIC codes:    {sorted(params['sic_set'])}")
    print(f"Output path:  {_rel(params['output_path'])}")
    print(f"Pagination:   page_size={PAGINATION_PAGE_SIZE}, safety_cap={PAGINATION_MAX_HITS}")
    print(f"HTTP client:  vanilla requests (no curl_cffi for efts.sec.gov; matches fetch_sec_public_data.py canonical)")
    print(f"Query:        q=\"Item N.NN\" phrase + post-filter on _source.items (avoids items= URL param caching bug; see docstring)")
    print(f"SIC filter:   client-side from _source.sics (no separate CIK->SIC lookup)")
    print(f"Excerpts:     {'ON' if params['with_excerpts'] else 'OFF'} (max_chars={params['excerpt_max_chars']}; 2 HTTP calls per accession when ON)")
    print()
    print("Planned per-Item queries:")
    for item in params["items"]:
        print(
            f"  GET {EDGAR_FTS_URL}?q=%22Item+{item}%22&forms=8-K"
            f"&dateRange=custom&startdt={params['start_date']}&enddt={params['end_date']}"
        )
    print("=" * 72)

    if not args.execute:
        print("\n[dry run] No HTTP calls made. Re-run with --execute to fetch.")
        return 0

    print("\n[execute] Starting fetch...\n")

    log_dir = REPO_ROOT / "signals" / "cache"
    run_ts_compact = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    main_log, log_path = setup_logging(log_dir, run_ts_compact)
    main_log.info(f"==== edgar_monitor started ====")
    main_log.info(f"ET today={params['today_et']}, range={params['start_date']}..{params['end_date']}")
    main_log.info(f"Items={params['items']}, SIC set size={len(params['sic_set'])}")
    main_log.info(f"Output path: {params['output_path']}")

    session = requests.Session()

    per_item_matched: dict[str, list[dict]] = {}
    per_item_unmatched: dict[str, int] = {}
    per_item_cap_hit: dict[str, bool] = {}

    for item in params["items"]:
        main_log.info(f"---- Querying items={item} ----")
        hits, cap_hit = fetch_item_hits(item, params["start_date"], params["end_date"], session, main_log)
        matched, unmatched = filter_by_sic(hits, params["sic_set"])
        per_item_matched[item] = matched
        per_item_unmatched[item] = unmatched
        per_item_cap_hit[item] = cap_hit
        main_log.info(
            f"items={item}: {len(hits)} returned, {len(matched)} matched SIC, "
            f"{unmatched} unmatched, cap_hit={cap_hit}"
        )
        # Politeness sleep between Item queries
        time.sleep(SEC_RATE_LIMIT_SLEEP)

    # Excerpt phase (Component 3) — fetch bodies + extract per-Item sections
    excerpt_map: dict[str, dict[str, str]] = {}
    if params["with_excerpts"]:
        main_log.info("---- Body excerpt phase ----")
        excerpt_map = fetch_excerpts_for_hits(
            per_item_matched=per_item_matched,
            session=session,
            log=main_log,
            max_chars=params["excerpt_max_chars"],
        )
    else:
        main_log.info("Excerpt phase SKIPPED (--no-excerpts / config default disabled)")

    digest = render_digest(
        params=params,
        per_item_matched=per_item_matched,
        per_item_unmatched=per_item_unmatched,
        per_item_cap_hit=per_item_cap_hit,
        item_descriptions=item_descriptions,
        excerpt_map=excerpt_map if params["with_excerpts"] else None,
    )
    params["output_path"].parent.mkdir(parents=True, exist_ok=True)
    params["output_path"].write_text(digest, encoding="utf-8")
    main_log.info(f"Digest written: {params['output_path']}")
    main_log.info("==== edgar_monitor complete ====")

    print()
    print(f"Done.")
    print(f"  Digest: {_rel(params['output_path'])}")
    print(f"  Log:    {_rel(log_path)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
