#!/usr/bin/env python3
"""Export the database as CSV files for inclusion in Metrodroid.

Output (same format the repository used before the SQLite migration):

  export/operators.csv      acquirer_id,operator_id,operator_name,terminal_info
  export/<station_file>     terminal_id,station_name,comments   (one per operator)

Usage:
  python scripts/export_csv.py                 # -> export/
  python scripts/export_csv.py --out some/dir
  python scripts/export_csv.py --db other.db

Existing files with the same names in the output directory are overwritten;
other files are left alone.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from ncmc_common import DEFAULT_DB, DEFAULT_EXPORT_DIR, connect

OPERATOR_HEADER = ["acquirer_id", "operator_id", "operator_name", "terminal_info"]
STATION_HEADER = ["terminal_id", "station_name", "comments"]


def write_csv(path: Path, header, rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def export(conn, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    operators = conn.execute("SELECT * FROM operators ORDER BY rowid").fetchall()
    written = []

    path = out_dir / "operators.csv"
    write_csv(path, OPERATOR_HEADER, (
        (f"0x{o['acquirer_id']}", f"0x{o['operator_id']}", o["name"], o["station_file"])
        for o in operators
    ))
    written.append(path)

    for op in operators:
        stations = conn.execute(
            "SELECT * FROM stations WHERE acquirer_id = ? AND operator_id = ? ORDER BY id",
            (op["acquirer_id"], op["operator_id"]),
        )
        path = out_dir / op["station_file"]
        write_csv(path, STATION_HEADER, (
            (f"0x{s['terminal_pattern']}" if s["terminal_pattern"] else "",
             s["name"], s["comments"])
            for s in stations
        ))
        written.append(path)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_EXPORT_DIR), help="output directory")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    written = export(conn, Path(args.out))
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
