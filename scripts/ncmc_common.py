"""Shared helpers for the NCMC ID tools.

Everything that more than one script needs lives here:
  * ID parsing / normalisation (acquirer, operator, terminal)
  * terminal-ID wildcard patterns such as 323XXX
  * transaction timestamps (effective date + minutes elapsed)
  * SQLite access for operators and stations
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DEFAULT_DB = DATA_DIR / "ncmc.db"
SCHEMA_FILE = DATA_DIR / "schema.sql"
DEFAULT_EXPORT_DIR = ROOT / "export"

ACQUIRER_NIBBLES = 2   # 1 byte
OPERATOR_NIBBLES = 4   # 2 bytes
TERMINAL_NIBBLES = 6   # 3 bytes
WILDCARD = "X"
DEFAULT_WILDCARD_NIBBLES = 3   # 0x323149 -> 0x323XXX

FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.csv$")


# ---------------------------------------------------------------------------
# ID parsing
# ---------------------------------------------------------------------------

def parse_int(value, hex_default: bool = False) -> Optional[int]:
    """Parse an int from a JSON/CSV value.

    ints pass through; "0x1F" is hex; other strings are decimal unless
    ``hex_default`` is set (used for CSV files, where "177D" means hex).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.lower().startswith("0x"):
            return int(text[2:], 16)
        return int(text, 16 if hex_default else 10)
    except ValueError:
        return None


def to_hex(value, nibbles: int, hex_default: bool = False) -> Optional[str]:
    """Return ``value`` as fixed-width uppercase hex (no 0x), or None."""
    number = parse_int(value, hex_default)
    if number is None or not 0 <= number < 16 ** nibbles:
        return None
    return f"{number:0{nibbles}X}"


def fmt_hex(hex_digits: Optional[str]) -> str:
    return f"0x{hex_digits}" if hex_digits else "?"


# ---------------------------------------------------------------------------
# Terminal-ID patterns (wildcards)
# ---------------------------------------------------------------------------

def normalize_pattern(text) -> Optional[str]:
    """'0x323xxx' -> '323XXX'.  Returns None unless it is 6 chars of 0-9A-FX."""
    if text is None:
        return None
    value = str(text).strip().upper()
    # Strip a 0x prefix, but not the legitimate pattern "0XXXXX".
    if len(value) == TERMINAL_NIBBLES + 2 and value.startswith("0X"):
        value = value[2:]
    if re.fullmatch(r"[0-9A-FX]{%d}" % TERMINAL_NIBBLES, value):
        return value
    return None


def pattern_matches(pattern: str, terminal: str) -> bool:
    """True if ``terminal`` (6 hex digits) fits ``pattern`` (X = any digit)."""
    return len(pattern) == len(terminal) and all(
        p == WILDCARD or p == t for p, t in zip(pattern, terminal)
    )


def specificity(pattern: str) -> int:
    """Number of fixed digits; higher = more specific (exact = 6)."""
    return len(pattern) - pattern.count(WILDCARD)


def patterns_overlap(a: str, b: str) -> bool:
    """True if some terminal ID could match both patterns."""
    return all(x == WILDCARD or y == WILDCARD or x == y for x, y in zip(a, b))


def suggest_pattern(terminal: str, wildcards: int = DEFAULT_WILDCARD_NIBBLES) -> str:
    """Mask the last ``wildcards`` digits: 323149 -> 323XXX."""
    keep = TERMINAL_NIBBLES - wildcards
    return terminal[:keep] + WILDCARD * wildcards


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def parse_effective_date(value) -> Optional[datetime]:
    """Parse the card's effective date (normally YYMMDD) at 00:00.

    This is the epoch that every transaction's minutesElapsed counts from.
    """
    if value is None or isinstance(value, bool):
        return None
    text = f"{value:06d}" if isinstance(value, int) else str(value).strip()
    for fmt, length in (("%y%m%d", 6), ("%Y%m%d", 8), ("%Y-%m-%d", 10)):
        if len(text) == length:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                pass
    return None


def transaction_time(epoch: Optional[datetime], minutes) -> Optional[datetime]:
    """epoch + minutes elapsed, or None if either is missing/invalid."""
    count = parse_int(minutes)
    if epoch is None or count is None or count < 0:
        return None
    return epoch + timedelta(minutes=count)


def fmt_time(moment: Optional[datetime]) -> str:
    return moment.strftime("%Y-%m-%d %H:%M") if moment else "unknown time"


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

def connect(path=DEFAULT_DB, must_exist: bool = True) -> sqlite3.Connection:
    path = Path(path)
    if must_exist and str(path) != ":memory:" and not path.exists():
        raise SystemExit(
            f"Database not found: {path}\n"
            "Create it with:  python scripts/ncmc_db.py init"
        )
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))


def default_station_file(acquirer: str, operator: str) -> str:
    return f"{acquirer}{operator}.csv"


def normalize_station_filename(name: Optional[str]) -> Optional[str]:
    """'cmrl' -> 'cmrl.csv'.  None if empty; ValueError if unsafe."""
    if not name or not name.strip():
        return None
    name = name.strip()
    if not name.lower().endswith(".csv"):
        name += ".csv"
    if not FILENAME_RE.match(name) or name.lower() == "operators.csv":
        raise ValueError(
            f"Invalid file name {name!r}: use letters, digits, '_', '-', '.' only"
        )
    return name


def get_operator(conn, acquirer: str, operator: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM operators WHERE acquirer_id = ? AND operator_id = ?",
        (acquirer, operator),
    ).fetchone()


def add_operator(conn, acquirer: str, operator: str, name: str,
                 station_file: Optional[str] = None) -> str:
    """Insert an operator; returns the station file name used."""
    station_file = (
        normalize_station_filename(station_file)
        or default_station_file(acquirer, operator)
    )
    conn.execute(
        "INSERT INTO operators (acquirer_id, operator_id, name, station_file) "
        "VALUES (?, ?, ?, ?)",
        (acquirer, operator, name.strip(), station_file),
    )
    return station_file


def add_station(conn, acquirer: str, operator: str, pattern: Optional[str],
                name: str = "", comments: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO stations (acquirer_id, operator_id, terminal_pattern, "
        "name, comments) VALUES (?, ?, ?, ?, ?)",
        (acquirer, operator, pattern, name.strip(), comments.strip()),
    )
    return cur.lastrowid


def lookup_station(conn, acquirer: str, operator: str,
                   terminal: str) -> Optional[sqlite3.Row]:
    """Best station row for a terminal ID: the matching pattern with the
    fewest wildcards wins; ties go to the earliest row."""
    best, best_score = None, -1
    rows = conn.execute(
        "SELECT * FROM stations WHERE acquirer_id = ? AND operator_id = ? "
        "AND terminal_pattern IS NOT NULL ORDER BY id",
        (acquirer, operator),
    )
    for row in rows:
        if pattern_matches(row["terminal_pattern"], terminal):
            score = specificity(row["terminal_pattern"])
            if score > best_score:
                best, best_score = row, score
    return best


def find_overlaps(conn, acquirer: str, operator: str, pattern: str,
                  ignore_id: Optional[int] = None) -> list[sqlite3.Row]:
    """Existing stations of this operator whose pattern overlaps ``pattern``."""
    rows = conn.execute(
        "SELECT * FROM stations WHERE acquirer_id = ? AND operator_id = ? "
        "AND terminal_pattern IS NOT NULL ORDER BY id",
        (acquirer, operator),
    )
    return [
        r for r in rows
        if r["id"] != ignore_id and patterns_overlap(r["terminal_pattern"], pattern)
    ]
