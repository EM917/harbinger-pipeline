-- Phase 1A Tier 6.1 funnel log schema
-- SQLite, single-host (DigitalOcean droplet), low concurrency

CREATE TABLE raw_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_week TEXT NOT NULL,           -- ISO 8601 'YYYY-WNN' (e.g., '2026-W21')
    scan_date TEXT NOT NULL,           -- ISO date scan ran
    accession_number TEXT NOT NULL UNIQUE,
    cik TEXT NOT NULL,
    ticker TEXT NOT NULL,              -- All Week 1 hits resolved; not nullable
    company_name TEXT NOT NULL,
    filing_type TEXT NOT NULL,         -- '8-K' / '8-K/A'
    filed_date TEXT NOT NULL,
    items TEXT NOT NULL,               -- JSON array of item codes
    sic_code TEXT,
    market_cap_millions REAL,          -- nullable if not preserved in digest
    body_excerpt TEXT,                 -- 8-K body relevant Item section
    exhibit_99_text TEXT,              -- EX-99.* (press release) FULL cleaned text — Exhibit 99 often
                                       -- carries the partnership substance the 8-K body omits (BLZE:
                                       -- the CoreWeave MSA press release was EX-99.1). Populated since
                                       -- TASKBOARD B3 (2026-07-12) by run_weekly_scan.py for hits
                                       -- triaged as CANDIDATES only (request-volume control), located
                                       -- via the filing-index TYPE column (EX-99 prefix) — NOT filename
                                       -- patterns, which missed BLZE's 'ex991backblazecoreweavepre.htm'
                                       -- (R8 lesson). NULL means: rejected hit / row predates B3 (NO
                                       -- historical backfill — the column fills for future scans only)
                                       -- / filing has no EX-99 / fetch failed (reason recorded in
                                       -- weekly_scans.notes + run log). A missing/failed exhibit NEVER
                                       -- alters the triage result (fetch runs AFTER the triage
                                       -- decision). Store-only: the rule engine still classifies from
                                       -- the 8-K body alone; feeding EX-99 text to the classifier is a
                                       -- future calibration decision, out of B3 scope.
    fetch_status TEXT NOT NULL,        -- 'success' / 'partial' / 'failed'
    fetch_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE triage_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_number TEXT NOT NULL REFERENCES raw_hits(accession_number),
    rejection_label TEXT,              -- NULL if is_candidate=TRUE; else one of:
                                       --   'financing_marker', 'debt_marker', 'ma_marker',
                                       --   'spac_marker', 'ip_acquisition_marker'   (Stage-1 content markers)
                                       --   'oversized_marker'                       (Tier 6.2 cap-band, >= $5B survivor)
                                       -- Reserved for the human manual-override tier, never runner-emitted:
                                       --   'ambiguous_manual_review'
                                       -- DEPRECATED — no code path emits these (Tier 6.3 reconcile, TASKBOARD B1):
                                       --   'unparseable'        STILL never emitted as a triage label — TASKBOARD B6
                                       --                        did NOT revive it. Two distinct failure modes exist,
                                       --                        neither of which is labeled here:
                                       --                        (a) body-fetch failure -> a raw_hits row IS written
                                       --                            (fetch_status='failed' + fetch_error) and triaged
                                       --                            as a candidate (is_candidate=1, cap_resolved=0);
                                       --                            a fetch failure must never silently reject a
                                       --                            potential candidate.
                                       --                        (b) FTS metadata-unparseable (no parseable ticker/CIK)
                                       --                            -> the hit cannot enter raw_hits at all
                                       --                            (ticker/cik NOT NULL). Since TASKBOARD B6 it is
                                       --                            persisted to the meta_unparseable_hits side table
                                       --                            (accession + full raw _source recoverable)
                                       --                            instead of being silently dropped, and counted
                                       --                            separately in weekly_scans.notes as
                                       --                            meta_unparseable=N (NOT folded into
                                       --                            sic_match_count). See that table's comment below.
                                       --   'missing_market_cap' superseded by cap_resolved=0 + candidate
                                       --                        (Tier 6.2 three-branch cap-band)
                                       --   'wrong_sic'          SIC filter runs client-side BEFORE storage
                                       --                        (run_weekly_scan.py); non-matching hits show up in
                                       --                        the weekly_scans raw_count->sic_match_count delta
                                       --                        but are never inserted, so no row can carry it
    is_candidate BOOLEAN NOT NULL,     -- TRUE only if surfaced for §0-§8 deep review
    triage_method TEXT NOT NULL,       -- 'manual_week1_baseline' / 'rule_engine_v1' / ...
    triage_evidence TEXT,              -- JSON or prose; for baseline: prose citing
                                       -- "EDGAR_MONITOR_CALIBRATION.md Tier 2.6 L#-L#"
    manual_override BOOLEAN DEFAULT 0, -- TRUE only when human flips rule_engine decision
    override_reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    cap_resolved INTEGER               -- Tier 6.2 cap-band: 1/0 if cap-band evaluated; NULL if
                                       -- content-rejected (cap-band never reached). Added to live
                                       -- dbs by migrate_tier62_cap_resolved.py (which appends it
                                       -- after created_at); included here so fresh inits match
                                       -- the committed db shape.
);

