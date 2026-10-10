#!/usr/bin/env python3
"""
verify_claims.py - case-generic literal-grep claim verification engine.

Tier 1.2 successor to scripts/verify_extract.py (deprecated). Reads a
per-case YAML registry at cases/{case-slug}/verify_registry.yaml that
declares:
  - source_corpus globs (files swept for UNATTRIBUTED tokens)
  - claims registry (per-claim grep / Form 4 lookup specifications)
  - allowed_derived list (user-curated tokens excluded from UNATTRIBUTED)
  - report customization (title, disclaimer, manual-review hints,
    expected_unattributed_range)

Engine features beyond verify_extract.py:
  - YAML-driven; no hardcoded case-specific constants.
  - Form 4 derived auto-allowlist: after build_form4_index, products of
    (shares x pricePerShare) are computed for every transaction and added
    to an engine-internal allowlist (4 representations per product to
    cover extract-author rounding variants). This was the dominant
    source of UNATTRIBUTED false-positives in the 2026-05-ALAB case.
    Disable via --no-form4-auto-allowlist.
  - INDETERMINATE status: when a claim's sources glob matches zero files,
    the claim is marked INDETERMINATE (distinct from NOT_FOUND), so
    "case not yet fetched" is distinguishable from "claim genuinely
    fails verification".
  - Health-check section in report: if YAML sets
    report.expected_unattributed_range, the report renders an actual-vs-
    expected status block (green/yellow/red). Omitted if not configured.

Exit codes:
    0 = all VERIFIED, no UNATTRIBUTED, no NOT_FOUND (INDETERMINATE allowed)
    1 = any NOT_FOUND OR any UNATTRIBUTED
    2 = unrecoverable error (YAML missing/malformed, case dir missing)

Output:
    cases/{slug}/verify_report.md  (full report, overwrite each run)
    Summary printed to stdout.

Usage:
    python scripts/verify_claims.py --case 2026-05-ALAB
    python scripts/verify_claims.py --case 2026-05-ALAB --registry /alt/path.yaml
    python scripts/verify_claims.py --case 2026-05-ALAB --no-form4-auto-allowlist

Dependencies: pyyaml, beautifulsoup4, lxml.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import sys
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Suppress noisy XML-via-HTML-parser warnings (Form 4 XMLs go through lxml).
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


# ---------------------------------------------------------------------------
# Token normalization (PRESERVED VERBATIM from verify_extract.py L136-139,
# including the .replace("$", "") strip that handles $192M vs 192M tokens)
# ---------------------------------------------------------------------------

def _normalize_token(t: str) -> str:
    """Lowercase + strip whitespace, commas, $ signs.

    Note: This intentionally strips ALL `$` characters (not just leading/
    trailing) so tokens like `$192M`, `192M`, and `$ 192 M` all normalize
    to the same form. The Tier 1.2 deep-dive (2026-05-22) confirmed this
    behavior is correct; 311 UNATTRIBUTED in the original ALAB run were
    dominated by Form 4 derived dollar values (shares x price products
    computed by extract author, not present in source XML), not by
    $-stripping omissions. See cases/2026-05-ALAB/analysis.md Source
    Hygiene section for the full diagnosis.
    """
    return t.lower().replace(" ", "").replace(",", "").replace("$", "")


# ---------------------------------------------------------------------------
# Token sweep patterns (PRESERVED VERBATIM from verify_extract.py L431-442)
# ---------------------------------------------------------------------------

TOKEN_PATTERNS = [
    # Currency with optional units
    re.compile(r"\$[\d,]+(?:\.\d+)?\s*(?:billion|million|thousand|B|M|K)?\b", re.I),
    # Percentages
    re.compile(r"\b\d+(?:\.\d+)?\s*%\b"),
    re.compile(r"\b\d+(?:-\d+)?\s*pp\b"),  # percentage points
    re.compile(r"\b\d+\s*bps\b", re.I),
    # Large bare numbers with commas (e.g. 3,262,299)
    re.compile(r"\b\d{1,3}(?:,\d{3}){2,}\b"),
    # Prices like $142.82 (any $XXX.XX form)
    re.compile(r"\$\d+(?:\.\d{2,4})\b"),
]


# ---------------------------------------------------------------------------
# YAML loading + validation
# ---------------------------------------------------------------------------

VALID_CLAIM_TYPES = {"regex_grep", "cooccurrence_grep", "form4_lookup", "form4_footnote"}
VALID_MATCH_TYPES = {"regex", "literal", "substring"}


def load_registry(registry_path: Path) -> dict:
    """Load + validate the per-case verify_registry.yaml. Exits with code 1
    on any unrecoverable validation error, printing a clear pointer."""
    if not registry_path.exists():
        print(
            f"ERROR: registry file not found: {registry_path}\n"
            f"       Expected at cases/<slug>/verify_registry.yaml.\n"
            f"       Pass --registry <path> to override.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        with open(registry_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        loc = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        print(
            f"ERROR: YAML parse failed{loc}: {registry_path}\n"
            f"       {getattr(e, 'problem', str(e))}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not isinstance(data, dict):
        print(f"ERROR: registry root must be a mapping (dict), got {type(data).__name__}", file=sys.stderr)
        sys.exit(1)

    # Required top-level keys
    if "claims" not in data or not isinstance(data["claims"], list):
        print("ERROR: registry missing required `claims:` list", file=sys.stderr)
        sys.exit(1)

    # Default source_corpus if omitted (covers minimal-registry case)
    data.setdefault("source_corpus", ["inputs/sec_filings/*.htm", "inputs/sec_filings/*.html", "inputs/sec_filings/*.xml"])
    data.setdefault("form4_glob", "inputs/sec_filings/*_4_*.xml")
    data.setdefault("allowed_derived", [])
    data.setdefault("report", {})
    data.setdefault("case", {})

    # Per-claim validation
    for idx, claim in enumerate(data["claims"]):
        if not isinstance(claim, dict):
            print(f"ERROR: claim at index {idx} is not a mapping", file=sys.stderr)
            sys.exit(1)
        for field in ("id", "type", "description"):
            if field not in claim:
                print(
                    f"ERROR: claim at index {idx} (id={claim.get('id', '<none>')}) "
                    f"missing required field `{field}`",
                    file=sys.stderr,
                )
                sys.exit(1)
        if claim["type"] not in VALID_CLAIM_TYPES:
            print(
                f"ERROR: claim {claim['id']} has unknown type `{claim['type']}`; "
                f"expected one of {sorted(VALID_CLAIM_TYPES)}",
                file=sys.stderr,
            )
            sys.exit(1)

        ctype = claim["type"]
        if ctype in {"regex_grep", "cooccurrence_grep", "form4_footnote"}:
            if "text" not in claim or not isinstance(claim["text"], list) or not claim["text"]:
                print(f"ERROR: claim {claim['id']} ({ctype}) missing non-empty `text:` list", file=sys.stderr)
                sys.exit(1)
            if claim.get("match_type") not in VALID_MATCH_TYPES:
                print(
                    f"ERROR: claim {claim['id']} ({ctype}) has invalid match_type "
                    f"`{claim.get('match_type')}`; expected one of {sorted(VALID_MATCH_TYPES)}",
                    file=sys.stderr,
                )
                sys.exit(1)
        if ctype in {"regex_grep", "cooccurrence_grep"}:
            if "sources" not in claim or not isinstance(claim["sources"], list) or not claim["sources"]:
                print(f"ERROR: claim {claim['id']} ({ctype}) missing non-empty `sources:` list", file=sys.stderr)
                sys.exit(1)
        if ctype == "cooccurrence_grep" and len(claim["text"]) != 2:
            print(
                f"ERROR: claim {claim['id']} (cooccurrence_grep) requires exactly 2 entries in `text:` "
                f"(pattern_a, pattern_b); got {len(claim['text'])}",
                file=sys.stderr,
            )
            sys.exit(1)
        if ctype in {"form4_lookup", "form4_footnote"}:
            if "lookup_by" not in claim or not isinstance(claim["lookup_by"], dict):
                print(f"ERROR: claim {claim['id']} ({ctype}) missing `lookup_by:` mapping", file=sys.stderr)
                sys.exit(1)
            if "name_contains" not in claim["lookup_by"]:
                print(f"ERROR: claim {claim['id']} ({ctype}) missing `lookup_by.name_contains`", file=sys.stderr)
                sys.exit(1)

    # Normalize allowed_derived entries to (token, reason) dicts
    normalized = []
    for idx, entry in enumerate(data["allowed_derived"]):
        if isinstance(entry, str):
            normalized.append({"token": entry, "reason": ""})
        elif isinstance(entry, dict) and "token" in entry:
            normalized.append({"token": entry["token"], "reason": entry.get("reason", "")})
        else:
            print(f"ERROR: allowed_derived[{idx}] must be a string or {{token, reason}} dict", file=sys.stderr)
            sys.exit(1)
    data["allowed_derived"] = normalized

    return data


# ---------------------------------------------------------------------------
# Source corpus loading (paths relative to case_dir)
# ---------------------------------------------------------------------------

def load_source_corpus(case_dir: Path, source_corpus_globs: list[str]) -> dict[str, dict]:
    """Load files matched by any of the case-relative globs. Corpus is
    keyed by the file's case-relative path (so per-claim sources globs
    can match against same-format paths). UTF-8 decode failures are
    logged and skipped, not crashed."""
    corpus: dict[str, dict] = {}
    seen: set[Path] = set()
    candidates: list[Path] = []
    for glob in source_corpus_globs:
        for f in sorted(case_dir.glob(glob)):
            if f.is_file():
                resolved = f.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                candidates.append(f)

    for f in candidates:
        try:
            raw = f.read_bytes()
        except OSError as e:
            print(f"WARN: could not read {f}: {e}", file=sys.stderr)
            continue
        try:
            soup = BeautifulSoup(raw, "lxml")
            plain = soup.get_text(separator="\n", strip=True)
        except Exception:
            try:
                plain = raw.decode("utf-8", errors="replace")
            except UnicodeDecodeError as e:
                print(f"WARN: UTF-8 decode failed for {f}: {e} — skipping file", file=sys.stderr)
                continue
        case_relative = f.relative_to(case_dir).as_posix()
        corpus[case_relative] = {
            "path": case_relative,
            "abs_path": str(f),
            "raw_bytes_len": len(raw),
            "plain_text": plain,
        }
    return corpus


# ---------------------------------------------------------------------------
# Form 4 index (preserved verbatim semantics from verify_extract.py L482-571)
# ---------------------------------------------------------------------------

def build_form4_index(case_dir: Path, form4_glob: str) -> list[dict]:
    """Parse all Form 4 XMLs matching form4_glob (case-relative) into a
    structured per-filing index. Schema preserved from verify_extract.py."""
    index: list[dict] = []
    matched = sorted(case_dir.glob(form4_glob))
    if not matched:
        return index

    for f in matched:
        try:
            tree = ET.parse(f)
            root = tree.getroot()
        except ET.ParseError as e:
            print(f"WARN: Form 4 parse error {f.name}: {e}", file=sys.stderr)
            continue

        name_el = root.find(".//reportingOwner/reportingOwnerId/rptOwnerName")
        name = name_el.text.strip() if name_el is not None and name_el.text else "(unknown)"

        title_parts = []
        rel = root.find(".//reportingOwner/reportingOwnerRelationship")
        if rel is not None:
            for tag in ("isDirector", "isOfficer", "isTenPercentOwner"):
                v = rel.find(tag)
                if v is not None and v.text in {"1", "true"}:
                    title_parts.append(tag.replace("is", ""))
            ot = rel.find("officerTitle")
            if ot is not None and ot.text:
                title_parts.append(ot.text.strip())
        title = " | ".join(title_parts) if title_parts else ""

        transactions: list[dict] = []
        for tx_type, tag in (("non-deriv", "nonDerivativeTransaction"), ("deriv", "derivativeTransaction")):
            for tx in root.findall(f".//{tag}"):
                def _gt(p: str) -> str | None:
                    e = tx.find(p)
                    return e.text.strip() if e is not None and e.text else None

                shares_str = _gt(".//transactionAmounts/transactionShares/value")
                price_str = _gt(".//transactionAmounts/transactionPricePerShare/value")
                try:
                    shares_val = int(round(float(shares_str))) if shares_str else 0
                except ValueError:
                    shares_val = 0
                try:
                    price_val = float(price_str) if price_str else 0.0
                except ValueError:
                    price_val = 0.0

                transactions.append({
                    "type": tx_type,
                    "date": _gt(".//transactionDate/value"),
                    "code": _gt(".//transactionCoding/transactionCode"),
                    "shares": shares_val,
                    "price": price_val,
                    "ad": _gt(".//transactionAmounts/transactionAcquiredDisposedCode/value"),
                    "security": _gt(".//securityTitle/value") or "",
                })

        footnotes_text_parts = []
        for fn in root.findall(".//footnote"):
            if fn.text:
                footnotes_text_parts.append(fn.text.strip())
        footnotes_text = "\n".join(footnotes_text_parts)

        has_10b5_1 = bool(re.search(r"10b5[\s\-]?1|Rule\s+10b5", footnotes_text, re.IGNORECASE))
        for tag in ("rule10b5OneIndicator", "v45RuleIndicator", "affiliations10b5One"):
            el = root.find(f".//{tag}")
            if el is not None:
                val_el = el.find("value")
                val = (val_el.text if val_el is not None and val_el.text else el.text or "").strip()
                if val in {"1", "true", "Y", "yes"}:
                    has_10b5_1 = True

        index.append({
            "file": f.name,
            "path": f.relative_to(case_dir).as_posix(),
            "name": name,
            "title": title,
            "transactions": transactions,
            "has_10b5_1": has_10b5_1,
            "footnotes_text": footnotes_text,
        })

    return index


# ---------------------------------------------------------------------------
# Form 4 derived auto-allowlist (Tier 1.2 mechanism)
# ---------------------------------------------------------------------------

def compute_form4_derived_allowlist(form4_index: list[dict]) -> set[str]:
    """Compute normalized notional values from all Form 4 transactions.

    For each transaction with shares > 0 AND pricePerShare > 0, compute
    product = shares * pricePerShare and add 4 representations to the
    auto-allowlist set:
      1. Full Python float repr -- handles trailing precision noise
      2. 2-decimal formatted -- most common extract format
      3. Truncated int (int(product)) -- handles extract rounding down
      4. Rounded int (round(product)) -- handles extract rounding nearest

    Edge cases SKIPPED (no notional value to compute):
      - shares == 0: incomplete parse / no transaction value
      - price == 0: grants (Code A), ESPP at $0, code-G gifts at $0 par
        -- these have no dollar notional an extract could derive, so
        skipping prevents polluting the allowlist with `0` which would
        mask real UNATTRIBUTED zeros.

    All 4 representations are run through _normalize_token() to match
    the same normalization used in sweep_tokens_in_extracts.
    """
    auto_allow: set[str] = set()
    for filing in form4_index:
        for tx in filing["transactions"]:
            shares = tx.get("shares", 0)
            price = tx.get("price", 0.0)
            if shares == 0 or price == 0:
                continue
            product = shares * price
            for rep in (
                f"{product}",           # e.g., '6060312.4194'
                f"{product:.2f}",       # e.g., '6060312.42'
                f"{int(product)}",      # e.g., '6060312'
                f"{round(product)}",    # e.g., '6060312'
            ):
                auto_allow.add(_normalize_token(rep))
    return auto_allow


# ---------------------------------------------------------------------------
# Claim runners (one per type)
# ---------------------------------------------------------------------------

def _compile_pattern(text: str, match_type: str) -> re.Pattern:
    """Compile per match_type. regex uses IGNORECASE | DOTALL (preserves
    verify_extract.py L596 flags); literal escapes for case-sensitive
    substring; substring escapes for case-insensitive substring."""
    if match_type == "regex":
        return re.compile(text, re.IGNORECASE | re.DOTALL)
    if match_type == "substring":
        return re.compile(re.escape(text), re.IGNORECASE)
    if match_type == "literal":
        return re.compile(re.escape(text))
    raise ValueError(f"unknown match_type: {match_type}")


def files_matching_globs(corpus_paths: list[str], globs: list[str]) -> list[str]:
    out = []
    for path in corpus_paths:
        for g in globs:
            if fnmatch.fnmatch(path, g):
                out.append(path)
                break
    return out


def run_regex_grep(claim: dict, corpus: dict[str, dict]) -> dict:
    matched_paths = files_matching_globs(list(corpus.keys()), claim["sources"])
    if not matched_paths:
        return {
            "status": "INDETERMINATE",
            "reason": f"sources glob matched 0 files (case not fetched, or glob outdated?)",
            "hits": [],
            "files_searched": [],
            "files_searched_count": 0,
        }
    hits: list[dict] = []
    radius = claim.get("context_radius", 500)
    match_type = claim["match_type"]
    for path in matched_paths:
        text = corpus[path]["plain_text"]
        for pat_text in claim["text"]:
            try:
                pat = _compile_pattern(pat_text, match_type)
            except re.error as e:
                hits.append({"file": path, "pattern": pat_text, "compile_error": str(e),
                             "match_text": "", "char_offset": 0, "plain_text_line": 0, "context": ""})
                continue
            for m in pat.finditer(text):
                start = max(0, m.start() - radius // 2)
                end = min(len(text), m.end() + radius // 2)
                line_no = text.count("\n", 0, m.start()) + 1
                hits.append({
                    "file": path,
                    "pattern": pat_text,
                    "match_text": m.group(0)[:200],
                    "char_offset": m.start(),
                    "plain_text_line": line_no,
                    "context": text[start:end].strip(),
                })

    status = "VERIFIED" if len(hits) >= claim.get("min_total_matches", 1) else "NOT_FOUND"
    return {
        "status": status,
        "hits": hits,
        "files_searched": matched_paths,
        "files_searched_count": len(matched_paths),
    }


def run_cooccurrence_grep(claim: dict, corpus: dict[str, dict]) -> dict:
    matched_paths = files_matching_globs(list(corpus.keys()), claim["sources"])
    if not matched_paths:
        return {
            "status": "INDETERMINATE",
            "reason": "sources glob matched 0 files (case not fetched, or glob outdated?)",
            "hits": [],
            "files_searched": [],
            "files_searched_count": 0,
        }
    pat_a_text, pat_b_text = claim["text"][0], claim["text"][1]
    match_type = claim["match_type"]
    radius = claim.get("context_radius", 200)
    hits: list[dict] = []
    try:
        pat_a = _compile_pattern(pat_a_text, match_type)
        pat_b = _compile_pattern(pat_b_text, match_type)
    except re.error as e:
        return {"status": "NOT_FOUND", "reason": f"pattern compile error: {e}",
                "hits": [], "files_searched": matched_paths, "files_searched_count": len(matched_paths)}

    # Cooccurrence semantic uses IGNORECASE only (preserves verify_extract.py
    # L685-686 flags); _compile_pattern adds DOTALL for regex match_type which
    # is a strict superset, harmless here.
    for path in matched_paths:
        text = corpus[path]["plain_text"]
        a_matches = list(pat_a.finditer(text))
        b_matches = list(pat_b.finditer(text))
        if not a_matches or not b_matches:
            continue
        best_dist = None
        best_pair = None
        for a in a_matches:
            for b in b_matches:
                d = abs(a.start() - b.start())
                if best_dist is None or d < best_dist:
                    best_dist = d
                    best_pair = (a, b)
        a, b = best_pair  # type: ignore
        anchor = b if b.start() > a.start() else a
        ctx_start = max(0, anchor.start() - radius)
        ctx_end = min(len(text), anchor.end() + radius)
        hits.append({
            "file": path,
            "a_pattern": pat_a_text,
            "b_pattern": pat_b_text,
            "a_count": len(a_matches),
            "b_count": len(b_matches),
            "first_a": a_matches[0].group(0),
            "first_b": b_matches[0].group(0),
            "closest_pair_distance_chars": best_dist,
            "context_around_closer_match": text[ctx_start:ctx_end].strip(),
        })

    status = "VERIFIED" if len(hits) >= claim.get("min_files_with_both", 1) else "NOT_FOUND"
    return {
        "status": status,
        "hits": hits,
        "files_searched": matched_paths,
        "files_searched_count": len(matched_paths),
    }


def run_form4_lookup(claim: dict, form4_index: list[dict]) -> dict:
    if not form4_index:
        return {"status": "INDETERMINATE", "reason": "form4_index empty (no Form 4 XMLs loaded)"}
    lb = claim["lookup_by"]
    name_needle = lb.get("name_contains", "").lower()
    date_needle = lb.get("date")
    shares_expected = lb.get("shares_expected")
    file_filter = lb.get("file_contains")

    if file_filter:
        candidate_filings = [f for f in form4_index if file_filter in f["file"]]
    else:
        candidate_filings = form4_index
    matched_filings = [f for f in candidate_filings if name_needle in f["name"].lower()]
    if not matched_filings:
        reason = f"no Form 4 with name containing '{name_needle}'"
        if file_filter:
            reason += f" AND file_contains '{file_filter}'"
        return {"status": "NOT_FOUND", "reason": reason}

    relevant_txns = []
    for filing in matched_filings:
        for tx in filing["transactions"]:
            if date_needle and tx["date"] == date_needle:
                relevant_txns.append({"filing": filing["file"], **tx})
    if not relevant_txns:
        return {
            "status": "NOT_FOUND",
            "reason": f"no transactions on {date_needle} for '{name_needle}' in {len(matched_filings)} candidate filings",
            "matched_filings": [f["file"] for f in matched_filings],
        }

    total_shares = sum(tx["shares"] for tx in relevant_txns)
    if total_shares == shares_expected:
        return {"status": "VERIFIED", "total_shares_summed": total_shares,
                "shares_expected": shares_expected, "transactions": relevant_txns}
    return {
        "status": "NOT_FOUND",
        "reason": (
            f"share count mismatch: expected {shares_expected}, actual sum across "
            f"{len(relevant_txns)} transactions = {total_shares}"
        ),
        "total_shares_summed": total_shares,
        "shares_expected": shares_expected,
        "transactions": relevant_txns,
    }


def run_form4_footnote(claim: dict, form4_index: list[dict]) -> dict:
    if not form4_index:
        return {"status": "INDETERMINATE", "reason": "form4_index empty (no Form 4 XMLs loaded)"}
    lb = claim["lookup_by"]
    name_needle = lb.get("name_contains", "").lower()
    require_plan = claim.get("require_10b5_1_marker", False)
    match_type = claim["match_type"]

    matched_filings = [f for f in form4_index if name_needle in f["name"].lower()]
    if not matched_filings:
        return {"status": "NOT_FOUND", "reason": f"no Form 4 with name containing '{name_needle}'"}

    hits = []
    for filing in matched_filings:
        if require_plan and not filing["has_10b5_1"]:
            continue
        for pat_text in claim["text"]:
            try:
                # Footnote scan preserves verify_extract.py L741 flags
                # (IGNORECASE only, no DOTALL); _compile_pattern's regex
                # flags include DOTALL which is harmless for footnote text.
                pat = _compile_pattern(pat_text, match_type)
            except re.error as e:
                continue
            for m in pat.finditer(filing["footnotes_text"]):
                ft = filing["footnotes_text"]
                hits.append({
                    "filing": filing["file"],
                    "name": filing["name"],
                    "has_10b5_1": filing["has_10b5_1"],
                    "matched_pattern": pat_text,
                    "matched_text": m.group(0),
                    "footnote_context": ft[max(0, m.start() - 150): min(len(ft), m.end() + 150)].strip(),
                })

    if hits:
        return {"status": "VERIFIED", "hits": hits}
    return {
        "status": "NOT_FOUND",
        "reason": (
            f"no footnote pattern match for '{name_needle}' in "
            f"{len(matched_filings)} Form 4 filings (require_10b5_1_marker={require_plan})"
        ),
        "matched_filings": [{"file": f["file"], "has_10b5_1": f["has_10b5_1"]} for f in matched_filings],
    }


# ---------------------------------------------------------------------------
# Bidirectional token sweep
# ---------------------------------------------------------------------------

def sweep_tokens_in_extracts(
    case_dir: Path,
    corpus: dict[str, dict],
    user_allowlist_normalized: set[str],
    user_allowlist_lookup: dict[str, str],
    form4_auto_allowlist: set[str],
) -> list[dict]:
    extracts_dir = case_dir / "inputs" / "extracts"
    if not extracts_dir.exists():
        return []

    findings: list[dict] = []
    source_normalized_blobs = {fname: _normalize_token(c["plain_text"]) for fname, c in corpus.items()}

    for md_file in sorted(extracts_dir.rglob("*.md")):
        if "_archive_" in str(md_file):
            continue
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"WARN: could not read extract {md_file}: {e}", file=sys.stderr)
            continue

        seen_in_this_file: set[str] = set()
        for pat in TOKEN_PATTERNS:
            for m in pat.finditer(text):
                raw_token = m.group(0).strip()
                if raw_token in seen_in_this_file:
                    continue
                seen_in_this_file.add(raw_token)

                norm = _normalize_token(raw_token)
                if not norm:
                    continue

                in_source = any(norm in blob for blob in source_normalized_blobs.values())
                in_user_allowlist = norm in user_allowlist_normalized
                in_form4_auto_allowlist = norm in form4_auto_allowlist

                if in_source:
                    classification = "VERIFIED_IN_SOURCE"
                elif in_user_allowlist:
                    classification = "ALLOWED_DERIVED"
                elif in_form4_auto_allowlist:
                    classification = "ALLOWED_FORM4_DERIVED"
                else:
                    classification = "UNATTRIBUTED"

                if classification == "VERIFIED_IN_SOURCE":
                    continue  # don't list verified tokens (too noisy)

                ctx_start = max(0, m.start() - 80)
                ctx_end = min(len(text), m.end() + 80)
                findings.append({
                    "token": raw_token,
                    "classification": classification,
                    "extract_file": md_file.relative_to(case_dir.parent.parent).as_posix(),
                    "context": text[ctx_start:ctx_end].replace("\n", " ").strip(),
                    "allowlist_reason": user_allowlist_lookup.get(norm, ""),
                })

    return findings


# ---------------------------------------------------------------------------
# Form 4 manual review dump (preserved from verify_extract.py L831-866)
# ---------------------------------------------------------------------------

def form4_dump(form4_index: list[dict]) -> str:
    lines = [
        f"_Total Form 4 filings parsed: {len(form4_index)}_",
        "",
        "| File | Reporter | Title | Date | Code | A/D | Shares | Price | Sec Type | 10b5-1? |",
        "| --- | --- | --- | --- | --- | --- | ---: | ---: | --- | :---: |",
    ]
    total_rows = 0
    for filing in form4_index:
        plan_marker = "Y" if filing["has_10b5_1"] else "-"
        for tx in filing["transactions"]:
            shares = f"{tx['shares']:,}" if tx["shares"] else ""
            price = f"${tx['price']:,.4f}" if tx["price"] else ""
            lines.append(
                "| "
                + " | ".join([
                    filing["file"],
                    filing["name"],
                    filing["title"][:40],
                    tx["date"] or "",
                    tx["code"] or "",
                    tx["ad"] or "",
                    shares,
                    price,
                    tx["security"][:30],
                    plan_marker,
                ])
                + " |"
            )
            total_rows += 1
    lines.append("")
    lines.append(f"_Total transaction rows: {total_rows}_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------

def render_health_check(
    unattributed_count: int,
    allowed_form4_count: int,
    expected_reduction_pct: list | None,
    expected_unattributed_range: list | None,
) -> list[str]:
    """Emit UNATTRIBUTED health-check block.

    Precedence (per Tier 1.2 spec):
      - If `expected_reduction_pct` configured: ratio drives status (recommended
        for cases with Form 4 auto-allowlist; robust to data growth).
      - If `expected_unattributed_range` ALSO configured: shown as secondary
        informational metric.
      - If only `expected_unattributed_range` (no ratio): absolute drives status
        (backward compat for non-Form-4 cases).
      - If neither: section silently suppressed.

    All three metrics (count, baseline, reduction ratio) are displayed
    regardless of which one drives status, so future case authors see the
    full picture.
    """
    has_ratio_cfg = (
        isinstance(expected_reduction_pct, list)
        and len(expected_reduction_pct) == 2
    )
    has_abs_cfg = (
        isinstance(expected_unattributed_range, list)
        and len(expected_unattributed_range) == 2
    )
    if not (has_ratio_cfg or has_abs_cfg):
        return []

    # Compute baseline + ratio (handles edge case baseline == 0)
    baseline = unattributed_count + allowed_form4_count
    if baseline > 0:
        reduction_pct: float | None = (allowed_form4_count / baseline) * 100.0
    else:
        reduction_pct = None

    lines = ["### UNATTRIBUTED Health Check", ""]
    lines.append(f"- Actual UNATTRIBUTED count: **{unattributed_count}**")
    lines.append(f"- Actual baseline (UNATTRIBUTED + ALLOWED_FORM4_DERIVED): **{baseline}**")
    if reduction_pct is None:
        lines.append("- Actual reduction ratio: **undefined** (no auto-allowlist tokens; baseline == 0)")
    else:
        lines.append(f"- Actual reduction ratio: **{reduction_pct:.1f}%** ({allowed_form4_count} / {baseline})")
    lines.append("")

    if has_ratio_cfg:
        # Primary status driven by ratio
        lo_pct, hi_pct = expected_reduction_pct[0], expected_reduction_pct[1]
        lines.append(f"- Expected reduction-ratio range (per `report.expected_reduction_pct`): **[{lo_pct}%, {hi_pct}%]** (primary status)")
        if reduction_pct is None:
            lines.append("- Status: **UNDEFINED** -- cannot evaluate ratio (no auto-allowlist tokens)")
        elif lo_pct <= reduction_pct <= hi_pct:
            lines.append(f"- Status: **OK** -- within calibrated ratio range; auto-allowlist working as expected.")
        elif reduction_pct > hi_pct:
            lines.append(f"- Status: **ABOVE EXPECTED RATIO** ({reduction_pct:.1f}% > {hi_pct}%)")
            lines.append("")
            lines.append("_Possible causes (investigate -- may indicate false-positive auto-allowlist):_")
            lines.append("- Auto-allowlist over-matching: cross-check ALLOWED_FORM4_DERIVED table; if any token doesn't trace to a Form 4 (shares x price) product, the 4-representation set is too broad")
            lines.append("- Source corpus shrunk: more derived values no longer found in source -- check load count vs prior runs")
        else:
            lines.append(f"- Status: **BELOW EXPECTED RATIO** ({reduction_pct:.1f}% < {lo_pct}%)")
            lines.append("")
            lines.append("_Possible causes:_")
            lines.append("- Auto-allowlist under-capturing: new transaction value formats in extracts that the 4-representation set (full repr / 2dp / int / round) doesn't cover")
            lines.append("- Form 4 XML parse errors -- transactions with shares==0 or price==0 are skipped intentionally, but if many extracts reference such values they won't be auto-allowed")
            lines.append("- form4_index undersized vs extract scope (check form4_glob in registry)")
        # Secondary informational
        if has_abs_cfg:
            abs_lo, abs_hi = expected_unattributed_range[0], expected_unattributed_range[1]
            lines.append("")
            lines.append(f"_Secondary metric (informational):_ expected absolute UNATTRIBUTED range **[{abs_lo}, {abs_hi}]**, actual {unattributed_count}.")
    else:
        # Backward-compat: absolute drives status
        lo, hi = expected_unattributed_range[0], expected_unattributed_range[1]
        lines.append(f"- Expected absolute UNATTRIBUTED range (per `report.expected_unattributed_range`): **[{lo}, {hi}]** (primary status)")
        if lo <= unattributed_count <= hi:
            lines.append(f"- Status: **OK** -- within calibrated absolute range; no action needed.")
        elif unattributed_count > hi:
            lines.append(f"- Status: **ABOVE EXPECTED** ({unattributed_count} > {hi})")
            lines.append("")
            lines.append("_Possible causes (investigate before downstream use):_")
            lines.append("- Form 4 auto-allowlist not triggering (check ALLOWED_FORM4_DERIVED count above)")
            lines.append("- New M-suffix or unit-shorthand mismatches in extracts (e.g., `$93.2M` vs source `93,213,000`)")
            lines.append("- Case-specific derivation patterns not yet captured in `allowed_derived`")
            lines.append("- form4_index empty (no Form 4 XMLs loaded -- check form4_glob in registry)")
            lines.append("")
            lines.append("_Note:_ for cases using Form 4 auto-allowlist, `expected_reduction_pct` (ratio-based) is more robust than absolute count -- absolute baseline shifts each Form 4 fetch.")
        else:
            lines.append(f"- Status: **BELOW EXPECTED** ({unattributed_count} < {lo})")
            lines.append("")
            lines.append("_Possible causes (investigate -- may indicate false-positive allowlist):_")
            lines.append("- Form 4 auto-allowlist over-matching (false positives suppressing real UNATTRIBUTED)")
            lines.append("- Cross-check ALLOWED_FORM4_DERIVED table: if any token doesn't trace to a Form 4 (shares x price) product, the auto-allowlist is too broad")
            lines.append("- Token sweep patterns may be missing tokens (regex tuning needed)")
    lines.append("")
    return lines


def render_report(
    case_dir: Path,
    case_slug: str,
    corpus: dict[str, dict],
    form4_index: list[dict],
    form4_auto_allowlist: set[str],
    form4_auto_allowlist_disabled: bool,
    claim_results: list[dict],
    token_findings: list[dict],
    registry_report_cfg: dict,
) -> str:
    now = datetime.now(timezone.utc).isoformat()
    n_verified = sum(1 for c in claim_results if c["result"]["status"] == "VERIFIED")
    n_not_found = sum(1 for c in claim_results if c["result"]["status"] == "NOT_FOUND")
    n_indeterminate = sum(1 for c in claim_results if c["result"]["status"] == "INDETERMINATE")
    n_unattributed = sum(1 for f in token_findings if f["classification"] == "UNATTRIBUTED")
    n_allowed_derived = sum(1 for f in token_findings if f["classification"] == "ALLOWED_DERIVED")
    n_allowed_f4 = sum(1 for f in token_findings if f["classification"] == "ALLOWED_FORM4_DERIVED")

    title = registry_report_cfg.get("title") or f"Verify Report - {case_slug}"
    expected_reduction_pct = registry_report_cfg.get("expected_reduction_pct")
    expected_unattributed_range = registry_report_cfg.get("expected_unattributed_range")
    disclaimer_addendum = registry_report_cfg.get("disclaimer_addendum", "")
    f4_hints = registry_report_cfg.get("form4_review_hints", {})

    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append(f"_Generated:_ `{now}`")
    lines.append("_Tool:_ `scripts/verify_claims.py` (Tier 1.2 case-generic engine)")
    lines.append(f"_Registry:_ `cases/{case_slug}/verify_registry.yaml`")
    lines.append("")

    lines.append("## DISCLAIMER")
    lines.append("")
    lines.append(
        "This report does NOT do semantic verification -- it only checks that "
        "load-bearing claims have literal string matches in source corpus. "
        "**User must scan the +/-N-char context window for each VERIFIED match "
        "to confirm the claim is used in analysis with consistent semantic "
        "scope.** Pattern match = necessary but not sufficient for trust."
    )
    lines.append("")
    if disclaimer_addendum:
        lines.append(disclaimer_addendum.rstrip())
        lines.append("")
    lines.append(
        "Hypothesis-tier and derived-tier claims (market cap math, calendar "
        "diffs, thesis probability bands, etc.) are NOT expected in source. "
        "Token sweep allowlists them via the registry's `allowed_derived` list "
        "(user-curated) AND, separately, via the engine's `form4_derived_auto_allowlist` "
        "mechanism (Form 4 shares x price products auto-computed at runtime)."
    )
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Source corpus: {len(corpus)} files loaded (per `source_corpus` globs)")
    lines.append(f"- Form 4 index: {len(form4_index)} filings parsed")
    if form4_auto_allowlist_disabled:
        lines.append(f"- Form 4 auto-allowlist: **DISABLED** (--no-form4-auto-allowlist)")
    else:
        lines.append(f"- Form 4 auto-allowlist: {len(form4_auto_allowlist)} unique normalized tokens (4 representations per transaction product)")
    lines.append(f"- Total claims in registry: {len(claim_results)}")
    lines.append(f"- **VERIFIED:** {n_verified}")
    lines.append(f"- **NOT_FOUND:** {n_not_found} (= BLOCKERS, investigate)")
    if n_indeterminate:
        lines.append(f"- **INDETERMINATE:** {n_indeterminate} (sources glob matched 0 files -- case fetch incomplete?)")
    lines.append(
        f"- Token sweep: {n_allowed_derived + n_allowed_f4 + n_unattributed} non-source tokens classified "
        f"({n_allowed_derived} ALLOWED_DERIVED + {n_allowed_f4} ALLOWED_FORM4_DERIVED + {n_unattributed} UNATTRIBUTED)"
    )
    lines.append("")

    if n_not_found > 0 or n_unattributed > 0:
        lines.append("> [EXIT 1] At least one NOT_FOUND claim or UNATTRIBUTED token. Investigate before downstream work.")
    elif n_indeterminate > 0:
        lines.append("> [EXIT 0 with warning] No NOT_FOUND or UNATTRIBUTED, but INDETERMINATE claims exist -- case fetch incomplete?")
    else:
        lines.append("> [EXIT 0] All registry claims VERIFIED, no UNATTRIBUTED tokens.")
    lines.append("")

    # Detailed claim results
    lines.append("## Detailed Results")
    lines.append("")
    for cr in claim_results:
        claim = cr["claim"]
        result = cr["result"]
        status = result["status"]
        if status == "VERIFIED":
            icon = "[OK]"
        elif status == "NOT_FOUND":
            icon = "[BLOCKER]"
        else:
            icon = "[INDET]"
        lines.append(f"### {icon} {claim['id']}: {claim['description']}")
        lines.append("")
        lines.append(f"- Type: `{claim['type']}`")
        lines.append(f"- Status: **{status}**")

        if claim["type"] == "regex_grep":
            lines.append(f"- Patterns: `{claim['text']}` (match_type={claim['match_type']})")
            lines.append(f"- Sources: `{claim['sources']}`")
            lines.append(f"- Files searched: {result.get('files_searched_count', 0)}")
            hits = result.get("hits", [])
            lines.append(f"- Matches: {len(hits)}")
            if status == "INDETERMINATE":
                lines.append("")
                lines.append(f"_Reason:_ {result.get('reason', '(no reason)')}")
            elif status == "NOT_FOUND":
                lines.append("")
                lines.append("**BLOCKER.** Investigate before proceeding:")
                lines.append("- Re-open the source HTML(s) manually")
                lines.append("- Confirm whether the claim is verbatim in source or derived from a table")
                lines.append("- If derived, mark the extract as DERIVED claim (not a quoted fact) and add to `allowed_derived` in registry")
                lines.append("- If verbatim but missed by patterns, extend the regex in `text:`")
                if not result.get("files_searched"):
                    lines.append(f"- (No source files matched glob `{claim['sources']}` -- verify glob)")
            else:
                for i, h in enumerate(hits[:5], 1):
                    lines.append("")
                    lines.append(f"**Match {i}:** `{h['file']}` @ plain-text line ~{h['plain_text_line']} (char {h['char_offset']})")
                    lines.append(f"- Pattern hit: `{h['pattern']}` -> matched: `{h['match_text']}`")
                    lines.append(f"- Context (+/-{claim.get('context_radius', 500) // 2} chars):")
                    lines.append("  ```")
                    for ctx_line in h["context"].splitlines():
                        lines.append(f"  {ctx_line}")
                    lines.append("  ```")
                if len(hits) > 5:
                    lines.append("")
                    lines.append(f"_...{len(hits) - 5} additional matches not shown._")

        elif claim["type"] == "cooccurrence_grep":
            lines.append(f"- pattern_a: `{claim['text'][0]}` (match_type={claim['match_type']})")
            lines.append(f"- pattern_b: `{claim['text'][1]}`")
            lines.append(f"- Sources: `{claim['sources']}`")
            lines.append(f"- Files searched: {result.get('files_searched_count', 0)}")
            hits = result.get("hits", [])
            lines.append(f"- Files where both patterns appear: {len(hits)}")
            if status == "INDETERMINATE":
                lines.append("")
                lines.append(f"_Reason:_ {result.get('reason', '(no reason)')}")
            elif status == "NOT_FOUND":
                lines.append("")
                lines.append("**BLOCKER.** Both patterns must appear in the same file. Investigate:")
                lines.append("- If one or both terms are absent from source, claim may be agent inference (not verbatim)")
                lines.append("- If terms are present but pattern doesn't match, extend regex")
            else:
                for h in hits[:3]:
                    lines.append("")
                    lines.append(f"**File:** `{h['file']}`")
                    lines.append(f"- A matches: {h['a_count']} (first: `{h['first_a']}`)")
                    lines.append(f"- B matches: {h['b_count']} (first: `{h['first_b']}`)")
                    lines.append(f"- Closest A-B distance: {h['closest_pair_distance_chars']} chars")
                    lines.append("- Context near closer match:")
                    lines.append("  ```")
                    for ctx_line in h["context_around_closer_match"].splitlines():
                        lines.append(f"  {ctx_line}")
                    lines.append("  ```")

        elif claim["type"] == "form4_lookup":
            lines.append(f"- Lookup: `{claim['lookup_by']}`")
            if status == "VERIFIED":
                lines.append(f"- Sum of shares matched expected: **{result['total_shares_summed']:,} == {result['shares_expected']:,}** [OK]")
                lines.append("- Transactions found:")
                lines.append("  ```")
                for tx in result["transactions"]:
                    lines.append(
                        f"  {tx['filing']} | date={tx['date']} | code={tx['code']} | "
                        f"A/D={tx['ad']} | shares={tx['shares']:,} | price=${tx['price']:.4f}"
                    )
                lines.append("  ```")
            elif status == "INDETERMINATE":
                lines.append(f"- Reason: {result.get('reason')}")
            else:
                lines.append(f"- Reason: {result.get('reason')}")
                if "matched_filings" in result:
                    lines.append(f"- Candidate filings: {result['matched_filings']}")
                if "transactions" in result:
                    lines.append("- Transactions on date:")
                    lines.append("  ```")
                    for tx in result["transactions"]:
                        lines.append(
                            f"  {tx['filing']} | date={tx['date']} | code={tx['code']} | "
                            f"A/D={tx['ad']} | shares={tx['shares']:,} | price=${tx['price']:.4f}"
                        )
                    lines.append("  ```")
                    lines.append("")
                    lines.append("**BLOCKER.** Either expected count is wrong (update claim), OR extract claim is wrong.")

        elif claim["type"] == "form4_footnote":
            lines.append(f"- Lookup: `{claim['lookup_by']}`, require 10b5-1 marker: {claim.get('require_10b5_1_marker', False)}")
            lines.append(f"- Footnote patterns: `{claim['text']}` (match_type={claim['match_type']})")
            if status == "VERIFIED":
                hits = result.get("hits", [])
                lines.append(f"- Matches: {len(hits)}")
                for h in hits[:3]:
                    lines.append("")
                    lines.append(f"**Hit:** `{h['filing']}` (name={h['name']}, has_10b5_1={h['has_10b5_1']})")
                    lines.append(f"- Matched: `{h['matched_text']}` (pattern `{h['matched_pattern']}`)")
                    lines.append("- Footnote context:")
                    lines.append("  ```")
                    for ctx_line in h["footnote_context"].splitlines():
                        lines.append(f"  {ctx_line}")
                    lines.append("  ```")
            elif status == "INDETERMINATE":
                lines.append(f"- Reason: {result.get('reason')}")
            else:
                lines.append(f"- Reason: {result.get('reason')}")
                if "matched_filings" in result:
                    lines.append("- Candidate filings (with 10b5-1 marker status):")
                    for mf in result["matched_filings"]:
                        lines.append(f"  - `{mf['file']}` (has_10b5_1={mf['has_10b5_1']})")
                lines.append("")
                lines.append("**BLOCKER.** Investigate footnote text in candidate Form 4s.")

        lines.append("")
        lines.append("---")
        lines.append("")

    # Token sweep
    lines.append("## Bidirectional Token Sweep")
    lines.append("")
    extract_count = len({f["extract_file"] for f in token_findings}) if token_findings else 0
    lines.append(f"_Extracts scanned: {extract_count} files (excluding `_archive_*`)._  ")
    lines.append(
        "_Tokens shown: only NON-VERIFIED-IN-SOURCE. VERIFIED-IN-SOURCE tokens "
        "are excluded from this list to reduce noise._"
    )
    lines.append("")
    lines.append(
        f"**Classifications:** ALLOWED_DERIVED = {n_allowed_derived}, "
        f"ALLOWED_FORM4_DERIVED = {n_allowed_f4}, UNATTRIBUTED = {n_unattributed}"
    )
    lines.append("")

    # Health-check block (optional; ratio-based primary, absolute secondary)
    lines.extend(render_health_check(
        unattributed_count=n_unattributed,
        allowed_form4_count=n_allowed_f4,
        expected_reduction_pct=expected_reduction_pct,
        expected_unattributed_range=expected_unattributed_range,
    ))

    if n_unattributed:
        lines.append("### UNATTRIBUTED tokens (flag for review)")
        lines.append("")
        lines.append("| Token | Extract file | Surrounding context (+/-80 chars) |")
        lines.append("| --- | --- | --- |")
        for f in token_findings:
            if f["classification"] != "UNATTRIBUTED":
                continue
            ctx = f["context"].replace("|", "\\|")[:200]
            lines.append(f"| `{f['token']}` | `{f['extract_file']}` | {ctx} |")
        lines.append("")
        lines.append(
            "For each: decide if it's (a) a real source-corpus claim with a "
            "pattern miss (extend the source side), (b) a derived/hypothesis "
            "value that should be added to `allowed_derived`, (c) a Form 4 "
            "derived value the auto-allowlist missed (file as engine bug), or "
            "(d) an actual fabrication."
        )
        lines.append("")
    else:
        lines.append("_(no UNATTRIBUTED tokens -- all non-source tokens are allowlisted)_")
        lines.append("")

    if n_allowed_f4:
        lines.append("### ALLOWED_FORM4_DERIVED tokens (engine-auto)")
        lines.append("")
        lines.append("_Tokens matched by the Form 4 derived auto-allowlist (shares x pricePerShare products)._")
        lines.append("")
        lines.append("| Token | Extract file | Context (+/-80 chars) |")
        lines.append("| --- | --- | --- |")
        for f in token_findings:
            if f["classification"] != "ALLOWED_FORM4_DERIVED":
                continue
            ctx = f["context"].replace("|", "\\|")[:200]
            lines.append(f"| `{f['token']}` | `{f['extract_file']}` | {ctx} |")
        lines.append("")

    if n_allowed_derived:
        lines.append("### ALLOWED_DERIVED tokens (user-curated registry)")
        lines.append("")
        lines.append("| Token | Extract file | Allowlist reason |")
        lines.append("| --- | --- | --- |")
        for f in token_findings:
            if f["classification"] != "ALLOWED_DERIVED":
                continue
            lines.append(f"| `{f['token']}` | `{f['extract_file']}` | {f['allowlist_reason']} |")
        lines.append("")

    # Form 4 manual review dump
    lines.append("---")
    lines.append("")
    lines.append("## Form 4 Manual Review Dump")
    lines.append("")
    if f4_hints:
        intro = f4_hints.get("intro")
        if intro:
            lines.append(intro)
            lines.append("")
        for bullet in f4_hints.get("bullets", []):
            lines.append(f"- {bullet}")
        if f4_hints.get("bullets"):
            lines.append("")
    lines.append(form4_dump(form4_index))
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
        description="Case-generic literal-grep claim verification engine (Tier 1.2)."
    )
    parser.add_argument(
        "--case", required=True,
        help="Case slug (e.g., 2026-05-ALAB). Resolves to cases/<slug>/.",
    )
    parser.add_argument(
        "--registry", default=None,
        help="Optional override path to YAML registry. "
             "Default: cases/<slug>/verify_registry.yaml",
    )
    parser.add_argument(
        "--no-form4-auto-allowlist", action="store_true",
        help="Disable the engine's auto-allowlist of Form 4 derived notional values "
             "(shares x pricePerShare products). Useful for diagnosis baseline.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    case_slug = args.case.strip()
    case_dir = repo_root / "cases" / case_slug
    if not case_dir.exists():
        print(f"ERROR: case directory not found: {case_dir}", file=sys.stderr)
        return 2

    registry_path = Path(args.registry).resolve() if args.registry else (case_dir / "verify_registry.yaml")
    print(f"[verify_claims] case={case_slug}")
    print(f"[verify_claims] registry={registry_path.relative_to(repo_root) if registry_path.is_relative_to(repo_root) else registry_path}")

    registry = load_registry(registry_path)

    source_globs = registry["source_corpus"]
    form4_glob = registry["form4_glob"]
    claims = registry["claims"]

    print(f"[verify_claims] Loading source corpus from {len(source_globs)} glob pattern(s)...")
    corpus = load_source_corpus(case_dir, source_globs)
    print(f"[verify_claims]   loaded {len(corpus)} files")

    print(f"[verify_claims] Building Form 4 index from `{form4_glob}`...")
    form4_index = build_form4_index(case_dir, form4_glob)
    print(f"[verify_claims]   parsed {len(form4_index)} Form 4 filings")

    if args.no_form4_auto_allowlist:
        form4_auto_allowlist: set[str] = set()
        print(f"[verify_claims] Form 4 derived auto-allowlist: DISABLED (--no-form4-auto-allowlist)")
    else:
        form4_auto_allowlist = compute_form4_derived_allowlist(form4_index)
        print(f"[verify_claims] Form 4 derived auto-allowlist: {len(form4_auto_allowlist)} unique normalized tokens")

    user_allowlist_lookup: dict[str, str] = {}
    for entry in registry["allowed_derived"]:
        norm = _normalize_token(entry["token"])
        user_allowlist_lookup[norm] = entry["reason"]
    user_allowlist_normalized = set(user_allowlist_lookup.keys())
    print(f"[verify_claims] User allowed_derived list: {len(user_allowlist_normalized)} normalized tokens")

    print(f"[verify_claims] Running {len(claims)} claim checks...")
    claim_results: list[dict] = []
    for claim in claims:
        ctype = claim["type"]
        if ctype == "regex_grep":
            result = run_regex_grep(claim, corpus)
        elif ctype == "cooccurrence_grep":
            result = run_cooccurrence_grep(claim, corpus)
        elif ctype == "form4_lookup":
            result = run_form4_lookup(claim, form4_index)
        elif ctype == "form4_footnote":
            result = run_form4_footnote(claim, form4_index)
        else:
            result = {"status": "NOT_FOUND", "reason": f"unknown claim type: {ctype}"}
        claim_results.append({"claim": claim, "result": result})
        if result["status"] == "VERIFIED":
            icon = "[OK]"
        elif result["status"] == "INDETERMINATE":
            icon = "[INDET]"
        else:
            icon = "[BLOCK]"
        # Truncate description for live display; report keeps full
        desc = claim["description"][:70]
        print(f"  {icon} {claim['id']}: {desc}")

    print("[verify_claims] Sweeping extracts/ for non-source tokens...")
    token_findings = sweep_tokens_in_extracts(
        case_dir, corpus,
        user_allowlist_normalized=user_allowlist_normalized,
        user_allowlist_lookup=user_allowlist_lookup,
        form4_auto_allowlist=form4_auto_allowlist,
    )
    n_unattr = sum(1 for f in token_findings if f["classification"] == "UNATTRIBUTED")
    n_user_allow = sum(1 for f in token_findings if f["classification"] == "ALLOWED_DERIVED")
    n_f4_allow = sum(1 for f in token_findings if f["classification"] == "ALLOWED_FORM4_DERIVED")
    print(
        f"[verify_claims]   {n_unattr} UNATTRIBUTED + {n_user_allow} ALLOWED_DERIVED + "
        f"{n_f4_allow} ALLOWED_FORM4_DERIVED tokens"
    )

    report_md = render_report(
        case_dir=case_dir,
        case_slug=case_slug,
        corpus=corpus,
        form4_index=form4_index,
        form4_auto_allowlist=form4_auto_allowlist,
        form4_auto_allowlist_disabled=args.no_form4_auto_allowlist,
        claim_results=claim_results,
        token_findings=token_findings,
        registry_report_cfg=registry["report"],
    )
    report_path = case_dir / "verify_report.md"
    report_path.write_text(report_md, encoding="utf-8")
    print(f"\n[verify_claims] Report written to: {report_path.relative_to(repo_root)}")

    n_verified = sum(1 for cr in claim_results if cr["result"]["status"] == "VERIFIED")
    n_not_found = sum(1 for cr in claim_results if cr["result"]["status"] == "NOT_FOUND")
    n_indet = sum(1 for cr in claim_results if cr["result"]["status"] == "INDETERMINATE")
    print(
        f"\nSummary: VERIFIED={n_verified}, NOT_FOUND={n_not_found}, INDETERMINATE={n_indet} | "
        f"UNATTRIBUTED tokens={n_unattr}"
    )

    return 0 if (n_not_found == 0 and n_unattr == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
