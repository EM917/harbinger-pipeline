#!/usr/bin/env python3
"""
fetch_market_cap.py - Phase 1A Tier 6.2 — market-cap fetch for the cap-band filter.

Source: yfinance ONLY. ALAB / the monitor must NOT take any runtime dependency on
moomoo / OpenD or any broker gateway (Tier 6.2 §4). Results are cached to
`data/market_cap_cache.json` (gitignored) with a fetch timestamp, and written back
to `raw_hits.market_cap_millions` so the funnel log is self-describing.

Cap-band precedence note: content markers run BEFORE cap-band, so for the 18 Week-1
baseline hits (all content-rejected) the backfilled market cap does NOT change any
label — it is purely observability. The regression (R1) is therefore independent of
market-cap backfill.

CLI:
    python scripts/fetch_market_cap.py --from-db            # backfill all raw_hits tickers
    python scripts/fetch_market_cap.py --tickers ALAB,NVDA  # ad-hoc
    python scripts/fetch_market_cap.py --from-db --refresh  # ignore cache, re-fetch

Programmatic:
    from fetch_market_cap import get_market_cap_mm
    cap_mm = get_market_cap_mm("ALAB", cache)   # float | None
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "funnel_log.db"
DEFAULT_CACHE = REPO_ROOT / "data" / "market_cap_cache.json"


def _load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def _fetch_yf_market_cap_mm(ticker: str) -> tuple[float | None, str]:
    """Return (market_cap_millions | None, source_str). yfinance only."""
    try:
        import yfinance as yf
    except ImportError:
        return None, "yfinance-not-installed"
    # Prefer fast_info (lightweight); fall back to .info.
    try:
        fi = yf.Ticker(ticker).fast_info
        mc = None
        try:
            mc = fi.get("market_cap")  # dict-like in recent yfinance
        except AttributeError:
            mc = getattr(fi, "market_cap", None)
        if mc:
            return float(mc) / 1e6, "yfinance.fast_info.market_cap"
    except Exception:
        pass
    try:
        info = yf.Ticker(ticker).info
        mc = info.get("marketCap")
        if mc:
            return float(mc) / 1e6, "yfinance.info.marketCap"
    except Exception:
        pass
    return None, "yfinance-unavailable"


def get_market_cap_mm(
    ticker: str, cache: dict, refresh: bool = False, log=print
) -> float | None:
    """Cached market-cap lookup ($MM). Updates `cache` in place; caller persists."""
    key = ticker.upper().strip()
    if not refresh and key in cache and cache[key].get("market_cap_millions") is not None:
        return cache[key]["market_cap_millions"]
    cap_mm, source = _fetch_yf_market_cap_mm(key)
    cache[key] = {
        "market_cap_millions": cap_mm,
        "source": source,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if cap_mm is None:
        log(f"WARNING: market cap unavailable for {key} (source={source})")
    return cap_mm


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    ap = argparse.ArgumentParser(description="Fetch market caps via yfinance (cap-band filter).")
    ap.add_argument("--db-path", default=str(DEFAULT_DB))
    ap.add_argument("--cache-path", default=str(DEFAULT_CACHE))
    ap.add_argument("--from-db", action="store_true",
                    help="Fetch caps for every distinct ticker in raw_hits and write them back.")
    ap.add_argument("--tickers", default=None, help="Comma-separated tickers (ad-hoc; no writeback).")
    ap.add_argument("--refresh", action="store_true", help="Ignore cache; re-fetch.")
    args = ap.parse_args()

    db_path = Path(args.db_path).resolve()
    cache_path = Path(args.cache_path).resolve()
    cache = _load_cache(cache_path)

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        for t in tickers:
            cap = get_market_cap_mm(t, cache, refresh=args.refresh)
            print(f"{t}: {cap if cap is not None else 'N/A'} $MM")
        _save_cache(cache_path, cache)
        return 0

    if args.from_db:
        if not db_path.exists():
            print(f"ERROR: db not found: {db_path}", file=sys.stderr)
            return 1
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT DISTINCT ticker FROM raw_hits ORDER BY ticker").fetchall()
        n_ok = 0
        n_null = 0
        for (ticker,) in rows:
            cap = get_market_cap_mm(ticker, cache, refresh=args.refresh)
            if cap is None:
                n_null += 1
            else:
                n_ok += 1
                conn.execute(
                    "UPDATE raw_hits SET market_cap_millions=? WHERE ticker=?",
                    (cap, ticker),
                )
            print(f"{ticker}: {cap if cap is not None else 'N/A (left NULL)'} $MM")
        conn.commit()
        conn.close()
        _save_cache(cache_path, cache)
        print(f"\nBackfill complete: {n_ok} caps written, {n_null} left NULL (yfinance unavailable).")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
