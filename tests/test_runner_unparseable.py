#!/usr/bin/env python3
"""
test_runner_unparseable.py - Phase 1A Tier 6.3 hardening (TASKBOARD B6).

Proves the meta-unparseable persistence path is correct WITHOUT any network or
touching the committed db: builds a throwaway db from data/funnel_schema.sql,
then drives run_weekly_scan.record_unparseable_hit directly.

B6 fixes the silent-drop failure mode: SIC-matched 8-Ks whose FTS metadata cannot
be parsed into a (ticker, CIK) pair used to be dropped (only a weekly_scans.notes
count survived; the accession was unrecoverable — recurred 3x in the B2 backfill,
W23/W24/W25). This test asserts:

  U1  a fully-unparseable hit is persisted and the accession + all available raw
      metadata (incl. the full FTS _source JSON) are recoverable;
  U2  INSERT OR IGNORE is idempotent by accession (overlapping rescan chunks
      never duplicate rows);
  U3  a partially-parseable hit (CIK present, ticker missing) preserves whatever
      WAS parsed;
  U4  the side table is isolated — raw_hits / triage_decisions stay empty, so
      sic_match_count / triaged_count / candidate_count are never inflated.

Run standalone:  python tests/test_runner_unparseable.py   (exit 0 = pass)
Run via pytest:  pytest tests/test_runner_unparseable.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from run_weekly_scan import record_unparseable_hit  # noqa: E402

SCHEMA = REPO_ROOT / "data" / "funnel_schema.sql"


def _fresh_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


def run_checks() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        conn = _fresh_db(Path(td) / "tmp_funnel.db")

        # --- U1: fully-unparseable hit (no ticker, no CIK) is recoverable ---
        adsh1 = "0009999999-26-000001"
        src1 = {
            "adsh": adsh1,
            "display_names": ["SOME OBSCURE TRUST SERIES 2026-A"],  # no (TICKER), no (CIK ...)
            "ciks": [],
            "sics": ["6221"],
            "items": ["1.01"],
            "file_date": "2026-06-03",
            "file_type": "8-K",
        }
        reason1 = "unparseable FTS metadata: ticker=None, cik=None"
        newly = record_unparseable_hit(conn, "2026-W23", "2026-07-13", adsh1, "1.01", src1, reason1)
        conn.commit()
        if not newly:
            failures.append("U1: first insert should return True (newly inserted)")
        row = conn.execute(
            "SELECT scan_week, scan_date, accession_number, trigger_item, ticker, cik, "
            "display_name, sic_code, filing_type, filed_date, items, raw_source_json, reason "
            "FROM meta_unparseable_hits WHERE accession_number = ?",
            (adsh1,),
        ).fetchone()
        if row is None:
            failures.append("U1: row not persisted")
        else:
            (sw, sd, acc, ti, tk, ck, dn, sic, ft, fd, items, rsj, rsn) = row
            if acc != adsh1:
                failures.append(f"U1: accession not recoverable ({acc!r})")
            if tk is not None or ck is not None:
                failures.append(f"U1: expected ticker/cik NULL, got ({tk!r}, {ck!r})")
            if dn != "SOME OBSCURE TRUST SERIES 2026-A":
                failures.append(f"U1: display_name not preserved ({dn!r})")
            if sic != "6221" or ft != "8-K" or fd != "2026-06-03":
                failures.append(f"U1: metadata not preserved (sic={sic!r} ft={ft!r} fd={fd!r})")
            if json.loads(items) != ["1.01"]:
                failures.append(f"U1: items not preserved ({items!r})")
            if json.loads(rsj) != src1:
                failures.append("U1: raw_source_json does not round-trip (full recoverability lost)")
            if rsn != reason1 or sw != "2026-W23" or sd != "2026-07-13" or ti != "1.01":
                failures.append("U1: scan_week/scan_date/trigger_item/reason not preserved")

        # --- U2: idempotent by accession (rescan chunk) ---
        again = record_unparseable_hit(conn, "2026-W23", "2026-07-13", adsh1, "1.01", src1, reason1)
        conn.commit()
        if again:
            failures.append("U2: second insert of same accession should return False (idempotent)")
        cnt = conn.execute(
            "SELECT COUNT(*) FROM meta_unparseable_hits WHERE accession_number = ?", (adsh1,)
        ).fetchone()[0]
        if cnt != 1:
            failures.append(f"U2: expected exactly 1 row after re-insert, got {cnt}")

        # --- U3: partial parse (CIK present, ticker missing) preserves the CIK ---
        adsh2 = "0009999999-26-000002"
        src2 = {
            "adsh": adsh2,
            "display_names": ["FOREIGN FILER LTD  (CIK 0001234567)"],  # CIK parses; no ticker
            "ciks": ["0001234567"],
            "sics": ["7372"],
            "items": ["1.01"],
            "file_date": "2026-06-04",
            "file_type": "8-K",
        }
        record_unparseable_hit(conn, "2026-W23", "2026-07-13", adsh2, "1.01", src2,
                               "unparseable FTS metadata: ticker=None, cik='0001234567'")
        conn.commit()
        tk2, ck2 = conn.execute(
            "SELECT ticker, cik FROM meta_unparseable_hits WHERE accession_number = ?", (adsh2,)
        ).fetchone()
        if tk2 is not None:
            failures.append(f"U3: expected ticker NULL, got {tk2!r}")
        if ck2 != "0001234567":
            failures.append(f"U3: expected recovered cik '0001234567', got {ck2!r}")

        # --- U4: isolation — the main triage tables are never touched ---
        for t in ("raw_hits", "triage_decisions"):
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if n != 0:
                failures.append(f"U4: {t} should stay empty (isolation), got {n} rows")
        total = conn.execute("SELECT COUNT(*) FROM meta_unparseable_hits").fetchone()[0]
        if total != 2:
            failures.append(f"U4: expected 2 side-table rows, got {total}")

        conn.close()
    return failures


# ---- pytest entrypoint ----
def test_meta_unparseable_persistence():
    failures = run_checks()
    assert not failures, "B6 meta-unparseable failures:\n" + "\n".join(failures)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    print("B6 — meta-unparseable persistence (temp db, no network):")
    failures = run_checks()
    if failures:
        print("FAIL:")
        for x in failures:
            print("  -", x)
        return 1
    print("  [OK] U1 recoverable  U2 idempotent  U3 partial-parse preserved  U4 isolation")
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
