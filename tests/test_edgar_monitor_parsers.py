#!/usr/bin/env python3
"""
test_edgar_monitor_parsers.py - TASKBOARD B5 — fixture regression contract for
edgar_monitor.py's two load-bearing parsing heuristics.

Purpose is REGRESSION PROTECTION of current behavior, NOT bug-fixing. Both
documented Known limitations are pinned AS-IS and marked expected:
  - parse_display_name: semicolon-separated ticker pairings "(TICK1; TICK2)"
    are not parsed (ticker=None, parenthetical retained in company).
  - extract_item_section: a cross-reference to the target item code appearing
    BEFORE the real Item header is extracted as if it were the section
    ("cross-ref-before-header", fix-on-observation per TASKBOARD pool).
Fixing either limitation SHOULD fail this test — update the pinned expectation
together with the fix and its RULES.md/CALIBRATION.md evidence.

P-series — parse_display_name (pure function, representative EDGAR
display_names strings from the function's own docstring + observed formats):
  P1 single ticker; P2 comma pairing "(SKIL, SKILW)" -> first (primary) ticker
  (Component 1.1 fix); P3 no-ticker filer; P4 semicolon pairing = pinned known
  limitation; P5 unparseable input passthrough; P6 lowercase parenthetical is
  not a ticker.

E-series — extract_item_section against the committed real 8-K bodies under
tests/fixtures/week1_bodies/ (read-only; NO network):
  E1 BNZI (0001193125-26-227353) Item 1.01 early truncation: at max_chars=300
     the sentence-boundary heuristic cuts at 184 chars ("...Demio, Inc....")
     — the loan / convertible-note substance sits ~33 chars past the cut.
     This is the exact display-truncation that fed the Tier 2.6 ma_marker
     misclassification (Tier 6.2 P1 finding, EDGAR_MONITOR_CALIBRATION.md).
     Pinned as expected: the excerpt is a DISPLAY artifact; the classification
     fix is run_weekly_scan.py's full-body rule, not a truncation change.
  E2 RMXI / E3 SITM: cross-ref-before-header known limitation, live in the
     committed corpus (front-matter "under Item 1.01, Item 3.02, and Item
     9.01" / "Item 8.01 below" match before the real headers).
  E4 happy-path extraction sanity (NVTS 1.01, BNZI 9.01).
  E5 header-not-found placeholder on a real body (BNZI has no Item 8.01).
  E6 GSIT-type cross-reference EMPTY section. GSIT's own accession
     (0001104659-26-065370, items 2.02+8.01 — CALIBRATION.md "Known Component
     3 issue") is NOT in the committed corpus (week1_bodies = the 18 Item 1.01
     hits) and B5 forbids fetching new fixtures. Exhaustive probe of all 18
     committed bodies x every item code present found NO naturally occurring
     "[empty section]" return, so the GSIT shape (Item 8.01 = short cross-ref
     to Item 2.02's content) is reproduced synthetically — same precedent as
     test_rule_engine_regression.py R5's synthetic survivor.
  E7 degenerate inputs (empty body / no Item headers).

Run standalone:  python tests/test_edgar_monitor_parsers.py   (exit 0 = pass)
Run via pytest:  pytest tests/test_edgar_monitor_parsers.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from edgar_monitor import parse_display_name, extract_item_section  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "week1_bodies"

BNZI = "0001193125-26-227353"  # Banzai International — Tier 6.2 P1 case
RMXI = "0001213900-26-058408"  # RMX Industries — cross-ref-before-header (9.01)
SITM = "0001193125-26-237180"  # SiTime — cross-ref-before-header (8.01)
NVTS = "0001104659-26-065731"  # Navitas — clean single-1.01 happy path


def _body(accession: str) -> str:
    return (FIXTURES / f"{accession}.txt").read_text(encoding="utf-8")


def _check(failures: list[str], case_id: str, ok: bool, detail: str) -> None:
    status = "OK  " if ok else "FAIL"
    print(f"  [{status}] {case_id}: {detail}")
    if not ok:
        failures.append(f"{case_id}: {detail}")


# ---------------------------------------------------------------------------
# P-series: parse_display_name
# ---------------------------------------------------------------------------

def run_display_name() -> list[str]:
    failures: list[str] = []
    # (case_id, input, expected (ticker, company, cik), note)
    cases = [
        (
            "P1 single-ticker",
            "REALLOYS INC.  (ALOY)  (CIK 0001567900)",
            ("ALOY", "REALLOYS INC.", "0001567900"),
            "docstring example",
        ),
        (
            "P2 comma-pair",
            "Skillsoft Corp.  (SKIL, SKILW)  (CIK 0001774675)",
            ("SKIL", "Skillsoft Corp.", "0001774675"),
            "Component 1.1: FIRST (primary common-share) ticker wins",
        ),
        (
            "P3 no-ticker",
            "CNL Strategic Residential Credit, Inc.  (CIK 0002066337)",
            (None, "CNL Strategic Residential Credit, Inc.", "0002066337"),
            "trust/foreign-filer format",
        ),
        (
            # KNOWN LIMITATION (expected, do not fix here): semicolon-separated
            # pairings are not parsed. Pinned actual behavior is slightly more
            # specific than the source comment ("not handled"): ticker comes
            # back None AND the unconsumed parenthetical stays inside the
            # company string. Not observed in the EDGAR sample as of
            # 2026-05-23; on first live observation, fix goes to the
            # fix-on-observation pool and this pin gets updated with it.
            "P4 semicolon-pair KNOWN-LIMITATION",
            "Some Trust Co.  (TICK1; TICK2)  (CIK 0001234567)",
            (None, "Some Trust Co.  (TICK1; TICK2)", "0001234567"),
            "expected: ticker=None, parenthetical retained in company",
        ),
        (
            "P5 unparseable-passthrough",
            "garbage no parens at all",
            (None, "garbage no parens at all", None),
            "docstring contract: original string back, no crash",
        ),
        (
            "P6 lowercase-not-a-ticker",
            "Lowercase ticker (abc) (CIK 0000000001)",
            (None, "Lowercase ticker (abc)", "0000000001"),
            "ticker regex is uppercase-anchored",
        ),
    ]
    for case_id, raw, expected, note in cases:
        got = parse_display_name(raw)
        _check(failures, case_id, got == expected,
               f"{note} | exp={ascii(str(expected))} got={ascii(str(got))}")
    return failures


# ---------------------------------------------------------------------------
# E-series: extract_item_section
# ---------------------------------------------------------------------------

# E1 pin — the exact 300-char-budget excerpt for BNZI Item 1.01. The sentence-
# boundary heuristic in truncate_excerpt cuts at the "Inc." after "Demio,"
# (184 chars), well short of max_chars. Everything after the cut — the loan
# agreement with CP BF Lending and the First Senior Convertible Note — is what
# Tier 2.6 never saw when it mislabeled BNZI ma_marker (corrected to
# debt_marker in Tier 6.2 P1). Expected/pinned display behavior.
BNZI_101_EXCERPT_300 = (
    "Entry into a Material Definitive Agreement.Banzai International, Inc. "
    "(the “Company”) previously reported that on February 19, 2021, "
    "the Company, along with Joe Davy and Demio, Inc...."
)

EMPTY_SECTION_PLACEHOLDER = (
    "[empty section -- all matches were cross-references or trivial]"
)

# E6 synthetic GSIT-shaped body (see module docstring for why synthetic):
# Item 8.01 is a short cross-reference into Item 2.02, and the "Item 2.02"
# inside the cross-ref sentence itself matches _ITEM_HEADER_RE, terminating
# the 8.01 candidate section at < _MIN_SECTION_CONTENT chars -> placeholder.
GSIT_SHAPED_BODY = (
    "Item 2.02 Results of Operations and Financial Condition. On May 20, "
    "2026, GSI Technology, Inc. issued a press release announcing its "
    "financial results for the fourth quarter and fiscal year. The full text "
    "of the press release is furnished as Exhibit 99.1 to this Current "
    "Report. Item 8.01 Other Events. The information set forth in Item 2.02 "
    "above is incorporated by reference. Item 9.01 Financial Statements and "
    "Exhibits. (d) Exhibits. 99.1 Press release."
)


def run_extract() -> list[str]:
    failures: list[str] = []

    # --- E1: BNZI early truncation (Tier 6.2 P1 mechanism) ---
    bnzi = _body(BNZI)
    excerpt = extract_item_section(bnzi, "1.01", 300)
    _check(failures, "E1a BNZI-1.01-exact-excerpt",
           excerpt == BNZI_101_EXCERPT_300,
           f"exp 184-char sentence-boundary cut, got len={len(excerpt)} "
           f"{ascii(excerpt[:80])}...")
    _check(failures, "E1b BNZI-excerpt-hides-debt",
           "loan" not in excerpt.lower() and "note" not in excerpt.lower(),
           "loan/note substance must sit PAST the truncation cut (P1 lesson)")
    full = extract_item_section(bnzi, "1.01", 100_000)
    _check(failures, "E1c BNZI-full-section-has-debt",
           len(full) == 4313
           and "loan agreement" in full.lower()
           and "convertible promissory note" in full.lower(),
           f"full untruncated section len={len(full)} (exp 4313) must contain "
           f"the loan + convertible-note language")

    # --- E2: RMXI 9.01 cross-ref-before-header (KNOWN LIMITATION, expected) ---
    # RMXI's front matter reads "...on Form 8-K under Item 1.01, Item 3.02,
    # and Item 9.01 notwithstanding..." — that in-prose "Item 9.01" matches
    # first and yields a >= 50-char section (front-matter prose), so the
    # first-match heuristic returns it INSTEAD of the real Item 9.01
    # (Financial Statements and Exhibits) near the end of the document.
    # This is the documented Known limitation manifesting in a real committed
    # body; fix-on-observation, pin as expected.
    rmxi_901 = extract_item_section(_body(RMXI), "9.01", 300)
    _check(failures, "E2 RMXI-9.01-crossref KNOWN-LIMITATION",
           rmxi_901.startswith(
               "notwithstanding the fact that the Company was not a "
               "reporting company under Section 15(d)")
           and not rmxi_901.startswith("Financial Statement"),
           f"expected front-matter cross-ref context, got {ascii(rmxi_901[:70])}...")

    # --- E3: SITM 8.01 cross-ref-before-header, second live instance ---
    # "...described in Item 8.01 below..." in SITM's 1.01 section matches
    # before the real Item 8.01 header; extraction starts mid-sentence.
    sitm_801 = extract_item_section(_body(SITM), "8.01", 300)
    _check(failures, "E3 SITM-8.01-crossref KNOWN-LIMITATION",
           sitm_801.startswith(
               "below, which includes $150.0 million aggregate principal "
               "amount of Notes"),
           f"expected mid-sentence cross-ref start, got {ascii(sitm_801[:70])}...")

    # --- E4: happy-path sanity on clean bodies ---
    nvts_101 = extract_item_section(_body(NVTS), "1.01", 300)
    _check(failures, "E4a NVTS-1.01-happy-path",
           nvts_101.startswith(
               "Entry into a Material Definitive Agreement. On May 18, 2026, "
               "Navitas Semiconductor Corporation"),
           f"got {ascii(nvts_101[:70])}...")
    bnzi_901 = extract_item_section(bnzi, "9.01", 300)
    _check(failures, "E4b BNZI-9.01-happy-path",
           bnzi_901.startswith("Financial Statements and Exhibits. (d) Exhibits."),
           f"got {ascii(bnzi_901[:70])}...")

    # --- E5: header-not-found placeholder on a real body ---
    bnzi_801 = extract_item_section(bnzi, "8.01", 300)
    _check(failures, "E5 BNZI-8.01-not-found",
           bnzi_801 == "[item 8.01 header not found in body]",
           f"got {ascii(bnzi_801)}")

    # --- E6: GSIT-type cross-reference empty section (synthetic shape) ---
    gsit_801 = extract_item_section(GSIT_SHAPED_BODY, "8.01", 300)
    _check(failures, "E6a GSIT-shape-8.01-empty-section",
           gsit_801 == EMPTY_SECTION_PLACEHOLDER,
           f"got {ascii(gsit_801)}")
    # sanity: the substantive 2.02 section on the same body still extracts
    gsit_202 = extract_item_section(GSIT_SHAPED_BODY, "2.02", 300)
    _check(failures, "E6b GSIT-shape-2.02-content",
           gsit_202.startswith("Results of Operations and Financial Condition."),
           f"got {ascii(gsit_202[:70])}...")

    # --- E7: degenerate inputs ---
    _check(failures, "E7a empty-body",
           extract_item_section("", "1.01", 300) == "[empty document body]",
           "empty string placeholder")
    _check(failures, "E7b no-headers",
           extract_item_section("no item headers here at all", "1.01", 300)
           == "[no Item N.NN headers found in body]",
           "headerless body placeholder")

    return failures


# ---- pytest entrypoints ----
def test_parse_display_name_pinned():
    failures = run_display_name()
    assert not failures, "parse_display_name regressions:\n" + "\n".join(failures)


def test_extract_item_section_pinned():
    failures = run_extract()
    assert not failures, "extract_item_section regressions:\n" + "\n".join(failures)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    print("P — parse_display_name pinned behaviors:")
    fp = run_display_name()
    print(f"\nP: {6 - len(fp)}/6 pinned" + (" — PASS" if not fp else " — FAIL"))
    print("\nE — extract_item_section fixture regressions:")
    fe = run_extract()
    print(f"E: {12 - len(fe)}/12 pinned" + (" — PASS" if not fe else " — FAIL"))
    if fp or fe:
        print("\nFAILURES:")
        for x in (*fp, *fe):
            print("  -", x)
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
