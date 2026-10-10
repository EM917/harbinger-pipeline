#!/usr/bin/env python3
"""
rule_engine.py - Phase 1A Tier 6.2 — deterministic 8-K triage rule engine.

Replaces the hardcoded `ticker -> rejection_label` dict in
`migrate_week1_baseline.py` (now retired to a golden fixture / reference) with a
deterministic content-marker classifier + cap-band filter.

Contract (Tier 6.2 §3):
    classify(body_text, market_cap_millions) -> RuleResult

Determinism: the same (body_text, market_cap_millions) always yields the same
output. No randomness, no wall-clock, no network. (Market cap is fetched
separately by scripts/fetch_market_cap.py and passed IN, so the engine stays
pure and testable.)

Two stages:
  1. Content markers — an ORDERED list of named predicates; FIRST match wins,
     the rest short-circuit. A match means the hit is a known non-partnership
     rejection class (financing / debt / M&A / SPAC / IP). No content marker
     matches -> the hit is a "survivor" -> stage 2.
  2. Cap-band filter (survivors only) — sub-$5B passes to human review as a
     candidate; >= $5B is rejected as `oversized_marker`; cap-unavailable
     surfaces as a candidate with cap_resolved=0 (bias UP to human review,
     never silently drop a potential candidate).

This engine ONLY rejects. It does NOT positively detect partnerships — survivors
are surfaced for human §0-§8 review. Per Phase 5 (permanently vetoed), there is
no LLM judgment anywhere in the rejection path.

See RULES.md for predicate evidence, priority-order constraints, and the
denylist-leakage note.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Cap-band constant (Tier 6.2 §4) — sub-$5B, strictly less than. Adjustable.
# ---------------------------------------------------------------------------
CAP_BAND_MAX_MM: float = 5000.0


@dataclass(frozen=True)
class MarkerRule:
    """A content-marker predicate.

    Match logic: any(p in text for p in any_of) AND
                 (not and_any_of OR any(p in text for p in and_any_of)).
    All phrases are lowercase substrings; the engine lowercases the body once.
    """
    label: str
    any_of: tuple[str, ...]
    and_any_of: tuple[str, ...] = ()
    rationale: str = ""
    evidence: tuple[str, ...] = ()  # supporting Week-1 accession_number(s)

    def match(self, text_lower: str) -> str | None:
        """Return the matched primary phrase, or None."""
        primary = next((p for p in self.any_of if p in text_lower), None)
        if primary is None:
            return None
        if self.and_any_of and not any(p in text_lower for p in self.and_any_of):
            return None
        return primary


# ---------------------------------------------------------------------------
# ORDERED content markers (first-match wins). Order is load-bearing — see §3.3
# and RULES.md. Data-forced constraints (Week-1 full-body multi-matches):
#   - debt BEFORE financing : BNZI/SABR/SITM bodies carry both debt + a financing
#     phrase but are debt.
#   - ip   BEFORE financing : RMXI body carries IP + "private placement" but is IP.
# spac/ma are placed first for specificity (their phrases are distinctive and do
# not collide with any other Week-1 hit).
#
# DELIBERATE phrase EXCLUSIONS (these would cause Week-1 false positives — see
# RULES.md "Excluded phrases"):
#   - bare "term loan"            -> ICHR (financing) mentions it incidentally
#   - bare "convertible note" /
#     "convertible promissory note" / "senior convertible note"
#                                 -> MOBX (financing) carries a convertible note
#   BNZI (debt) still matches via "loan agreement"; CRWV via "credit agreement";
#   so dropping the broad phrases costs no debt-hit coverage.
# ---------------------------------------------------------------------------
MARKERS: tuple[MarkerRule, ...] = (
    MarkerRule(
        label="spac_marker",
        any_of=("sponsor",),
        and_any_of=("earnout", "business combination", "forfeiture", "de-spac"),
        rationale="SPAC sponsor share settlement / earnout / forfeiture",
        evidence=("0001104659-26-065731",),  # NVTS
    ),
    MarkerRule(
        label="ip_acquisition_marker",
        any_of=("intellectual property purchase agreement",),
        rationale="IP / asset purchase agreement (not a commercial partnership)",
        evidence=("0001213900-26-058408",),  # RMXI
    ),
    MarkerRule(
        label="ma_marker",
        any_of=(
            "sale and purchase agreement",
            "agreement and plan of merger",
            "asset purchase agreement",
            "merger agreement",
            "membership interest purchase",
        ),
        rationale="M&A / divestiture: merger or sale-and-purchase agreement",
        evidence=("0001437749-26-017991",),  # SKIL
    ),
    MarkerRule(
        label="debt_marker",
        any_of=(
            "senior notes",
            "exchangeable senior notes",
            "convertible senior notes",
            "note purchase agreement",
            "notes offering",
            "credit agreement",
            "credit increase",
            "loan agreement",
            "indenture",
            "delayed draw",
        ),
        rationale="debt instrument: notes / credit facility / loan / indenture",
        evidence=(
            "0001493152-26-024444",  # ATHR  note purchase agreement
            "0001140361-26-022270",  # BDX   notes offering / indenture
            "0001193125-26-227353",  # BNZI  loan agreement (8-K/A; Tier 6.2 P1 reclass)
            "0001769628-26-000236",  # CRWV  credit agreement / delayed draw
            "0001628280-26-035821",  # RDNW  credit increase
            "0001193125-26-230898",  # SABR  exchangeable senior notes / indenture
            "0001193125-26-237180",  # SITM  convertible senior notes / indenture
        ),
    ),
    MarkerRule(
        label="financing_marker",
        any_of=(
            "underwriting agreement",
            "securities purchase agreement",
            "private placement",
            "at-the-market",
            "at the market",
            "sales agreement",
            "stock purchase agreement",
            "registered direct",
            "placement agent",
            "pre-funded warrant",
        ),
        rationale="equity / dilutive financing: underwriting / PIPE / ATM / RDO",
        evidence=(
            "0001493152-26-024738",  # CODX  securities purchase agreement / PIPE
            "0001104659-26-063581",  # EXYN  underwriting agreement
            "0001652535-26-000048",  # ICHR  ATM sales agreement
            "0001437749-26-017643",  # IPWR  securities purchase agreement
            "0001493152-26-024386",  # MOBX  securities purchase agreement (+ conv note)
            "0001829126-26-005453",  # NXL   stock purchase agreement
            "0001628280-26-037242",  # QTI   underwriting agreement
            "0001104659-26-063936",  # RPID  underwriting agreement
        ),
    ),
)

# New label introduced by Tier 6.2 (cap-band stage), distinct from the 5 content
# markers above.
OVERSIZED_LABEL = "oversized_marker"


@dataclass
class RuleResult:
    is_candidate: int                  # 0 or 1
    rejection_label: str | None        # None iff is_candidate == 1
    cap_resolved: int | None           # 1/0 if cap-band reached; None if content-rejected
    matched_marker: str | None = None  # which content marker fired (if any)
    matched_phrase: str | None = None  # the phrase that fired (for evidence prose)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def evidence_prose(self, method: str = "rule_engine_v1") -> str:
        if self.matched_marker:
            return (
                f"{method}: content-rejected as {self.matched_marker} "
                f"via phrase {self.matched_phrase!r}"
            )
        if self.rejection_label == OVERSIZED_LABEL:
            return f"{method}: survivor rejected by cap-band (market cap >= ${CAP_BAND_MAX_MM:.0f}MM)"
        if self.is_candidate and self.cap_resolved == 0:
            return f"{method}: survivor; market cap unavailable -> surfaced to human review (cap_resolved=0)"
        return f"{method}: survivor; sub-${CAP_BAND_MAX_MM:.0f}MM -> candidate for human review"


def classify_content(body_text: str) -> tuple[str | None, str | None, str | None]:
    """Stage 1. Returns (label, marker_label, matched_phrase).
    label is None (survivor) if no content marker matches."""
    t = body_text.lower()
    for rule in MARKERS:
        phrase = rule.match(t)
        if phrase is not None:
            return rule.label, rule.label, phrase
    return None, None, None


def apply_cap_band(market_cap_millions: float | None) -> RuleResult:
    """Stage 2 (survivors only). Tier 6.2 §4 three-branch logic."""
    if market_cap_millions is None:
        return RuleResult(
            is_candidate=1,
            rejection_label=None,
            cap_resolved=0,
            warnings=(
                "market cap unavailable (yfinance fetch failed); surfacing to "
                "human review rather than silently dropping a potential candidate",
            ),
        )
    if market_cap_millions >= CAP_BAND_MAX_MM:
        return RuleResult(
            is_candidate=0,
            rejection_label=OVERSIZED_LABEL,
            cap_resolved=1,
        )
    return RuleResult(is_candidate=1, rejection_label=None, cap_resolved=1)


def classify(body_text: str, market_cap_millions: float | None = None) -> RuleResult:
    """Full pipeline: content markers first, then cap-band for survivors."""
    label, marker, phrase = classify_content(body_text)
    if label is not None:
        # Content-rejected: cap-band not reached, cap_resolved stays None.
        return RuleResult(
            is_candidate=0,
            rejection_label=label,
            cap_resolved=None,
            matched_marker=marker,
            matched_phrase=phrase,
        )
    return apply_cap_band(market_cap_millions)


# ---------------------------------------------------------------------------
# CLI (ad-hoc classification of a body-text file)
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description="Classify an 8-K body text with the Tier 6.2 rule engine.")
    ap.add_argument("--body-file", required=True, help="Path to a cleaned 8-K body text file.")
    ap.add_argument("--market-cap-mm", type=float, default=None,
                    help="Market cap in $millions (optional; enables cap-band for survivors).")
    args = ap.parse_args()
    from pathlib import Path
    text = Path(args.body_file).read_text(encoding="utf-8")
    res = classify(text, args.market_cap_mm)
    print(f"is_candidate   : {res.is_candidate}")
    print(f"rejection_label: {res.rejection_label}")
    print(f"cap_resolved   : {res.cap_resolved}")
    print(f"matched_marker : {res.matched_marker}")
    print(f"matched_phrase : {res.matched_phrase}")
    print(f"evidence       : {res.evidence_prose()}")
    for w in res.warnings:
        print(f"WARNING: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
