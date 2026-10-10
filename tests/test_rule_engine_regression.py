#!/usr/bin/env python3
"""
test_rule_engine_regression.py - Phase 1A Tier 6.2 — hard regression contract.

R1: the deterministic rule engine MUST reproduce the committed Week-1 golden
    (`triage_decisions` (accession -> (is_candidate, rejection_label))) for all 18
    hits, classifying from the committed body fixtures under
    tests/fixtures/week1_bodies/. `cap_resolved` is NOT asserted (all 18 are
    content-rejected at the marker stage, so cap-band is irrelevant to them).

R5: the cap-band stage is reachable on all three branches for a synthetic survivor
    (an excerpt that matches no content marker): sub-$5B -> candidate;
    >= $5B -> oversized_marker; cap unavailable -> candidate + cap_resolved=0.

Golden source is the DB itself (NOT re-imported from the migrate dict), so a drift
in the migrate dict can never silently "fix" the test.

Run standalone:  python tests/test_rule_engine_regression.py   (exit 0 = pass)
Run via pytest:  pytest tests/test_rule_engine_regression.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from rule_engine import classify, OVERSIZED_LABEL, CAP_BAND_MAX_MM  # noqa: E402

DB_PATH = REPO_ROOT / "data" / "funnel_log.db"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "week1_bodies"


def _load_golden() -> dict[str, tuple[int, str | None]]:
    # Scoped to the Week-1 baseline (scan_week 2026-W21): the golden contract is
    # the 18 committed hand-triage labels. Tier 6.3's run_weekly_scan.py appends
    # later weeks to the same db (triage_method='rule_engine_v1'); those rows are
    # engine OUTPUT, not golden, and must not enter this query (an unscoped join
    # would also break the len==18 assertion on the first post-W21 ingest).
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        """
        SELECT r.accession_number, r.ticker, t.is_candidate, t.rejection_label
        FROM raw_hits r JOIN triage_decisions t
          ON r.accession_number = t.accession_number
        WHERE r.scan_week = '2026-W21'
        """
    ).fetchall()
    conn.close()
    return {acc: (tk, int(isc), lbl) for acc, tk, isc, lbl in rows}


def run_r1() -> list[str]:
    """Returns a list of failure strings (empty == 18/18 pass)."""
    golden = _load_golden()
    assert len(golden) == 18, f"expected 18 golden rows, got {len(golden)}"
    failures: list[str] = []
    for acc, (ticker, exp_is_cand, exp_label) in sorted(golden.items(), key=lambda x: (x[1][2] or "", x[1][0])):
        fixture = FIXTURES / f"{acc}.txt"
        if not fixture.exists():
            failures.append(f"{ticker} ({acc}): missing fixture {fixture}")
            continue
        body = fixture.read_text(encoding="utf-8")
        res = classify(body, market_cap_millions=None)
        got = (res.is_candidate, res.rejection_label)
        exp = (exp_is_cand, exp_label)
        status = "OK  " if got == exp else "FAIL"
        print(f"  [{status}] {ticker:<6} {acc}  exp={exp}  got={got}")
        if got != exp:
            failures.append(f"{ticker} ({acc}): expected {exp}, got {got}")
    return failures


def run_r5() -> list[str]:
    """Synthetic survivor through all three cap-band branches."""
    # An excerpt that deliberately matches NO content marker (a partnership-style
    # collaboration — exactly the survivor class the engine must NOT reject on content).
    survivor = (
        "Item 1.01 Entry into a Material Definitive Agreement. On June 1, 2026, the "
        "Company entered into a strategic collaboration and co-development arrangement "
        "with a leading hyperscaler to jointly develop custom AI accelerator silicon, "
        "which the customer will deploy at production scale across its data centers."
    )
    failures: list[str] = []

    # Branch 1: sub-$5B -> candidate
    r1 = classify(survivor, market_cap_millions=1500.0)
    print(f"  [R5.a sub-$5B ] is_candidate={r1.is_candidate} label={r1.rejection_label} cap_resolved={r1.cap_resolved}")
    if not (r1.is_candidate == 1 and r1.rejection_label is None and r1.cap_resolved == 1):
        failures.append(f"R5.a sub-$5B: expected (1, None, 1), got ({r1.is_candidate}, {r1.rejection_label}, {r1.cap_resolved})")

    # Branch 2: >= $5B -> oversized_marker
    r2 = classify(survivor, market_cap_millions=CAP_BAND_MAX_MM + 1.0)
    print(f"  [R5.b >=$5B  ] is_candidate={r2.is_candidate} label={r2.rejection_label} cap_resolved={r2.cap_resolved}")
    if not (r2.is_candidate == 0 and r2.rejection_label == OVERSIZED_LABEL and r2.cap_resolved == 1):
        failures.append(f"R5.b >=$5B: expected (0, {OVERSIZED_LABEL}, 1), got ({r2.is_candidate}, {r2.rejection_label}, {r2.cap_resolved})")

    # Branch 3: cap unavailable -> candidate + cap_resolved=0 + warning
    r3 = classify(survivor, market_cap_millions=None)
    print(f"  [R5.c no-cap ] is_candidate={r3.is_candidate} label={r3.rejection_label} cap_resolved={r3.cap_resolved} warnings={len(r3.warnings)}")
    if not (r3.is_candidate == 1 and r3.rejection_label is None and r3.cap_resolved == 0 and r3.warnings):
        failures.append(f"R5.c no-cap: expected (1, None, 0, warn), got ({r3.is_candidate}, {r3.rejection_label}, {r3.cap_resolved}, {len(r3.warnings)})")

    return failures


# ---- pytest entrypoints ----
def test_r1_eighteen_label_regression():
    failures = run_r1()
    assert not failures, "R1 regression failures:\n" + "\n".join(failures)


def test_r5_capband_three_branches():
    failures = run_r5()
    assert not failures, "R5 cap-band failures:\n" + "\n".join(failures)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    print("R1 — 18-label regression vs committed golden:")
    f1 = run_r1()
    print(f"\nR1: {18 - len(f1)}/18 match golden" + (" — PASS" if not f1 else " — FAIL"))
    print("\nR5 — synthetic survivor cap-band three branches:")
    f5 = run_r5()
    print("R5: " + ("3/3 branches reachable — PASS" if not f5 else "FAIL"))
    if f1 or f5:
        print("\nFAILURES:")
        for x in (*f1, *f5):
            print("  -", x)
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