CREATE TABLE weekly_scans (
    scan_week TEXT PRIMARY KEY,
    scan_date TEXT NOT NULL,
    raw_count INTEGER NOT NULL,
    parsed_count INTEGER NOT NULL,
    sic_match_count INTEGER NOT NULL,
    cap_match_count INTEGER NOT NULL,
    triaged_count INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL,
    rejection_distribution TEXT,       -- JSON, e.g., {"financing_marker":8,"debt_marker":6,...}
    scan_status TEXT NOT NULL,         -- 'success' / 'partial' / 'failed'
    scan_duration_seconds REAL,
    notes TEXT
);

-- ---------------------------------------------------------------------------
-- meta_unparseable_hits (TASKBOARD B6) — SIC-matched 8-Ks whose FTS metadata
-- could not be parsed into a (ticker, CIK) pair, so they cannot enter raw_hits
-- (ticker/cik NOT NULL). Added by scripts/migrate_b6_meta_unparseable.py after
-- the silent-drop failure mode recurred 3x in the B2 backfill (W23/W24/W25 each
-- dropped 1 such hit — only a weekly_scans.notes count survived; the accession
-- was UNRECOVERABLE). This side table makes the accession + all available raw
-- metadata recoverable.
--
-- It is a SEPARATE mechanism from the DEPRECATED 'unparseable' triage label
-- (which B6 did NOT revive): these rows never enter raw_hits / triage_decisions,
-- so they never touch sic_match_count / triaged_count / candidate_count and
-- never reach the rule engine. The runner (run_weekly_scan.py) writes rows here
-- via record_unparseable_hit() and counts them separately in weekly_scans.notes
-- as meta_unparseable=N. Purely additive: creating this table alters no existing
-- row in raw_hits / triage_decisions / weekly_scans.
-- ---------------------------------------------------------------------------
CREATE TABLE meta_unparseable_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_week TEXT NOT NULL,           -- ISO 'YYYY-WNN' of the scan that surfaced it
    scan_date TEXT NOT NULL,           -- ISO date the scan ran
    accession_number TEXT NOT NULL UNIQUE,  -- recoverable key; idempotent via INSERT OR IGNORE
    trigger_item TEXT,                 -- the queried 8-K Item that matched (e.g. '1.01')
    ticker TEXT,                       -- partial parse; NULL is the whole point (why the row is here)
    cik TEXT,                          -- partial parse; may be NULL
    display_name TEXT,                 -- raw FTS display_names[0]
    sic_code TEXT,                     -- _source.sics[0] if present
    filing_type TEXT,                  -- _source.file_type / root_forms[0]
    filed_date TEXT,                   -- _source.file_date
    items TEXT,                        -- JSON array of _source.items
    raw_source_json TEXT,              -- full FTS _source dict as JSON (maximal recoverability)
    reason TEXT,                       -- why unparseable (which of ticker/cik was missing)
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_raw_hits_scan_week ON raw_hits(scan_week);
CREATE INDEX idx_raw_hits_ticker ON raw_hits(ticker);
CREATE INDEX idx_triage_accession ON triage_decisions(accession_number);
CREATE INDEX idx_triage_label ON triage_decisions(rejection_label);
CREATE INDEX idx_meta_unparseable_scan_week ON meta_unparseable_hits(scan_week);
