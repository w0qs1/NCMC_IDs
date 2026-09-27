#!/usr/bin/env python3
"""Export the database as two CSV files for inclusion in Metrodroid.

  export/operators.csv    id,name,mode
  export/stations.csv     reader_id,stop_name,operator_id

Example rows:
  operators.csv   0x0B177D,Chennai Metro,METRO
  stations.csv    0x001140,Airport,0x0B177D
                  0x126???,Victoria Memorial,0x043630     (? = any digit)

Station rows that Metrodroid cannot use are not exported: those without a
decoded reader ID, and those without a name.  They stay in the database and
the number skipped is reported.  The `comments` column is never exported.

Usage:
  python scripts/export_csv.py                 # -> export/
  python scripts/export_csv.py --out some/dir
  python scripts/export_csv.py --db other.db
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from ncmc_common import DEFAULT_DB, DEFAULT_EXPORT_DIR, connect

OPERATOR_HEADER = ["id", "name", "mode"]
STATION_HEADER = ["reader_id", "stop_name", "operator_id"]


def write_csv(path: Path, header, rows) -> int:
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def export(conn, out_dir: Path) -> dict:
    """Write operators.csv and stations.csv; return counts and skipped rows."""
    out_dir.mkdir(parents=True, exist_ok=True)

    operators = conn.execute("SELECT * FROM operators ORDER BY rowid").fetchall()
    n_operators = write_csv(
        out_dir / "operators.csv", OPERATOR_HEADER,
        ((f"0x{o['id']}", o["name"], o["mode"]) for o in operators),
    )

    stations = conn.execute(
        "SELECT s.* FROM stations s JOIN operators o ON o.id = s.operator_id "
        "ORDER BY o.rowid, s.id"
    ).fetchall()
    usable = [s for s in stations if s["reader_id"] and s["stop_name"]]
    n_stations = write_csv(
        out_dir / "stations.csv", STATION_HEADER,
        ((f"0x{s['reader_id']}", s["stop_name"], f"0x{s['operator_id']}") for s in usable),
    )
    return {
        "operators": n_operators,
        "stations": n_stations,
        "skipped_undecoded": sum(1 for s in stations if not s["reader_id"]),
        "skipped_unnamed": sum(1 for s in stations if s["reader_id"] and not s["stop_name"]),
        "out_dir": out_dir,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--out", default=str(DEFAULT_EXPORT_DIR), help="output directory")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    result = export(conn, Path(args.out))
    out = result["out_dir"]
    print(f"wrote {out / 'operators.csv'}  ({result['operators']} operators)")
    print(f"wrote {out / 'stations.csv'}  ({result['stations']} stations)")
    if result["skipped_undecoded"] or result["skipped_unnamed"]:
        print(f"not exported: {result['skipped_undecoded']} without a reader ID, "
              f"{result['skipped_unnamed']} without a name")
    return 0


if __name__ == "__main__":
    sys.exit(main())
