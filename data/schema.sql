-- NCMC operator / station ID database  (schema version 2)
-- ---------------------------------------------------------------------------
-- Hierarchy:
--
--   operators   one row per operator                        <- master list
--      |
--      +-- stations   many rows per operator, one per reader-ID pattern
--
-- All IDs are stored as UPPERCASE hex text WITHOUT a "0x" prefix so they are
-- readable in any SQLite browser:
--   operators.id       6 hex digits = acquirer ID (1 byte) + operator ID (2 bytes)
--                      e.g. 0B177D = acquirer 0B, operator 177D
--   stations.reader_id 6 hex digits (3 bytes), '?' = any digit
--                      e.g. 001140 (exact) or 126??? (any gate of station 126)
--
-- When several patterns match a reader ID, the one with the fewest '?' wins
-- (an exact ID overrides a mask).
--
-- The CSV files for Metrodroid are generated from these tables:
--   operators.csv   id,name,mode
--   stations.csv    reader_id,stop_name,operator_id
-- The `comments` column is for maintainers only and is not exported.
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
    -- Stop names are NOT unique: interchange stations legitimately appear
    -- under several IDs (one per line).
    UNIQUE (operator_id, reader_id),
    FOREIGN KEY (operator_id) REFERENCES operators (id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE INDEX idx_stations_operator ON stations (operator_id);

-- Human-friendly, read-only listing (handy in DB Browser for SQLite).
CREATE VIEW v_stations AS
SELECT o.name                     AS operator,
       o.mode                     AS mode,
       '0x' || o.id               AS operator_id,
       '0x' || s.reader_id        AS reader_id,      -- NULL if undecoded
       s.stop_name                AS stop_name,
       s.comments                 AS comments,
       s.id                       AS station_row
FROM stations s
JOIN operators o ON o.id = s.operator_id
ORDER BY o.rowid, s.id;

PRAGMA user_version = 2;
