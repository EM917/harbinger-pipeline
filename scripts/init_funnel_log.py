#!/usr/bin/env python3
"""
init_funnel_log.py - Phase 1A Tier 6.1 — create + initialize funnel log SQLite db.

Reads `data/funnel_schema.sql`, executes against a new SQLite database file at
`data/funnel_log.db` (default), and verifies all expected tables exist
(see EXPECTED_TABLES — raw_hits / triage_decisions / weekly_scans +
meta_unparseable_hits since TASKBOARD B6).

CLI:
    python scripts/init_funnel_log.py
    python scripts/init_funnel_log.py --db-path /alt/path.db --schema-path /alt/schema.sql

Refuses to overwrite an existing database (exit 1 if --db-path already exists).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "data" / "funnel_log.db"
DEFAULT_SCHEMA_PATH = REPO_ROOT / "data" / "funnel_schema.sql"

EXPECTED_TABLES = {"raw_hits", "triage_decisions", "weekly_scans", "meta_unparseable_hits"}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        description="Initialize the Phase 1A funnel log SQLite database from schema SQL."
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"Path to SQLite database to create. Default: {DEFAULT_DB_PATH}",
    )
    parser.add_argument(
        "--schema-path",
        default=str(DEFAULT_SCHEMA_PATH),
        help=f"Path to schema SQL file. Default: {DEFAULT_SCHEMA_PATH}",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).resolve()
    schema_path = Path(args.schema_path).resolve()

    if db_path.exists():
        print(f"ERROR: database already exists at {db_path}; refusing to overwrite.", file=sys.stderr)
        print("Delete the existing file manually if you want to re-initialize.", file=sys.stderr)
        return 1

    if not schema_path.exists():
        print(f"ERROR: schema file not found: {schema_path}", file=sys.stderr)
        return 1

    schema_sql = schema_path.read_text(encoding="utf-8")

    # Ensure parent dir exists
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(schema_sql)
        conn.commit()

        # Verify all expected tables exist (EXPECTED_TABLES)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
        )
        actual_tables = {row[0] for row in cur.fetchall()}
        missing = EXPECTED_TABLES - actual_tables
        unexpected = actual_tables - EXPECTED_TABLES - {"sqlite_sequence"}  # AUTOINCREMENT creates this
        if missing:
            print(f"ERROR: expected tables not created: {sorted(missing)}", file=sys.stderr)
            return 1
        if unexpected:
            print(f"WARN: unexpected tables present: {sorted(unexpected)}", file=sys.stderr)

        print(f"Database initialized: {db_path}")
        print(f"Schema applied from: {schema_path}")
        print(f"Tables created ({len(actual_tables)}): {sorted(actual_tables)}")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
