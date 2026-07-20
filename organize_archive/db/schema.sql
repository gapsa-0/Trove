-- organize_archive schema (v1)
-- Core tables for Phases 1-2. Later phases (dates, geo, phash, dedup, faces,
-- embeddings) add their own tables via migrations. Every derived fact should
-- carry a *_source / confidence column when it lands.

CREATE TABLE IF NOT EXISTS roots (
    id        INTEGER PRIMARY KEY,
    path      TEXT NOT NULL UNIQUE,
    added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    root_id     INTEGER NOT NULL REFERENCES roots(id),
    rel_path    TEXT NOT NULL,          -- path relative to the root
    ext         TEXT,                   -- lowercased, no dot
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,          -- filesystem modified time (epoch seconds)
    media_type  TEXT NOT NULL,          -- image/video/audio/document/archive/other
    fast_hash   TEXT,                   -- cheap partial-content prefilter
    sha256      TEXT,                   -- full content hash (NULL until hashed)
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    present     INTEGER NOT NULL DEFAULT 1,  -- 0 = not seen in latest scan
    hidden      INTEGER NOT NULL DEFAULT 0,  -- 1 = non-canonical duplicate (Phase 4)
    UNIQUE (root_id, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_files_sha256     ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_fast_hash  ON files(fast_hash);
CREATE INDEX IF NOT EXISTS idx_files_media_type ON files(media_type);
CREATE INDEX IF NOT EXISTS idx_files_mtime      ON files(mtime);
CREATE INDEX IF NOT EXISTS idx_files_present    ON files(present);

CREATE TABLE IF NOT EXISTS scan_runs (
    id             INTEGER PRIMARY KEY,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    roots          TEXT,           -- JSON list of root paths scanned
    files_seen     INTEGER DEFAULT 0,
    files_new      INTEGER DEFAULT 0,
    files_updated  INTEGER DEFAULT 0,
    bytes_hashed   INTEGER DEFAULT 0
);

-- ---- Phase 3: metadata & dates -------------------------------------------

-- Embedded/derived media properties (from exiftool).
CREATE TABLE IF NOT EXISTS media_meta (
    file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    width         INTEGER,
    height        INTEGER,
    duration_s    REAL,
    make          TEXT,
    model         TEXT,
    orientation   INTEGER,
    mime          TEXT,
    detected_type TEXT             -- content-sniffed type (fixes extensionless files)
);

-- One resolved best datetime per file, with provenance.
CREATE TABLE IF NOT EXISTS dates (
    file_id        INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    best_datetime  TEXT,           -- ISO 8601 wall-clock ("YYYY-MM-DDTHH:MM:SS")
    date_source    TEXT,           -- takeout_json | exif | filename | mtime
    date_confidence REAL           -- 0..1
);
CREATE INDEX IF NOT EXISTS idx_dates_best   ON dates(best_datetime);
CREATE INDEX IF NOT EXISTS idx_dates_source ON dates(date_source);

-- GPS location per file, with provenance.
CREATE TABLE IF NOT EXISTS geo (
    file_id     INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    lat         REAL,
    lon         REAL,
    alt         REAL,
    geo_source  TEXT               -- takeout_json | exif
);

-- Matched Google Takeout sidecar, with the fields we consume.
CREATE TABLE IF NOT EXISTS takeout_sidecar (
    file_id           INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    json_rel_path     TEXT,
    title             TEXT,
    description       TEXT,
    taken_time        INTEGER,     -- photoTakenTime epoch (UTC)
    match_method      TEXT,        -- which candidate rule matched
    match_confidence  REAL
);
