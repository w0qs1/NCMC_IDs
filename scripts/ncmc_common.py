"""Shared helpers for the NCMC ID tools.

Everything that more than one script needs lives here:
  * ID parsing / normalisation (operator = acquirer + operator, reader)
  * reader-ID wildcard patterns such as 126???
  * operator modes (BUS, METRO, ...)
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
SCHEMA_VERSION = 3
COLOUR_NIBBLES = 6                                     # RGB, 3 bytes: RRGGBB

ACQUIRER_NIBBLES = 2                                   # 1 byte  (JSON: acquirerId)
OPERATOR_NIBBLES = 4                                   # 2 bytes (JSON: operatorId)
OPERATOR_ID_NIBBLES = ACQUIRER_NIBBLES + OPERATOR_NIBBLES   # 3 bytes, e.g. 0B177D
READER_NIBBLES = 6                                     # 3 bytes (JSON: terminalId)
WILDCARD = "?"
DEFAULT_WILDCARD_NIBBLES = 3                           # 0x323149 -> 0x323???

# Operator modes (must match the CHECK constraint in data/schema.sql).
MODES = (
    "BUS", "TRAIN", "TRAM", "METRO", "FERRY", "TICKET_MACHINE",
    "VENDING_MACHINE", "POS", "OTHER", "TROLLEYBUS", "TOLL_ROAD",
    "MONORAIL", "CABLECAR",
)


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


def join_operator_id(acquirer: str, operator: str) -> str:
    """('0B', '177D') -> '0B177D'."""
    return acquirer + operator


def split_operator_id(operator_id: str) -> tuple[str, str]:
    """'0B177D' -> ('0B', '177D')."""
    return operator_id[:ACQUIRER_NIBBLES], operator_id[ACQUIRER_NIBBLES:]


# ---------------------------------------------------------------------------
# Reader-ID patterns (wildcards)
# ---------------------------------------------------------------------------

def normalize_pattern(text) -> Optional[str]:
    """'0x126???' -> '126???'.  Returns None unless it is 6 chars of 0-9A-F?.

    'X' is accepted as an alias for '?' (older files used 0x126XXX).
    """
    if text is None:
        return None
    value = str(text).strip().upper()
    # Strip a 0x prefix, but not the legitimate alias-pattern "0XXXXX".
    if len(value) == READER_NIBBLES + 2 and value.startswith("0X"):
        value = value[2:]
    value = value.replace("X", WILDCARD)
    if re.fullmatch(r"[0-9A-F?]{%d}" % READER_NIBBLES, value):
        return value
    return None


def pattern_matches(pattern: str, reader: str) -> bool:
    """True if ``reader`` (6 hex digits) fits ``pattern`` (? = any digit)."""
    return len(pattern) == len(reader) and all(
        p == WILDCARD or p == r for p, r in zip(pattern, reader)
    )


def specificity(pattern: str) -> int:
    """Number of fixed digits; higher = more specific (exact = 6)."""
    return len(pattern) - pattern.count(WILDCARD)


def patterns_overlap(a: str, b: str) -> bool:
    """True if some reader ID could match both patterns."""
    return all(x == WILDCARD or y == WILDCARD or x == y for x, y in zip(a, b))


def suggest_pattern(reader: str, wildcards: int = DEFAULT_WILDCARD_NIBBLES) -> str:
    """Mask the last ``wildcards`` digits: 323149 -> 323???."""
    keep = READER_NIBBLES - wildcards
    return reader[:keep] + WILDCARD * wildcards


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def normalize_mode(text) -> Optional[str]:
    """'metro' / 'Ticket machine' / '4' -> 'METRO' / 'TICKET_MACHINE' / 'METRO'."""
    if text is None:
        return None
    value = str(text).strip().upper().replace(" ", "_").replace("-", "_")
    if value in MODES:
        return value
    if value.isdigit() and 1 <= int(value) <= len(MODES):
        return MODES[int(value) - 1]
    return None


# ---------------------------------------------------------------------------
# Line colours
# ---------------------------------------------------------------------------

def normalize_colour(text) -> Optional[str]:
    """'#1e90ff' / '0x1E90FF' / '1E90FF' -> '1E90FF'.

    Returns None unless the value is exactly six hex digits (RRGGBB).
    """
    if text is None:
        return None
    value = str(text).strip().upper()
    for prefix in ("#", "0X"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if re.fullmatch(r"[0-9A-F]{%d}" % COLOUR_NIBBLES, value):
        return value
    return None


def fmt_colour(colour: Optional[str]) -> str:
    return f"#{colour}" if colour else "no colour"


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

def connect(path=DEFAULT_DB, must_exist: bool = True,
            check_version: bool = True) -> sqlite3.Connection:
    path = Path(path)
    in_memory = str(path) == ":memory:"
    if must_exist and not in_memory and not path.exists():
        raise SystemExit(
            f"Database not found: {path}\n"
            "Create it with:  python scripts/ncmc_db.py init"
        )
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if must_exist and check_version and not in_memory:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            conn.close()
            hint = ("Upgrade it in place with:  python scripts/ncmc_db.py migrate"
                    if version == SCHEMA_VERSION - 1 else
                    "Rebuild it with 'init' + 'import-csv'.")
            raise SystemExit(
                f"{path} uses schema version {version}, but these scripts need "
                f"version {SCHEMA_VERSION}.\n{hint}"
            )
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))


# The v2 -> v3 change: lines, station_lines and the same-operator trigger.
# Must stay identical to the lines section of data/schema.sql.
LINES_SCHEMA = """
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


