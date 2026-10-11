#!/usr/bin/env python3
"""Upgrade an NCMC database from schema version 2 to 3 (adds lines).

Adds the `lines` and `station_lines` tables, the same-operator trigger and
the `v_lines` view, and rebuilds `v_stations` with a `lines` column.
Existing operators and stations are not touched.

Stand-alone: needs only the Python standard library.

Usage:
  python scripts/migrate_db.py                      # data/ncmc.db
  python scripts/migrate_db.py path/to/ncmc.db
  python scripts/migrate_db.py --dry-run            # check only, change nothing
  python scripts/migrate_db.py --no-backup          # skip the .bak copy

A backup copy (ncmc.db.v2.bak) is written next to the database first.
The upgrade runs in one transaction: it either completes or changes nothing.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

FROM_VERSION, TO_VERSION = 2, 3
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "ncmc.db"

# Keep identical to the lines section of data/schema.sql.
MIGRATION_SQL = """
CREATE TABLE lines (
    id          INTEGER PRIMARY KEY,
    operator_id TEXT NOT NULL,
    name        TEXT NOT NULL COLLATE NOCASE
                CHECK (length(trim(name)) > 0),
    colour      TEXT
                CHECK (colour IS NULL
                       OR (length(colour) = 6
                           AND colour NOT GLOB '*[^0-9A-F]*')),
    UNIQUE (operator_id, name),
    FOREIGN KEY (operator_id) REFERENCES operators (id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE station_lines (
    station_id INTEGER NOT NULL,
    line_id    INTEGER NOT NULL,
    PRIMARY KEY (station_id, line_id),
    FOREIGN KEY (station_id) REFERENCES stations (id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    FOREIGN KEY (line_id) REFERENCES lines (id)
        ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE INDEX idx_station_lines_line ON station_lines (line_id);

CREATE TRIGGER trg_station_lines_same_operator
BEFORE INSERT ON station_lines
WHEN (SELECT operator_id FROM stations WHERE id = NEW.station_id)
     IS NOT (SELECT operator_id FROM lines WHERE id = NEW.line_id)
BEGIN
    SELECT RAISE(ABORT, 'station and line belong to different operators');
END;

DROP VIEW IF EXISTS v_stations;
CREATE VIEW v_stations AS
SELECT o.name                     AS operator,
       o.mode                     AS mode,
       '0x' || o.id               AS operator_id,
       '0x' || s.reader_id        AS reader_id,
       s.stop_name                AS stop_name,
       s.comments                 AS comments,
       s.id                       AS station_row,
       (SELECT group_concat(label, ', ')
          FROM (SELECT l.name || COALESCE(' #' || l.colour, '') AS label
                  FROM station_lines sl
                  JOIN lines l ON l.id = sl.line_id
                 WHERE sl.station_id = s.id
                 ORDER BY sl.rowid))  AS lines
FROM stations s
JOIN operators o ON o.id = s.operator_id
ORDER BY o.rowid, s.id;

CREATE VIEW v_lines AS
SELECT o.name                     AS operator,
       '0x' || o.id               AS operator_id,
       l.name                     AS line,
       '#' || l.colour            AS colour,
       (SELECT COUNT(*) FROM station_lines sl
         WHERE sl.line_id = l.id) AS station_rows,
       l.id                       AS line_row
FROM lines l
JOIN operators o ON o.id = l.operator_id
ORDER BY o.rowid, l.id;
"""


def counts(conn) -> tuple[int, int]:
    return (conn.execute("SELECT COUNT(*) FROM operators").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db", nargs="?", default=str(DEFAULT_DB))
    parser.add_argument("--dry-run", action="store_true",
                        help="check the database and report; change nothing")
    parser.add_argument("--no-backup", action="store_true",
                        help="do not write a .v2.bak copy first")
    args = parser.parse_args(argv)

    path = Path(args.db)
    if not path.is_file():
        print(f"Database not found: {path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(path), isolation_level=None)   # manual transactions
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == TO_VERSION:
            print(f"{path} is already at schema version {TO_VERSION}; nothing to do.")
            return 0
        if version != FROM_VERSION:
            print(f"{path} is at schema version {version}; this tool only upgrades "
                  f"version {FROM_VERSION}.", file=sys.stderr)
            return 1
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"operators", "stations"} <= tables:
            print("This does not look like an NCMC database (no operators/stations "
                  "table).", file=sys.stderr)
            return 1
        if tables & {"lines", "station_lines"}:
            print("A 'lines' or 'station_lines' table already exists but the version "
                  "is 2; refusing to guess.  Inspect the database by hand.",
                  file=sys.stderr)
            return 1
        problems = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if problems != "ok":
            print(f"Database integrity check failed: {problems}", file=sys.stderr)
            return 1

        before = counts(conn)
        print(f"{path}: version {version}, {before[0]} operator(s), {before[1]} station row(s).")
        if args.dry_run:
            print(f"Dry run: would upgrade to version {TO_VERSION}.")
            return 0

        if not args.no_backup:
            backup = path.with_name(path.name + ".v2.bak")
            if backup.exists():
                print(f"Backup {backup} already exists; move it away or use --no-backup.",
                      file=sys.stderr)
                return 1
            shutil.copy2(path, backup)
            print(f"Backup written: {backup}")

        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            "BEGIN;" + MIGRATION_SQL + f"PRAGMA user_version = {TO_VERSION};COMMIT;")

        after = counts(conn)
        if after != before or conn.execute("PRAGMA foreign_key_check").fetchall():
            print("Verification failed - restore the backup.", file=sys.stderr)
            return 1
        print(f"Upgraded to version {TO_VERSION}: lines and station_lines added; "
              f"{after[0]} operator(s) and {after[1]} station row(s) unchanged.")
        print("Next: add lines with  python scripts/ncmc_db.py add-line OPERATOR NAME -C '#RRGGBB'")
        return 0
    except sqlite3.Error as error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        print(f"Upgrade failed, database unchanged: {error}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
