#!/usr/bin/env python3
"""Manage the NCMC operator/station SQLite database.

Examples
--------
  python scripts/ncmc_db.py init
  python scripts/ncmc_db.py import-csv old_csv_dir/        # one-time migration
  python scripts/ncmc_db.py operators
  python scripts/ncmc_db.py stations "Chennai Metro"
  python scripts/ncmc_db.py add-operator 0x0B 0x177D "Chennai Metro" --file cmrl
  python scripts/ncmc_db.py add-station 0B:177D 0x001XXX "Airport"
  python scripts/ncmc_db.py add-station 0B:177D - "Mannadi" -c "?"   # ID unknown
  python scripts/ncmc_db.py edit-station 12 --name "Alandur" --pattern 0x004XXX
  python scripts/ncmc_db.py find hyderabad 0x323149
  python scripts/ncmc_db.py check

An OPERATOR argument is either "ACQ:OPERATOR" in hex (0B:177D) or part of
the operator's name (case-insensitive).
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

from ncmc_common import (
    ACQUIRER_NIBBLES, DEFAULT_DB, OPERATOR_NIBBLES, TERMINAL_NIBBLES,
    add_operator, add_station, connect, default_station_file, find_overlaps,
    fmt_hex, get_operator, init_schema, lookup_station, normalize_pattern,
    patterns_overlap, specificity, to_hex,
)

OPERATOR_ID_RE = re.compile(
    r"^(?:0x)?([0-9a-f]{1,2})\s*[:/,]\s*(?:0x)?([0-9a-f]{1,4})$", re.I
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_operator(conn, spec: str) -> sqlite3.Row:
    """Turn '0B:177D' or a (partial) name into exactly one operator row."""
    match = OPERATOR_ID_RE.match(spec.strip())
    if match:
        acquirer = to_hex(match.group(1), ACQUIRER_NIBBLES, hex_default=True)
        operator = to_hex(match.group(2), OPERATOR_NIBBLES, hex_default=True)
        row = get_operator(conn, acquirer, operator)
        if row is None:
            raise SystemExit(f"No operator {fmt_hex(acquirer)}/{fmt_hex(operator)}.")
        return row

    rows = conn.execute(
        "SELECT * FROM operators WHERE name = ? COLLATE NOCASE", (spec,)
    ).fetchall()
    if not rows:
        rows = conn.execute(
            "SELECT * FROM operators WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE",
            ("%" + spec.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchall()
    if not rows:
        raise SystemExit(f"No operator matches {spec!r}. See: ncmc_db.py operators")
    if len(rows) > 1:
        names = ", ".join(f"{r['name']} ({r['acquirer_id']}:{r['operator_id']})" for r in rows)
        raise SystemExit(f"{spec!r} is ambiguous: {names}")
    return rows[0]


def parse_pattern_arg(text: str):
    """'-' means 'terminal ID not decoded yet' (stored as NULL)."""
    if text.strip() == "-":
        return None
    pattern = normalize_pattern(text)
    if pattern is None:
        raise SystemExit(
            f"Invalid terminal pattern {text!r}: need {TERMINAL_NIBBLES} hex "
            "digits, with X as wildcard (e.g. 0x323XXX)."
        )
    return pattern


def show_overlap_warning(conn, row_op, pattern, ignore_id=None) -> None:
    for other in find_overlaps(conn, row_op["acquirer_id"], row_op["operator_id"],
                               pattern, ignore_id):
        print(f"  note: overlaps existing #{other['id']} "
              f"{fmt_hex(other['terminal_pattern'])} ({other['name'] or 'unnamed'}); "
              "when both match, the pattern with fewer X's wins", file=sys.stderr)


# ---------------------------------------------------------------------------
# CSV import (one-time migration from the old flat files)
# ---------------------------------------------------------------------------

def read_csv_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, skipinitialspace=True)
        if not reader.fieldnames:
            return
        reader.fieldnames = [(n or "").strip().lower() for n in reader.fieldnames]
        for raw in reader:
            yield {k: (v or "").strip() for k, v in raw.items() if k}


def import_csv(conn, directory: Path) -> dict:
    """Load operators.csv plus the station files it references."""
    operators_csv = directory / "operators.csv"
    if not operators_csv.exists():
        raise SystemExit(f"{operators_csv} not found.")

    stats = {"operators": 0, "stations": 0, "warnings": []}
    warn = stats["warnings"].append

    for line, row in enumerate(read_csv_rows(operators_csv), start=2):
        acquirer = to_hex(row.get("acquirer_id"), ACQUIRER_NIBBLES, hex_default=True)
        operator = to_hex(row.get("operator_id"), OPERATOR_NIBBLES, hex_default=True)
        if acquirer is None or operator is None:
            warn(f"operators.csv line {line}: invalid IDs {row!r} - skipped")
            continue
        station_file = row.get("terminal_info") or default_station_file(acquirer, operator)
        try:
            add_operator(conn, acquirer, operator, row.get("operator_name", ""), station_file)
        except (sqlite3.IntegrityError, ValueError) as error:
            warn(f"operators.csv line {line}: {acquirer}:{operator} not imported ({error})")
            continue
        stats["operators"] += 1

        path = directory / station_file
        if not path.exists():
            warn(f"{station_file}: listed for {acquirer}:{operator} but not found")
            continue
        for s_line, srow in enumerate(read_csv_rows(path), start=2):
            raw_id = srow.get("terminal_id", "")
            pattern = normalize_pattern(raw_id) if raw_id else None
            if raw_id and pattern is None:
                warn(f"{station_file} line {s_line}: bad terminal_id {raw_id!r} - skipped")
                continue
            try:
                add_station(conn, acquirer, operator, pattern,
                            srow.get("station_name", ""), srow.get("comments", ""))
            except sqlite3.IntegrityError:
                warn(f"{station_file} line {s_line}: duplicate {raw_id!r} - skipped")
                continue
            stats["stations"] += 1
    return stats


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    db = Path(args.db)
    if db.exists():
        if not args.force:
            raise SystemExit(f"{db} already exists (use --force to recreate it).")
        db.unlink()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db, must_exist=False)
    init_schema(conn)
    conn.close()
    print(f"Created {db}")


def cmd_import_csv(args):
    db = Path(args.db)
    fresh = not db.exists()
    conn = connect(db, must_exist=False)
    if fresh:
        db.parent.mkdir(parents=True, exist_ok=True)
        init_schema(conn)
    with conn:
        stats = import_csv(conn, Path(args.directory))
    for message in stats["warnings"]:
        print(f"[!] {message}", file=sys.stderr)
    print(f"Imported {stats['operators']} operator(s) and "
          f"{stats['stations']} station row(s) into {db}")


def cmd_operators(args):
    conn = connect(args.db)
    rows = conn.execute(
        "SELECT o.*, (SELECT COUNT(*) FROM stations s WHERE s.acquirer_id = o.acquirer_id "
        "AND s.operator_id = o.operator_id) AS n FROM operators o ORDER BY o.rowid"
    ).fetchall()
    print(f"{'Acquirer':<9}{'Operator':<10}{'Stations':>8}  {'File':<16}Name")
    for r in rows:
        print(f"0x{r['acquirer_id']:<7}0x{r['operator_id']:<8}{r['n']:>8}  "
              f"{r['station_file']:<16}{r['name']}")


def cmd_stations(args):
    conn = connect(args.db)
    if args.operator:
        ops = [resolve_operator(conn, args.operator)]
    else:
        ops = conn.execute("SELECT * FROM operators ORDER BY rowid").fetchall()
    for op in ops:
        print(f"\n{op['name']}  (acquirer 0x{op['acquirer_id']}, operator 0x{op['operator_id']})")
        rows = conn.execute(
            "SELECT * FROM stations WHERE acquirer_id = ? AND operator_id = ? ORDER BY id",
            (op["acquirer_id"], op["operator_id"]),
        ).fetchall()
        if not rows:
            print("  (no stations)")
        for r in rows:
            terminal = fmt_hex(r["terminal_pattern"]) if r["terminal_pattern"] else "-"
            note = f"   {r['comments']}" if r["comments"] else ""
            print(f"  #{r['id']:<4} {terminal:<10} {r['name'] or '(unnamed)'}{note}")


def cmd_add_operator(args):
    conn = connect(args.db)
    acquirer = to_hex(args.acquirer, ACQUIRER_NIBBLES, hex_default=True)
    operator = to_hex(args.operator_id, OPERATOR_NIBBLES, hex_default=True)
    if acquirer is None or operator is None:
        raise SystemExit("Acquirer ID must be 1 byte and operator ID 2 bytes (hex).")
    try:
        with conn:
            station_file = add_operator(conn, acquirer, operator, args.name, args.file)
    except sqlite3.IntegrityError as error:
        raise SystemExit(f"Could not add operator: {error}")
    except ValueError as error:
        raise SystemExit(str(error))
    print(f"Added {args.name} (0x{acquirer}/0x{operator}) -> {station_file}")


def cmd_add_station(args):
    conn = connect(args.db)
    op = resolve_operator(conn, args.operator)
    pattern = parse_pattern_arg(args.pattern)
    try:
        with conn:
            row_id = add_station(conn, op["acquirer_id"], op["operator_id"],
                                 pattern, args.name, args.comments or "")
    except sqlite3.IntegrityError:
        raise SystemExit(f"{op['name']} already has {fmt_hex(pattern)}. "
                         "Use edit-station to change it.")
    if pattern:
        show_overlap_warning(conn, op, pattern, ignore_id=row_id)
    print(f"Added #{row_id}: {fmt_hex(pattern) if pattern else '-'} {args.name}")


def cmd_edit_station(args):
    conn = connect(args.db)
    row = conn.execute("SELECT * FROM stations WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        raise SystemExit(f"No station with id {args.id}.")
    name = row["name"] if args.name is None else args.name.strip()
    comments = row["comments"] if args.comments is None else args.comments.strip()
    pattern = row["terminal_pattern"] if args.pattern is None else parse_pattern_arg(args.pattern)
    try:
        with conn:
            conn.execute(
                "UPDATE stations SET name = ?, comments = ?, terminal_pattern = ? WHERE id = ?",
                (name, comments, pattern, args.id),
            )
    except sqlite3.IntegrityError:
        raise SystemExit("Another station of this operator already uses that pattern.")
    if pattern:
        op = get_operator(conn, row["acquirer_id"], row["operator_id"])
        show_overlap_warning(conn, op, pattern, ignore_id=args.id)
    print(f"Updated #{args.id}: {fmt_hex(pattern) if pattern else '-'} {name}")


def cmd_delete_station(args):
    conn = connect(args.db)
    with conn:
        deleted = conn.execute("DELETE FROM stations WHERE id = ?", (args.id,)).rowcount
    print("Deleted." if deleted else f"No station with id {args.id}.")


def cmd_find(args):
    conn = connect(args.db)
    op = resolve_operator(conn, args.operator)
    terminal = to_hex(args.terminal, TERMINAL_NIBBLES, hex_default=True)
    if terminal is None:
        raise SystemExit("Terminal ID must be 3 bytes (6 hex digits).")
    row = lookup_station(conn, op["acquirer_id"], op["operator_id"], terminal)
    if row is None:
        print(f"{op['name']}: 0x{terminal} is not in the database.")
    else:
        print(f"{op['name']}: 0x{terminal} -> {row['name'] or '(unnamed)'} "
              f"[matched 0x{row['terminal_pattern']}, row #{row['id']}]")


def cmd_check(args):
    conn = connect(args.db)
    problems = 0
    for op in conn.execute("SELECT * FROM operators ORDER BY rowid"):
        rows = conn.execute(
            "SELECT * FROM stations WHERE acquirer_id = ? AND operator_id = ? "
            "AND terminal_pattern IS NOT NULL ORDER BY id",
            (op["acquirer_id"], op["operator_id"]),
        ).fetchall()
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if (patterns_overlap(a["terminal_pattern"], b["terminal_pattern"])
                        and specificity(a["terminal_pattern"]) == specificity(b["terminal_pattern"])
                        and a["name"] != b["name"]):
                    problems += 1
                    print(f"[!] {op['name']}: 0x{a['terminal_pattern']} ({a['name'] or 'unnamed'}) and "
                          f"0x{b['terminal_pattern']} ({b['name'] or 'unnamed'}) overlap "
                          "with equal specificity - ambiguous")
    unnamed = conn.execute("SELECT COUNT(*) FROM stations WHERE name = ''").fetchone()[0]
    undecoded = conn.execute(
        "SELECT COUNT(*) FROM stations WHERE terminal_pattern IS NULL").fetchone()[0]
    empty_ops = conn.execute(
        "SELECT name FROM operators o WHERE NOT EXISTS (SELECT 1 FROM stations s "
        "WHERE s.acquirer_id = o.acquirer_id AND s.operator_id = o.operator_id)"
    ).fetchall()
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems += len(fk)
        print(f"[!] {len(fk)} foreign-key violation(s)")
    print(f"Info: {unnamed} unnamed station row(s), {undecoded} without a decoded terminal ID, "
          f"{len(empty_ops)} operator(s) with no stations.")
    print("OK" if not problems else f"{problems} problem(s) found")
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="database file (default: data/ncmc.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create an empty database")
    p.add_argument("--force", action="store_true", help="overwrite an existing database")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import-csv", help="import the old operators.csv + station CSVs")
    p.add_argument("directory")
    p.set_defaults(func=cmd_import_csv)

    p = sub.add_parser("operators", help="list operators")
    p.set_defaults(func=cmd_operators)

    p = sub.add_parser("stations", help="list stations (optionally for one operator)")
    p.add_argument("operator", nargs="?")
    p.set_defaults(func=cmd_stations)

    p = sub.add_parser("add-operator", help="add an operator to the master list")
    p.add_argument("acquirer", help="acquirer ID, 1 byte hex (e.g. 0x0B)")
    p.add_argument("operator_id", help="operator ID, 2 bytes hex (e.g. 0x177D)")
    p.add_argument("name")
    p.add_argument("--file", help="export file name, e.g. cmrl (default: <acq><op>.csv)")
    p.set_defaults(func=cmd_add_operator)

    p = sub.add_parser("add-station", help="add a station / terminal-ID pattern")
    p.add_argument("operator", help="0B:177D or part of the operator name")
    p.add_argument("pattern", help="6 hex digits, X = wildcard (0x323XXX); '-' if not decoded yet")
    p.add_argument("name")
    p.add_argument("-c", "--comments")
    p.set_defaults(func=cmd_add_station)

    p = sub.add_parser("edit-station", help="change a station by its row id")
    p.add_argument("id", type=int)
    p.add_argument("--name")
    p.add_argument("--pattern", help="new pattern, or '-' to clear")
    p.add_argument("-c", "--comments")
    p.set_defaults(func=cmd_edit_station)

    p = sub.add_parser("delete-station", help="delete a station by its row id")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_delete_station)

    p = sub.add_parser("find", help="look up a terminal ID")
    p.add_argument("operator")
    p.add_argument("terminal", help="e.g. 0x323149")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("check", help="sanity-check the database")
    p.set_defaults(func=cmd_check)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
