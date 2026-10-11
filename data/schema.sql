-- NCMC operator / station ID database  (schema version 3)
-- ---------------------------------------------------------------------------
-- Hierarchy:
--
--   operators   one row per operator                        <- master list
--      |
--      +-- lines      many rows per operator, one per line (name + RGB colour)
--      |
--      +-- stations   many rows per operator, one per reader-ID pattern
--
--   station_lines   links stations to lines (many-to-many); a station that
--                   is on several lines is an interchange.
--
-- All IDs are stored as UPPERCASE hex text WITHOUT a "0x" prefix so they are
-- readable in any SQLite browser:
--   operators.id       6 hex digits = acquirer ID (1 byte) + operator ID (2 bytes)
--                      e.g. 0B177D = acquirer 0B, operator 177D
--   stations.reader_id 6 hex digits (3 bytes), '?' = any digit
--                      e.g. 001140 (exact) or 126??? (any gate of station 126)
--   lines.colour       6 hex digits RRGGBB (RGB), e.g. 0072BC; NULL = unknown
--
-- When several patterns match a reader ID, the one with the fewest '?' wins
-- (an exact ID overrides a mask).
--
-- The CSV files for Metrodroid are generated from these tables:
--   operators.csv   id,name,mode
--   lines.csv       id,name,operator_id,colour           (colour as #RRGGBB)
--   stations.csv    reader_id,stop_name,operator_id,line_ids
--                   (line_ids = ids from lines.csv, separated by ';')
-- The `comments` column is for maintainers only and is not exported.
--
-- Upgrading a version-2 database: python scripts/ncmc_db.py migrate
-- (applies the "lines" section below; keep it in sync with LINES_SCHEMA in
-- ncmc_common.py).
-- ---------------------------------------------------------------------------

PRAGMA foreign_keys = ON;

CREATE TABLE operators (
    id    TEXT NOT NULL PRIMARY KEY
          CHECK (length(id) = 6 AND id NOT GLOB '*[^0-9A-F]*'),
    name  TEXT NOT NULL CHECK (length(trim(name)) > 0),
    mode  TEXT NOT NULL
          CHECK (mode IN ('BUS', 'TRAIN', 'TRAM', 'METRO', 'FERRY',
                          'TICKET_MACHINE', 'VENDING_MACHINE', 'POS', 'OTHER',
                          'TROLLEYBUS', 'TOLL_ROAD', 'MONORAIL', 'CABLECAR'))
);

CREATE TABLE stations (
    id          INTEGER PRIMARY KEY,   -- row number (insertion order = export order)
    operator_id TEXT NOT NULL,
    -- NULL = station is known but its reader ID has not been decoded yet.
    reader_id   TEXT
                CHECK (reader_id IS NULL
                       OR (length(reader_id) = 6
                           AND reader_id NOT GLOB '*[^0-9A-F?]*')),
    stop_name   TEXT NOT NULL DEFAULT '',   -- '' = ID seen, name unknown
    comments    TEXT NOT NULL DEFAULT '',   -- maintainer notes; '?' marks uncertainty
    -- Stop names are NOT unique: a station may appear under several IDs
    -- (e.g. one per line).  Lines are attached through station_lines, so one
    -- row can also serve several lines at once (an interchange).
    UNIQUE (operator_id, reader_id),
    FOREIGN KEY (operator_id) REFERENCES operators (id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX idx_stations_operator ON stations (operator_id);

-- ===== lines (added in version 3) ==========================================

CREATE TABLE lines (
    id          INTEGER PRIMARY KEY,
    operator_id TEXT NOT NULL,
    name        TEXT NOT NULL COLLATE NOCASE
                CHECK (length(trim(name)) > 0),
    -- RGB as six uppercase hex digits, no '#': '0072BC'.  NULL = unknown.
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

-- A station can only be on lines of its own operator.
CREATE TRIGGER trg_station_lines_same_operator
BEFORE INSERT ON station_lines
WHEN (SELECT operator_id FROM stations WHERE id = NEW.station_id)
     IS NOT (SELECT operator_id FROM lines WHERE id = NEW.line_id)
BEGIN
    SELECT RAISE(ABORT, 'station and line belong to different operators');
END;

-- ===== views ================================================================

-- Human-friendly, read-only listing (handy in DB Browser for SQLite).
-- `lines` is e.g. 'Blue Line #0072BC, Green Line #00A651' (interchange = several).
CREATE VIEW v_stations AS
SELECT o.name                     AS operator,
       o.mode                     AS mode,
       '0x' || o.id               AS operator_id,
       '0x' || s.reader_id        AS reader_id,      -- NULL if undecoded
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
       '#' || l.colour            AS colour,         -- NULL if unknown
       (SELECT COUNT(*) FROM station_lines sl
         WHERE sl.line_id = l.id) AS station_rows,
       l.id                       AS line_row
FROM lines l
JOIN operators o ON o.id = l.operator_id
ORDER BY o.rowid, l.id;

PRAGMA user_version = 3;
