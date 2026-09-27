#!/usr/bin/env python3
"""Manage the NCMC operator/station SQLite database.

Examples
--------
  python scripts/ncmc_db.py init
  python scripts/ncmc_db.py import-csv some_dir/        # operators.csv + stations.csv
  python scripts/ncmc_db.py operators
  python scripts/ncmc_db.py stations "Chennai Metro"
  python scripts/ncmc_db.py add-operator 0x0B177D "Chennai Metro" METRO
  python scripts/ncmc_db.py add-station 0x0B177D 0x001??? "Airport"
  python scripts/ncmc_db.py add-station 0x0B177D - "Mannadi" -c "?"   # ID unknown
  python scripts/ncmc_db.py edit-station 12 --name "Alandur" --reader-id 0x004???
  python scripts/ncmc_db.py edit-operator hyderabad --mode METRO
  python scripts/ncmc_db.py find hyderabad 0x323149
  python scripts/ncmc_db.py check

An OPERATOR argument is the 6-digit operator ID (0x0B177D), or part of the
operator's name (case-insensitive).  '?' in a reader ID matches any digit.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

from ncmc_common import (
    ACQUIRER_NIBBLES, DEFAULT_DB, MODES, OPERATOR_ID_NIBBLES, OPERATOR_NIBBLES,
    READER_NIBBLES, add_operator, add_station, connect, find_overlaps, fmt_hex,
    get_operator, init_schema, join_operator_id, lookup_station, normalize_mode,
    normalize_pattern, patterns_overlap, specificity, to_hex,
)

OPERATOR_ID_RE = re.compile(r"^(?:0x)?([0-9a-f]{%d})$" % OPERATOR_ID_NIBBLES, re.I)
SPLIT_ID_RE = re.compile(          # legacy "0B:177D" form
    r"^(?:0x)?([0-9a-f]{1,2})\s*[:/,]\s*(?:0x)?([0-9a-f]{1,4})$", re.I)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_operator_id(text: str):
    """'0x0B177D' / '0B177D' / '0B:177D' -> '0B177D', else None."""
    text = text.strip()
    match = OPERATOR_ID_RE.match(text)
    if match:
        return match.group(1).upper()
    match = SPLIT_ID_RE.match(text)
    if match:
        acquirer = to_hex(match.group(1), ACQUIRER_NIBBLES, hex_default=True)
        operator = to_hex(match.group(2), OPERATOR_NIBBLES, hex_default=True)
        return join_operator_id(acquirer, operator)
    return None


def resolve_operator(conn, spec: str) -> sqlite3.Row:
    """Turn an operator ID or a (partial) name into exactly one operator row."""
    operator_id = parse_operator_id(spec)
    if operator_id:
        row = get_operator(conn, operator_id)
        if row is None:
            raise SystemExit(f"No operator {fmt_hex(operator_id)}.")
        return row

    rows = conn.execute(
        "SELECT * FROM operators WHERE name = ? COLLATE NOCASE", (spec,)
    ).fetchall()
    if not rows:
        escaped = spec.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            "SELECT * FROM operators WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE",
            (f"%{escaped}%",),
        ).fetchall()
    if not rows:
        raise SystemExit(f"No operator matches {spec!r}. See: ncmc_db.py operators")
    if len(rows) > 1:
        names = ", ".join(f"{r['name']} ({fmt_hex(r['id'])})" for r in rows)
        raise SystemExit(f"{spec!r} is ambiguous: {names}")
    return rows[0]


def parse_pattern_arg(text: str):
    """'-' means 'reader ID not decoded yet' (stored as NULL)."""
    if text.strip() == "-":
        return None
    pattern = normalize_pattern(text)
    if pattern is None:
        raise SystemExit(
            f"Invalid reader ID {text!r}: need {READER_NIBBLES} hex digits, "
            "with ? as wildcard (e.g. 0x126???)."
        )
    return pattern


def parse_mode_arg(text: str) -> str:
    mode = normalize_mode(text)
    if mode is None:
        raise SystemExit(f"Invalid mode {text!r}. Choose one of: {', '.join(MODES)}")
    return mode


def show_overlap_warning(conn, operator_id, pattern, ignore_id=None) -> None:
    for other in find_overlaps(conn, operator_id, pattern, ignore_id):
        print(f"  note: overlaps existing #{other['id']} "
              f"{fmt_hex(other['reader_id'])} ({other['stop_name'] or 'unnamed'}); "
              "when both match, the pattern with fewer ?'s wins", file=sys.stderr)


# ---------------------------------------------------------------------------
# CSV import (operators.csv + stations.csv)
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
    """Load operators.csv (id,name,mode) and stations.csv
    (reader_id,stop_name,operator_id) from ``directory``."""
    operators_csv = directory / "operators.csv"
    stations_csv = directory / "stations.csv"
    for path in (operators_csv, stations_csv):
        if not path.exists():
            raise SystemExit(f"{path} not found.")

    stats = {"operators": 0, "stations": 0, "warnings": []}
    warn = stats["warnings"].append

    for line, row in enumerate(read_csv_rows(operators_csv), start=2):
        operator_id = to_hex(row.get("id"), OPERATOR_ID_NIBBLES, hex_default=True)
        if operator_id is None:
            warn(f"operators.csv line {line}: invalid id {row.get('id')!r} - skipped")
            continue
        try:
            add_operator(conn, operator_id, row.get("name", ""), row.get("mode", ""))
        except (sqlite3.IntegrityError, ValueError) as error:
            warn(f"operators.csv line {line}: {fmt_hex(operator_id)} not imported ({error})")
            continue
        stats["operators"] += 1

    for line, row in enumerate(read_csv_rows(stations_csv), start=2):
        raw_id = row.get("reader_id", "")
        pattern = normalize_pattern(raw_id)
        if pattern is None:
            warn(f"stations.csv line {line}: bad reader_id {raw_id!r} - skipped")
            continue
        operator_id = to_hex(row.get("operator_id"), OPERATOR_ID_NIBBLES, hex_default=True)
        if operator_id is None or get_operator(conn, operator_id) is None:
            warn(f"stations.csv line {line}: unknown operator "
                 f"{row.get('operator_id')!r} - skipped")
            continue
        try:
            add_station(conn, operator_id, pattern, row.get("stop_name", ""))
        except sqlite3.IntegrityError:
            warn(f"stations.csv line {line}: duplicate {raw_id!r} for "
                 f"{fmt_hex(operator_id)} - skipped")
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
    conn = connect(db, must_exist=not fresh)
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
        "SELECT o.*, (SELECT COUNT(*) FROM stations s WHERE s.operator_id = o.id) AS n "
        "FROM operators o ORDER BY o.rowid"
    ).fetchall()
    print(f"{'ID':<10}{'Mode':<17}{'Stations':>8}  Name")
    for r in rows:
        print(f"0x{r['id']:<8}{r['mode']:<17}{r['n']:>8}  {r['name']}")


def cmd_stations(args):
    conn = connect(args.db)
    ops = ([resolve_operator(conn, args.operator)] if args.operator
           else conn.execute("SELECT * FROM operators ORDER BY rowid").fetchall())
    for op in ops:
        print(f"\n{op['name']}  ({fmt_hex(op['id'])}, {op['mode']})")
        rows = conn.execute(
            "SELECT * FROM stations WHERE operator_id = ? ORDER BY id", (op["id"],)
        ).fetchall()
        if not rows:
            print("  (no stations)")
        for r in rows:
            reader = fmt_hex(r["reader_id"]) if r["reader_id"] else "-"
            note = f"   {r['comments']}" if r["comments"] else ""
            print(f"  #{r['id']:<4} {reader:<10} {r['stop_name'] or '(unnamed)'}{note}")


def cmd_add_operator(args):
    conn = connect(args.db)
    operator_id = parse_operator_id(args.id)
    if operator_id is None:
        raise SystemExit("Operator ID must be 6 hex digits: acquirer (1 byte) + "
                         "operator (2 bytes), e.g. 0x0B177D.")
    try:
        with conn:
            add_operator(conn, operator_id, args.name, args.mode)
    except sqlite3.IntegrityError:
        raise SystemExit(f"Operator {fmt_hex(operator_id)} already exists.")
    except ValueError as error:
        raise SystemExit(str(error))
    print(f"Added {args.name} ({fmt_hex(operator_id)}, {normalize_mode(args.mode)})")


def cmd_edit_operator(args):
    conn = connect(args.db)
    op = resolve_operator(conn, args.operator)
    name = op["name"] if args.name is None else args.name.strip()
    mode = op["mode"] if args.mode is None else parse_mode_arg(args.mode)
    if not name:
        raise SystemExit("Operator name must not be empty.")
    with conn:
        conn.execute("UPDATE operators SET name = ?, mode = ? WHERE id = ?",
                     (name, mode, op["id"]))
    print(f"Updated {fmt_hex(op['id'])}: {name} ({mode})")


def cmd_add_station(args):
    conn = connect(args.db)
    op = resolve_operator(conn, args.operator)
    pattern = parse_pattern_arg(args.reader_id)
    try:
        with conn:
            row_id = add_station(conn, op["id"], pattern, args.name, args.comments or "")
    except sqlite3.IntegrityError:
        raise SystemExit(f"{op['name']} already has {fmt_hex(pattern)}. "
                         "Use edit-station to change it.")
    if pattern:
        show_overlap_warning(conn, op["id"], pattern, ignore_id=row_id)
    print(f"Added #{row_id}: {fmt_hex(pattern) if pattern else '-'} {args.name}")


def cmd_edit_station(args):
    conn = connect(args.db)
    row = conn.execute("SELECT * FROM stations WHERE id = ?", (args.id,)).fetchone()
    if row is None:
        raise SystemExit(f"No station with id {args.id}.")
    name = row["stop_name"] if args.name is None else args.name.strip()
    comments = row["comments"] if args.comments is None else args.comments.strip()
    pattern = row["reader_id"] if args.reader_id is None else parse_pattern_arg(args.reader_id)
    try:
        with conn:
            conn.execute(
                "UPDATE stations SET stop_name = ?, comments = ?, reader_id = ? WHERE id = ?",
                (name, comments, pattern, args.id),
            )
    except sqlite3.IntegrityError:
        raise SystemExit("Another station of this operator already uses that reader ID.")
    if pattern:
        show_overlap_warning(conn, row["operator_id"], pattern, ignore_id=args.id)
    print(f"Updated #{args.id}: {fmt_hex(pattern) if pattern else '-'} {name}")


def cmd_delete_station(args):
    conn = connect(args.db)
    with conn:
        deleted = conn.execute("DELETE FROM stations WHERE id = ?", (args.id,)).rowcount
    print("Deleted." if deleted else f"No station with id {args.id}.")


def cmd_find(args):
    conn = connect(args.db)
    op = resolve_operator(conn, args.operator)
    reader = to_hex(args.reader_id, READER_NIBBLES, hex_default=True)
    if reader is None:
        raise SystemExit("Reader ID must be 3 bytes (6 hex digits), e.g. 0x323149.")
    row = lookup_station(conn, op["id"], reader)
    if row is None:
        print(f"{op['name']}: 0x{reader} is not in the database.")
    else:
        print(f"{op['name']}: 0x{reader} -> {row['stop_name'] or '(unnamed)'} "
              f"[matched 0x{row['reader_id']}, row #{row['id']}]")


def cmd_check(args):
    conn = connect(args.db)
    problems = 0
    for op in conn.execute("SELECT * FROM operators ORDER BY rowid"):
        rows = conn.execute(
            "SELECT * FROM stations WHERE operator_id = ? AND reader_id IS NOT NULL "
            "ORDER BY id", (op["id"],)).fetchall()
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                if (patterns_overlap(a["reader_id"], b["reader_id"])
                        and specificity(a["reader_id"]) == specificity(b["reader_id"])
                        and a["stop_name"] != b["stop_name"]):
                    problems += 1
                    print(f"[!] {op['name']}: 0x{a['reader_id']} ({a['stop_name'] or 'unnamed'}) "
                          f"and 0x{b['reader_id']} ({b['stop_name'] or 'unnamed'}) overlap "
                          "with equal specificity - ambiguous")
    unnamed = conn.execute("SELECT COUNT(*) FROM stations WHERE stop_name = ''").fetchone()[0]
    undecoded = conn.execute(
        "SELECT COUNT(*) FROM stations WHERE reader_id IS NULL").fetchone()[0]
    empty_ops = conn.execute(
        "SELECT COUNT(*) FROM operators o WHERE NOT EXISTS "
        "(SELECT 1 FROM stations s WHERE s.operator_id = o.id)").fetchone()[0]
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems += len(fk)
        print(f"[!] {len(fk)} foreign-key violation(s)")
    print(f"Info: {unnamed} unnamed and {undecoded} undecoded station row(s) "
          f"(these are not exported); {empty_ops} operator(s) with no stations.")
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
    modes = ", ".join(MODES)

    p = sub.add_parser("init", help="create an empty database")
    p.add_argument("--force", action="store_true", help="overwrite an existing database")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import-csv", help="import operators.csv + stations.csv from a directory")
    p.add_argument("directory")
    p.set_defaults(func=cmd_import_csv)

    p = sub.add_parser("operators", help="list operators")
    p.set_defaults(func=cmd_operators)

    p = sub.add_parser("stations", help="list stations (optionally for one operator)")
    p.add_argument("operator", nargs="?")
    p.set_defaults(func=cmd_stations)

    p = sub.add_parser("add-operator", help="add an operator to the master list")
    p.add_argument("id", help="6 hex digits: acquirer (1 byte) + operator (2 bytes), e.g. 0x0B177D")
    p.add_argument("name")
    p.add_argument("mode", help=f"one of: {modes}")
    p.set_defaults(func=cmd_add_operator)

    p = sub.add_parser("edit-operator", help="change an operator's name or mode")
    p.add_argument("operator", help="0x0B177D or part of the name")
    p.add_argument("--name")
    p.add_argument("--mode", help=f"one of: {modes}")
    p.set_defaults(func=cmd_edit_operator)

    p = sub.add_parser("add-station", help="add a station / reader-ID pattern")
    p.add_argument("operator", help="0x0B177D or part of the operator name")
    p.add_argument("reader_id", help="6 hex digits, ? = wildcard (0x126???); '-' if not decoded yet")
    p.add_argument("name", help="stop name")
    p.add_argument("-c", "--comments", help="maintainer note (not exported)")
    p.set_defaults(func=cmd_add_station)

    p = sub.add_parser("edit-station", help="change a station by its row id")
    p.add_argument("id", type=int)
    p.add_argument("--name")
    p.add_argument("--reader-id", help="new reader ID pattern, or '-' to clear")
    p.add_argument("-c", "--comments")
    p.set_defaults(func=cmd_edit_station)

    p = sub.add_parser("delete-station", help="delete a station by its row id")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_delete_station)

    p = sub.add_parser("find", help="look up a reader ID")
    p.add_argument("operator")
    p.add_argument("reader_id", help="e.g. 0x323149")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("check", help="sanity-check the database")
    p.set_defaults(func=cmd_check)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
