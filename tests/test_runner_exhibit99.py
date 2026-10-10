#!/usr/bin/env python3
"""
test_runner_exhibit99.py - Phase 1A Tier 6.3 exhibit capture (TASKBOARD B3).

Proves the EX-99 locator is TYPE-COLUMN based (not filename-pattern based)
WITHOUT any network: drives run_weekly_scan.extract_exhibit99_entries on
synthetic filing-index HTML shaped like the real EDGAR Document Format Files
table (Seq | Description | Document | Type | Size).

Why type-column matching (R8 lesson): fetch_sec_public_data.py's ALAB-calibrated
filename EXHIBIT_PATTERNS pulled 0 exhibits for the BLZE 8-K (accession
0001628280-26-044804 — cases/2026-07-BLZE/inputs/fetch.log: "no exhibits
matched") because its press release was named 'ex991backblazecoreweavepre.htm':
words after the exhibit number defeat the `...\\.htm$` filename anchors. The
index Type cell said 'EX-99.1' all along. This test asserts:

  E1  the exact BLZE-style filename the filename matcher missed IS located via
      the Type column;
  E2  a trailing ' iXBRL' suffix on the Document cell is stripped
      (same rule as edgar_monitor.fetch_filing_index);
  E3  multiple EX-99.* rows are all returned, in table order, while non-EX-99
      rows (primary 8-K, EX-10.1, GRAPHIC) are excluded — EX-99 is a TYPE
      PREFIX match (EX-99.1, EX-99.2, ... all qualify);
  E4  an index with no EX-99 row yields an empty list (runner then keeps
      exhibit_99_text NULL + notes record; triage unchanged).

Run standalone:  python tests/test_runner_exhibit99.py   (exit 0 = pass)
Run via pytest:  pytest tests/test_runner_exhibit99.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from run_weekly_scan import extract_exhibit99_entries  # noqa: E402


def _index_html(rows: list[tuple[str, str, str, str, str]]) -> str:
    """Build a synthetic EDGAR filing-index page. Each row is
    (seq, description, document_cell_html, type, size)."""
    trs = "\n".join(
        f"<tr><td>{seq}</td><td>{desc}</td><td>{doc}</td><td>{ftype}</td><td>{size}</td></tr>"
        for seq, desc, doc, ftype, size in rows
    )
    return f"""<html><body>
<table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
{trs}
</table>
</body></html>"""


def run_checks() -> list[str]:
    failures: list[str] = []

    # --- E1: BLZE-style filename located via Type column ---
    # 'ex991backblazecoreweavepre.htm' matches NONE of fetch_sec_public_data.py's
    # EXHIBIT_PATTERNS (letters between the exhibit number and '.htm'); the Type
    # cell is what identifies it.
    html1 = _index_html([
        ("1", "8-K", '<a href="blze-20260623.htm">blze-20260623.htm</a> iXBRL', "8-K", "123456"),
        ("2", "PRESS RELEASE",
         '<a href="ex991backblazecoreweavepre.htm">ex991backblazecoreweavepre.htm</a>',
         "EX-99.1", "54321"),
    ])
    got1 = extract_exhibit99_entries(html1)
    if got1 != [("EX-99.1", "ex991backblazecoreweavepre.htm")]:
        failures.append(f"E1: expected the BLZE-style EX-99.1 via Type column, got {got1!r}")

    # --- E2: trailing ' iXBRL' suffix stripped from the Document cell ---
    html2 = _index_html([
        ("1", "8-K", '<a href="primary.htm">primary.htm</a>', "8-K", "1000"),
        ("2", "EARNINGS RELEASE", '<a href="ex99x1.htm">ex99x1.htm</a> iXBRL', "EX-99.1", "2000"),
    ])
    got2 = extract_exhibit99_entries(html2)
    if got2 != [("EX-99.1", "ex99x1.htm")]:
        failures.append(f"E2: expected iXBRL suffix stripped -> 'ex99x1.htm', got {got2!r}")

    # --- E3: EX-99 prefix match, multiple docs in order, non-EX-99 excluded ---
    html3 = _index_html([
        ("1", "8-K", '<a href="body.htm">body.htm</a> iXBRL', "8-K", "1000"),
        ("2", "MASTER AGREEMENT", '<a href="ex101msa.htm">ex101msa.htm</a>', "EX-10.1", "9000"),
        ("3", "PRESS RELEASE", '<a href="pressrelease2026.htm">pressrelease2026.htm</a>', "EX-99.1", "2000"),
        ("4", "INVESTOR PRESENTATION", '<a href="deck.htm">deck.htm</a>', "EX-99.2", "3000"),
        ("5", "LOGO", '<a href="img01.jpg">img01.jpg</a>', "GRAPHIC", "500"),
    ])
    got3 = extract_exhibit99_entries(html3)
    if got3 != [("EX-99.1", "pressrelease2026.htm"), ("EX-99.2", "deck.htm")]:
        failures.append(f"E3: expected both EX-99.* rows in order (and only those), got {got3!r}")

    # --- E4: no EX-99 row -> empty list (exhibit_99_text stays NULL) ---
    html4 = _index_html([
        ("1", "8-K", '<a href="body.htm">body.htm</a>', "8-K", "1000"),
        ("2", "CREDIT AGREEMENT", '<a href="ex101credit.htm">ex101credit.htm</a>', "EX-10.1", "9000"),
    ])
    got4 = extract_exhibit99_entries(html4)
    if got4 != []:
        failures.append(f"E4: expected empty list for a no-EX-99 filing, got {got4!r}")

    return failures


# ---- pytest entrypoint ----
def test_exhibit99_type_column_locator():
    failures = run_checks()
    assert not failures, "B3 exhibit-99 locator failures:\n" + "\n".join(failures)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    print("B3 — EX-99 locator, Type-column match (synthetic index HTML, no network):")
    failures = run_checks()
    if failures:
        print("FAIL:")
        for x in failures:
            print("  -", x)
        return 1
    print("  [OK] E1 BLZE-style filename via Type  E2 iXBRL strip  E3 prefix+order+exclusion  E4 no-EX-99 empty")
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
