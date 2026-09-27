#!/usr/bin/env python3
"""Read a Metrodroid JSON dump of an NCMC card, look its operators and
readers up in the database, and add anything new.

Steps
  1. Load the dump and keep ONLY an allowlist of fields (PII filter, below).
  2. Show the transaction history with real date/time: the card's effective
     date is the epoch and each transaction's minutesElapsed is added to it.
  3. Look every operator and reader ID up in data/ncmc.db (wildcard patterns
     such as 323??? are honoured; the most specific match wins).
  4. For anything unknown, ask for an operator name and mode (BUS, METRO, ...)
     or a station name, propose a wildcard pattern for the reader ID, and
     save on confirmation.

An operator ID is the acquirer ID (1 byte) followed by the operator ID
(2 bytes): acquirerId 0x0B + operatorId 0x177D -> 0x0B177D.

Usage:
  python scripts/ncmc_lookup.py dump.json
  python scripts/ncmc_lookup.py dump.json --no-prompt    # report only, never writes

Only operator IDs, reader IDs, modes and the names you type are written to
the database.  Timestamps, amounts and balances are shown on screen but are
never stored.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ncmc_common import (
    ACQUIRER_NIBBLES, DEFAULT_DB, MODES, OPERATOR_NIBBLES, READER_NIBBLES,
    add_operator, add_station, connect, find_overlaps, fmt_hex, fmt_time,
    get_operator, join_operator_id, lookup_station, normalize_mode,
    normalize_pattern, parse_effective_date, pattern_matches, specificity,
    split_operator_id, suggest_pattern, to_hex, transaction_time,
)

# ===========================================================================
# 1. PII filter - ALLOWLIST
# ---------------------------------------------------------------------------
# Only the keys listed here are ever read from the dump.  Everything else
# (card number, UID/serial, holder name, balance, raw dumps, ...) is ignored,
# including fields this script has never heard of.  Add a key here only if it
# is certain not to identify a person.
# ===========================================================================
APP_KEYS = {"effectiveDate", "transactions"}
LOCATION_KEYS = {"acquirerId", "operatorId", "terminalId", "minutesElapsed"}
TXN_EXTRA_KEYS = {"transactionSequence", "amountUnits", "statusCode", "rfu"}
TXN_KEYS = LOCATION_KEYS | TXN_EXTRA_KEYS | {"entry"}
# Deliberately NOT allowed: balanceUnits, and anything not listed above.


@dataclass
class Location:
    """Where/when something happened.  IDs are normalised uppercase hex.

    ``terminal`` is the reader ID (Metrodroid's JSON calls it terminalId).
    """
    acquirer: Optional[str]
    operator: Optional[str]
    terminal: Optional[str]
    minutes: Optional[int]

    @property
    def valid(self) -> bool:
        return None not in (self.acquirer, self.operator, self.terminal)

    @property
    def operator_id(self) -> Optional[str]:
        """Acquirer + operator as one 6-digit ID, e.g. '0B177D'."""
        if self.acquirer is None or self.operator is None:
            return None
        return join_operator_id(self.acquirer, self.operator)


@dataclass
class Txn:
    index: int
    location: Location
    sequence: object = None
    amount: object = None
    status: object = None
    rfu: object = None
    entry: Optional[Location] = None


@dataclass
class SafeDump:
    effective_raw: object
    epoch: Optional[object]
    transactions: list = field(default_factory=list)
    ignored_app_keys: list = field(default_factory=list)
    ignored_txn_keys: list = field(default_factory=list)


def _scalar(value):
    """Only plain scalars survive; nested structures are dropped."""
    return value if isinstance(value, (int, str, float)) and not isinstance(value, bool) else None


def _location(raw: dict) -> Location:
    return Location(
        acquirer=to_hex(raw.get("acquirerId"), ACQUIRER_NIBBLES),
        operator=to_hex(raw.get("operatorId"), OPERATOR_NIBBLES),
        terminal=to_hex(raw.get("terminalId"), READER_NIBBLES),
        minutes=_scalar(raw.get("minutesElapsed")),
    )


def find_ncmc_application(data):
    """Locate the NCMC application: Metrodroid stores it as ["ncmc", {...}]."""
    if isinstance(data, list):
        if len(data) == 2 and data[0] == "ncmc" and isinstance(data[1], dict):
            return data[1]
        for item in data:
            found = find_ncmc_application(item)
            if found is not None:
                return found
    elif isinstance(data, dict):
        if isinstance(data.get("ncmc"), dict):
            return data["ncmc"]
        for value in data.values():
            found = find_ncmc_application(value)
            if found is not None:
                return found
    return None


def sanitize(app: dict) -> SafeDump:
    """Copy the allowlisted fields out of the NCMC application dict."""
    ignored_app = set(app) - APP_KEYS
    ignored_txn: set = set()
    transactions = []

    raw_list = app.get("transactions")
    for index, raw in enumerate(raw_list if isinstance(raw_list, list) else [], start=1):
        if not isinstance(raw, dict):
            continue
        ignored_txn |= set(raw) - TXN_KEYS
        entry = None
        if isinstance(raw.get("entry"), dict):
            ignored_txn |= set(raw["entry"]) - LOCATION_KEYS
            entry = _location(raw["entry"])
        transactions.append(Txn(
            index=index,
            location=_location(raw),
            sequence=_scalar(raw.get("transactionSequence")),
            amount=_scalar(raw.get("amountUnits")),
            status=_scalar(raw.get("statusCode")),
            rfu=_scalar(raw.get("rfu")),
            entry=entry,
        ))

    effective_raw = _scalar(app.get("effectiveDate"))
    return SafeDump(
        effective_raw=effective_raw,
        epoch=parse_effective_date(effective_raw),
        transactions=transactions,
        ignored_app_keys=sorted(ignored_app),
        ignored_txn_keys=sorted(ignored_txn - ignored_app),
    )


# ===========================================================================
# 2. History display
# ===========================================================================

def describe(conn, loc: Location) -> tuple[str, str]:
    """(operator text, stop text) for a location."""
    if not loc.valid:
        return "Invalid IDs", "?"
    operator = get_operator(conn, loc.operator_id)
    if operator is None:
        return f"Unknown operator ({fmt_hex(loc.operator_id)})", "?"
    station = lookup_station(conn, loc.operator_id, loc.terminal)
    if station is None:
        return operator["name"], "unknown station"
    return operator["name"], station["stop_name"] or "unnamed station"


def matched_pattern_note(conn, loc: Location) -> str:
    if not loc.valid:
        return ""
    station = lookup_station(conn, loc.operator_id, loc.terminal)
    if station and station["reader_id"] != loc.terminal:
        return f"  [matches 0x{station['reader_id']}]"
    return ""


def print_history(conn, dump: SafeDump, title: str = "NCMC TRANSACTION HISTORY") -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    if dump.epoch is None:
        print(f"Effective date: {dump.effective_raw!r} (missing or unreadable) - times unavailable")
    else:
        print(f"Effective date (epoch for minute counts): {dump.epoch:%Y-%m-%d}")
    print(f"Transactions: {len(dump.transactions)}")

    for txn in dump.transactions:
        loc = txn.location
        when = fmt_time(transaction_time(dump.epoch, loc.minutes))
        operator, station = describe(conn, loc)
        print(f"\n#{txn.index:<3} {when}   {operator} / {station}")
        print(f"      reader {fmt_hex(loc.terminal)}{matched_pattern_note(conn, loc)}"
              f"   (operator {fmt_hex(loc.operator_id)})")
        if txn.entry is not None:
            e_when = fmt_time(transaction_time(dump.epoch, txn.entry.minutes))
            e_operator, e_station = describe(conn, txn.entry)
            print(f"      entry:   {e_when}   {e_operator} / {e_station}"
                  f"   (reader {fmt_hex(txn.entry.terminal)})")
        extras = [f"{label} {value}" for label, value in (
            ("seq", txn.sequence), ("amount", txn.amount),
            ("status", txn.status), ("rfu", txn.rfu)) if value is not None]
        if extras:
            print("      " + " | ".join(extras))


# ===========================================================================
# 3 + 4. Discovery and interactive update
# ===========================================================================

class Prompter:
    """Thin wrapper around input() so tests can script the answers."""

    def ask(self, text: str, default: str = "") -> str:
        answer = input(text).strip()
        return answer or default


@dataclass
class Seen:
    txn_index: int
    role: str                 # "transaction" or "entry"
    when: str


@dataclass
class Report:
    new_operators: list = field(default_factory=list)    # (id, name, mode)
    new_stations: list = field(default_factory=list)     # (operator name, pattern, name)
    named_stations: list = field(default_factory=list)   # (operator name, pattern, name)
    unresolved: list = field(default_factory=list)       # (operator id, reader, reason)
    invalid: list = field(default_factory=list)          # (txn index, role, Location)

    @property
    def changed(self) -> bool:
        return bool(self.new_operators or self.new_stations or self.named_stations)


def collect_locations(dump: SafeDump):
    """Unique valid (operator id, reader) pairs in order of first appearance,
    each with where it was seen; plus the invalid records."""
    seen: dict = {}
    invalid = []
    for txn in dump.transactions:
        for role, loc in (("transaction", txn.location), ("entry", txn.entry)):
            if loc is None:
                continue
            if not loc.valid:
                invalid.append((txn.index, role, loc))
                continue
            when = fmt_time(transaction_time(dump.epoch, loc.minutes))
            seen.setdefault((loc.operator_id, loc.terminal), []).append(
                Seen(txn.index, role, when))
    return seen, invalid


def context(occurrences: list) -> str:
    parts = [f"#{o.txn_index} {o.role} {o.when}" for o in occurrences[:3]]
    if len(occurrences) > 3:
        parts.append(f"+{len(occurrences) - 3} more")
    return "; ".join(parts)


def ask_mode(prompter) -> str:
    """Show the mode menu and keep asking until a valid mode is chosen."""
    print("  Mode:")
    cells = [f"{i:>2}. {mode:<16}" for i, mode in enumerate(MODES, start=1)]
    for start in range(0, len(cells), 3):
        print("    " + "".join(cells[start:start + 3]).rstrip())
    while True:
        answer = prompter.ask("  Select mode (number or name): ")
        mode = normalize_mode(answer)
        if mode is not None:
            return mode
        print(f"  Enter a number 1-{len(MODES)} or one of the names above.")


def ask_operator(conn, prompter, operator_id, occurrences) -> Optional[tuple]:
    """Prompt for a new operator.  Returns (name, mode), or None to skip."""
    acquirer, operator = split_operator_id(operator_id)
    print(f"\nUnknown operator: {fmt_hex(operator_id)} "
          f"(acquirer 0x{acquirer}, operator 0x{operator})")
    print(f"  seen in: {context(occurrences)}")
    name = prompter.ask("  Operator name (Enter to skip): ")
    if not name:
        return None
    mode = ask_mode(prompter)
    add_operator(conn, operator_id, name, mode)
    return name, mode


def ask_pattern(conn, prompter, operator_id, reader,
                ignore_id: Optional[int] = None) -> str:
    """Prompt for the reader-ID pattern to store (must match ``reader``)."""
    suggestion = suggest_pattern(reader)
    while True:
        answer = prompter.ask(
            f"  Reader ID pattern [{fmt_hex(suggestion)}]  "
            f"(Enter = accept, 'exact' = {fmt_hex(reader)}, or type your own; "
            "? = any digit): ",
            suggestion)
        pattern = reader if answer.lower() == "exact" else normalize_pattern(answer)
        if pattern is None:
            print(f"  Need {READER_NIBBLES} hex digits, ? = wildcard (e.g. 0x323???).")
            continue
        if not pattern_matches(pattern, reader):
            print(f"  {fmt_hex(pattern)} does not match the observed {fmt_hex(reader)}.")
            continue
        overlaps = find_overlaps(conn, operator_id, pattern, ignore_id)
        if overlaps:
            for other in overlaps:
                print(f"  note: overlaps 0x{other['reader_id']} "
                      f"({other['stop_name'] or 'unnamed'})")
            if prompter.ask("  Use it anyway? [y/N]: ").lower() != "y":
                continue
        return pattern


def discover(conn, dump: SafeDump, prompter: Optional[Prompter]) -> Report:
    """Look everything up; when ``prompter`` is given, ask about the unknowns."""
    report = Report()
    seen, invalid = collect_locations(dump)
    report.invalid = invalid
    declined_operators: set = set()

    for (operator_id, reader), occurrences in seen.items():
        op_row = get_operator(conn, operator_id)

        # --- operator ----------------------------------------------------
        if op_row is None:
            if prompter is None or operator_id in declined_operators:
                report.unresolved.append((operator_id, reader, "unknown operator"))
                continue
            answer = ask_operator(conn, prompter, operator_id, occurrences)
            if answer is None:
                declined_operators.add(operator_id)
                report.unresolved.append((operator_id, reader, "operator skipped"))
                continue
            name, mode = answer
            report.new_operators.append((operator_id, name, mode))
            op_row = get_operator(conn, operator_id)

        # --- station -----------------------------------------------------
        station = lookup_station(conn, operator_id, reader)
        if station is not None and station["stop_name"]:
            continue                                   # fully known

        if prompter is None:
            reason = "unnamed station" if station else "unknown station"
            report.unresolved.append((operator_id, reader, reason))
            continue

        if station is not None:                        # known ID, no name yet
            print(f"\n{op_row['name']}: reader {fmt_hex(reader)} is in the database "
                  f"as 0x{station['reader_id']} but has no name.")
            print(f"  seen in: {context(occurrences)}")
            name = prompter.ask("  Stop name (Enter to skip): ")
            if not name:
                report.unresolved.append((operator_id, reader, "name skipped"))
                continue
            pattern = station["reader_id"]
            if specificity(pattern) == READER_NIBBLES:        # exact ID: offer a mask
                pattern = ask_pattern(conn, prompter, operator_id, reader,
                                      ignore_id=station["id"])
            try:
                conn.execute("UPDATE stations SET stop_name = ?, reader_id = ? WHERE id = ?",
                             (name, pattern, station["id"]))
            except sqlite3.IntegrityError:
                print("  Another row already uses that pattern - keeping the original ID.")
                pattern = station["reader_id"]
                conn.execute("UPDATE stations SET stop_name = ? WHERE id = ?",
                             (name, station["id"]))
            report.named_stations.append((op_row["name"], pattern, name))
            continue

        print(f"\n{op_row['name']}: new reader {fmt_hex(reader)}")
        print(f"  seen in: {context(occurrences)}")
        name = prompter.ask("  Stop name (Enter to skip): ")
        if not name:
            report.unresolved.append((operator_id, reader, "name skipped"))
            continue
        pattern = ask_pattern(conn, prompter, operator_id, reader)
        comments = prompter.ask("  Comments (optional, kept in the database only, "
                                "e.g. '? ID estimated'): ")
        add_station(conn, operator_id, pattern, name, comments)
        report.new_stations.append((op_row["name"], pattern, name))

    return report


def print_summary(report: Report, prompted: bool) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)

    def section(title, items, render):
        print(f"\n{title}: {len(items) or 'none'}")
        for item in items:
            print("  - " + render(item))

    section("New operators", report.new_operators,
            lambda i: f"{i[1]} [{i[2]}] ({fmt_hex(i[0])})")
    section("New stations", report.new_stations,
            lambda i: f"{i[0]}: {fmt_hex(i[1])} = {i[2]}")
    section("Stations named", report.named_stations,
            lambda i: f"{i[0]}: {fmt_hex(i[1])} = {i[2]}")
    section("Not resolved" + ("" if prompted else " (report-only mode)"), report.unresolved,
            lambda i: f"operator {fmt_hex(i[0])}, reader {fmt_hex(i[1])} - {i[2]}")
    section("Records with unreadable IDs", report.invalid,
            lambda i: f"transaction #{i[0]} ({i[1]})")


def print_ignored(dump: SafeDump) -> None:
    ignored = dump.ignored_app_keys + dump.ignored_txn_keys
    if ignored:
        print("Ignored (not on the allowlist, never displayed or stored): "
              + ", ".join(ignored))


# ===========================================================================
# Main
# ===========================================================================

def run(conn, dump: SafeDump, interactive: bool, prompter: Optional[Prompter] = None) -> Report:
    """Whole workflow on an already-open connection (used by main and tests)."""
    print_history(conn, dump)
    prompter = (prompter or Prompter()) if interactive else None
    report = discover(conn, dump, prompter)

    if report.changed:
        print_summary(report, prompted=True)
        answer = prompter.ask("\nSave these changes to the database? [Y/n]: ", "y").lower()
        if answer.startswith("y"):
            conn.commit()
            print("Saved.  Regenerate the CSVs with:  python scripts/export_csv.py")
            print_history(conn, dump, "UPDATED HISTORY")
        else:
            conn.rollback()
            print("Discarded - database unchanged.")
    else:
        print_summary(report, prompted=interactive)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dump", help="Metrodroid JSON export")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--no-prompt", action="store_true",
                        help="only report; never ask questions or write to the database")
    args = parser.parse_args(argv)

    path = Path(args.dump)
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        print(f"[!] Cannot read {path}: {error}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as error:
        print(f"[!] Invalid JSON: {error}", file=sys.stderr)
        return 1

    app = find_ncmc_application(data)
    if app is None:
        print("[!] No NCMC application found in this dump.", file=sys.stderr)
        return 1
    dump = sanitize(app)
    del data, app                       # nothing else from the file is used
    print_ignored(dump)

    interactive = not args.no_prompt and sys.stdin.isatty()
    if not args.no_prompt and not interactive:
        print("(stdin is not a terminal - running in report-only mode)")

    conn = connect(args.db)
    try:
        run(conn, dump, interactive)
    except (KeyboardInterrupt, EOFError):
        conn.rollback()
        print("\nInterrupted - database unchanged.")
        return 130
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
