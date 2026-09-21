-- NCMC operator / station ID database
-- ---------------------------------------------------------------------------
-- Hierarchy:
--
--   operators   one row per (acquirer_id, operator_id)      <- master list
--      |
--      +-- stations   many rows per operator, one per terminal-ID pattern
--
-- All IDs are stored as UPPERCASE hex text WITHOUT a "0x" prefix so they are
-- readable in any SQLite browser:
--   acquirer_id       2 hex digits  (1 byte)   e.g. 0B
--   operator_id       4 hex digits  (2 bytes)  e.g. 177D
--   terminal_pattern  6 hex digits  (3 bytes)  e.g. 001140 or 323XXX
--
-- In terminal_pattern an 'X' is a wildcard nibble: 323XXX matches 323000
-- through 323FFF, so the first 3 digits can identify a station while the
-- last 3 (gate / terminal) may be anything.  When several patterns match a
-- terminal, the one with the fewest wildcards wins (an exact ID overrides a
-- mask).
-- ---------------------------------------------------------------------------

PRAGMA foreign_keys = ON;

CREATE TABLE operators (
    acquirer_id  TEXT NOT NULL
                 CHECK (length(acquirer_id) = 2
                        AND acquirer_id NOT GLOB '*[^0-9A-F]*'),
    operator_id  TEXT NOT NULL
                 CHECK (length(operator_id) = 4
                        AND operator_id NOT GLOB '*[^0-9A-F]*'),
    name         TEXT NOT NULL,
    -- Name of the CSV generated for this operator's stations, e.g. cmrl.csv
    station_file TEXT NOT NULL UNIQUE COLLATE NOCASE
                 CHECK (station_file LIKE '%.csv'
                        AND lower(station_file) <> 'operators.csv'
                        AND station_file NOT GLOB '*[^A-Za-z0-9_.-]*'),
    PRIMARY KEY (acquirer_id, operator_id)
);

CREATE TABLE stations (
    id               INTEGER PRIMARY KEY,   -- insertion order = export order
    acquirer_id      TEXT NOT NULL,
    operator_id      TEXT NOT NULL,
    -- NULL = station is known but its terminal ID has not been decoded yet.
    terminal_pattern TEXT
                     CHECK (terminal_pattern IS NULL
                            OR (length(terminal_pattern) = 6
                                AND terminal_pattern NOT GLOB '*[^0-9A-FX]*')),
    name             TEXT NOT NULL DEFAULT '',   -- '' = ID seen, name unknown
    comments         TEXT NOT NULL DEFAULT '',   -- '?' marks uncertainty
    -- Station names are NOT unique: interchange stations legitimately appear
    -- under several IDs (one per line).
    UNIQUE (acquirer_id, operator_id, terminal_pattern),
    FOREIGN KEY (acquirer_id, operator_id)
        REFERENCES operators (acquirer_id, operator_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX idx_stations_operator ON stations (acquirer_id, operator_id);

-- Human-friendly, read-only listing (handy in DB Browser for SQLite).
CREATE VIEW v_stations AS
SELECT o.name                         AS operator,
       '0x' || o.acquirer_id          AS acquirer_id,
       '0x' || o.operator_id          AS operator_id,
       '0x' || s.terminal_pattern     AS terminal_id,   -- NULL if undecoded
       s.name                         AS station,
       s.comments                     AS comments,
       s.id                           AS station_row
FROM stations s
JOIN operators o USING (acquirer_id, operator_id)
ORDER BY o.rowid, s.id;

PRAGMA user_version = 1;