def migrate(conn: sqlite3.Connection) -> bool:
    """Upgrade a version-2 database to the current schema.

    Returns False if it was already current.  Existing data is untouched.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return False
    if version != SCHEMA_VERSION - 1:
        raise SystemExit(f"Cannot migrate from schema version {version}.")
    conn.executescript(
        "BEGIN;" + LINES_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
    )
    return True


def get_operator(conn, operator_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM operators WHERE id = ?", (operator_id,)).fetchone()


def add_operator(conn, operator_id: str, name: str, mode: str) -> None:
    """Insert an operator.  Raises ValueError for a bad name or mode."""
    clean_mode = normalize_mode(mode)
    if clean_mode is None:
        raise ValueError(f"Invalid mode {mode!r}. Choose one of: {', '.join(MODES)}")
    if not name or not name.strip():
        raise ValueError("Operator name must not be empty")
    conn.execute(
        "INSERT INTO operators (id, name, mode) VALUES (?, ?, ?)",
        (operator_id, name.strip(), clean_mode),
    )


def add_station(conn, operator_id: str, pattern: Optional[str],
                name: str = "", comments: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO stations (operator_id, reader_id, stop_name, comments) "
        "VALUES (?, ?, ?, ?)",
        (operator_id, pattern, name.strip(), comments.strip()),
    )
    return cur.lastrowid


def lookup_station(conn, operator_id: str, reader: str) -> Optional[sqlite3.Row]:
    """Best station row for a reader ID: the matching pattern with the fewest
    wildcards wins; ties go to the earliest row."""
    best, best_score = None, -1
    rows = conn.execute(
        "SELECT * FROM stations WHERE operator_id = ? AND reader_id IS NOT NULL "
        "ORDER BY id",
        (operator_id,),
    )
    for row in rows:
        if pattern_matches(row["reader_id"], reader):
            score = specificity(row["reader_id"])
            if score > best_score:
                best, best_score = row, score
    return best


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------

def add_line(conn, operator_id: str, name: str, colour=None,
             line_id: Optional[int] = None) -> int:
    """Insert a line and return its id.

    ``colour`` is any form normalize_colour accepts (or None/'' for no colour).
    Raises ValueError for a bad name/colour, sqlite3.IntegrityError for a
    duplicate name within the operator.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("Line name must not be empty")
    clean = None
    if colour not in (None, ""):
        clean = normalize_colour(colour)
        if clean is None:
            raise ValueError(
                f"Invalid colour {colour!r}: use six hex digits (RRGGBB), e.g. #1E90FF")
    cur = conn.execute(
        "INSERT INTO lines (id, operator_id, name, colour) VALUES (?, ?, ?, ?)",
        (line_id, operator_id, name, clean),
    )
    return cur.lastrowid


def get_line(conn, line_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM lines WHERE id = ?", (line_id,)).fetchone()


def operator_lines(conn, operator_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM lines WHERE operator_id = ? ORDER BY id", (operator_id,)
    ).fetchall()


def find_line(conn, operator_id: str, spec: str,
              partial: bool = True) -> list[sqlite3.Row]:
    """Lines of an operator matching ``spec`` by name (case-insensitive).

    An exact name match always wins; otherwise, if ``partial`` is set, every
    line whose name contains ``spec`` is returned.
    """
    spec = spec.strip()
    rows = conn.execute(
        "SELECT * FROM lines WHERE operator_id = ? AND name = ? COLLATE NOCASE",
        (operator_id, spec),
    ).fetchall()
    if rows or not partial or not spec:
        return rows
    escaped = spec.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return conn.execute(
        "SELECT * FROM lines WHERE operator_id = ? "
        "AND name LIKE ? ESCAPE '\\' COLLATE NOCASE ORDER BY id",
        (operator_id, f"%{escaped}%"),
    ).fetchall()


def station_lines(conn, station_id: int) -> list[sqlite3.Row]:
    """Lines serving a station, in the order they were attached."""
    return conn.execute(
        "SELECT l.* FROM station_lines sl JOIN lines l ON l.id = sl.line_id "
        "WHERE sl.station_id = ? ORDER BY sl.rowid",
        (station_id,),
    ).fetchall()


def link_station_line(conn, station_id: int, line_id: int) -> bool:
    """Attach a line to a station.  False if it was already attached.

    A line of a different operator raises sqlite3.IntegrityError (trigger).
    """
    exists = conn.execute(
        "SELECT 1 FROM station_lines WHERE station_id = ? AND line_id = ?",
        (station_id, line_id)).fetchone()
    if exists:
        return False
    conn.execute("INSERT INTO station_lines (station_id, line_id) VALUES (?, ?)",
                 (station_id, line_id))
    return True


def unlink_station_line(conn, station_id: int, line_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM station_lines WHERE station_id = ? AND line_id = ?",
        (station_id, line_id))
    return cur.rowcount > 0


def format_lines(lines) -> str:
    """'Blue #0000FF, Green #00FF00' (colour omitted when unset)."""
    return ", ".join(
        f"{l['name']} #{l['colour']}" if l["colour"] else l["name"] for l in lines)


def find_overlaps(conn, operator_id: str, pattern: str,
                  ignore_id: Optional[int] = None) -> list[sqlite3.Row]:
    """Existing stations of this operator whose pattern overlaps ``pattern``."""
    rows = conn.execute(
        "SELECT * FROM stations WHERE operator_id = ? AND reader_id IS NOT NULL "
        "ORDER BY id",
        (operator_id,),
    )
    return [
        r for r in rows
        if r["id"] != ignore_id and patterns_overlap(r["reader_id"], pattern)
    ]
