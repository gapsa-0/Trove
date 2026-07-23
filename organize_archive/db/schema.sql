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

-- ---- Phase 4: deduplication ----------------------------------------------

CREATE TABLE IF NOT EXISTS dup_groups (
    id                INTEGER PRIMARY KEY,
    method            TEXT NOT NULL,        -- 'exact' | 'phash'
    canonical_file_id INTEGER REFERENCES files(id),
    member_count      INTEGER NOT NULL,
    size_each         INTEGER,              -- bytes per copy (exact groups)
    redundant_bytes   INTEGER,              -- (count-1)*size_each reclaimable
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dupgroups_method ON dup_groups(method);

CREATE TABLE IF NOT EXISTS dup_members (
    group_id  INTEGER NOT NULL REFERENCES dup_groups(id) ON DELETE CASCADE,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    role      TEXT NOT NULL,                -- 'canonical' | 'duplicate'
    PRIMARY KEY (group_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_dupmembers_file ON dup_members(file_id);

-- ---- Phase: geo place clustering ------------------------------------------

-- Photos within radius_m of each other, grouped into one nameable place.
-- Durable place entities. Bootstrapped once per root by geo/clusters.py, then
-- kept incrementally (assign_unplaced): new GPS files attach to the nearest place
-- or spawn a new one, and the pipeline never deletes a place or its members. A
-- `pinned` place is user-created and its coordinate is a fixed pin.
CREATE TABLE IF NOT EXISTS place_clusters (
    id            INTEGER PRIMARY KEY,
    root_id       INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    name          TEXT,
    lat           REAL NOT NULL,       -- centroid (or user-dropped pin if pinned=1)
    lon           REAL NOT NULL,
    member_count  INTEGER NOT NULL,
    pinned        INTEGER NOT NULL DEFAULT 0,  -- 1 = user-created, fixed coordinate
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_placeclusters_root ON place_clusters(root_id);

CREATE TABLE IF NOT EXISTS place_cluster_members (
    cluster_id  INTEGER NOT NULL REFERENCES place_clusters(id) ON DELETE CASCADE,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    source      TEXT DEFAULT 'auto',   -- 'auto' (GPS-derived) | 'manual' (user-attached)
    PRIMARY KEY (cluster_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_placeclustermembers_file ON place_cluster_members(file_id);

-- ---- Phase 6: faces --------------------------------------------------------

-- A person is an auto-discovered cluster of face embeddings the user can name.
-- Rebuilt by faces/cluster.py; names are preserved across rebuilds by member
-- overlap (same trick as place_clusters).
CREATE TABLE IF NOT EXISTS persons (
    id            INTEGER PRIMARY KEY,
    name          TEXT,               -- user-assigned; NULL = unnamed cluster
    cover_face_id INTEGER,            -- representative face for the card
    face_count    INTEGER NOT NULL DEFAULT 0,
    centroid      BLOB,               -- L2-normalized mean embedding (float32); for merge suggestions
    created_at    TEXT NOT NULL
);

-- One detected face. Embedding is a 128-d float32 vector, L2-normalized, so
-- cosine similarity is a dot product. Box is in ORIGINAL-image pixel coords
-- (detection may run on a downscaled copy) so face crops stay sharp.
CREATE TABLE IF NOT EXISTS faces (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    box_x      INTEGER NOT NULL,
    box_y      INTEGER NOT NULL,
    box_w      INTEGER NOT NULL,
    box_h      INTEGER NOT NULL,
    det_score  REAL,
    embedding  BLOB NOT NULL,
    person_id  INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    manual_person TEXT,               -- pinned person NAME (survives recluster); NULL = auto
    not_person INTEGER NOT NULL DEFAULT 0,  -- 1 = user marked "not a person" (doll/animal/cartoon); excluded from clustering
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_faces_file   ON faces(file_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);

-- Which files have been face-scanned. Presence means "scanned" even when
-- n_faces = 0, so extraction is resumable and a photo with no faces is not
-- retried forever (mirrors how a `dates` row means "enriched").
CREATE TABLE IF NOT EXISTS face_scan (
    file_id    INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    n_faces    INTEGER NOT NULL DEFAULT 0,
    scanned_at TEXT NOT NULL
);

-- User answers to "are these two the same person?" — durable constraints that
-- steer clustering. Anchored to specific FACE ids (stable across the persons
-- DELETE/rebuild in faces/cluster.py, unlike ephemeral person ids). 'same' =
-- must-link (union the two faces' clusters); 'different' = cannot-link (block a
-- union that would put them together). Also feeds the review UI's suggestion queue.
CREATE TABLE IF NOT EXISTS face_links (
    face_a     INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,
    face_b     INTEGER NOT NULL REFERENCES faces(id) ON DELETE CASCADE,  -- face_a < face_b
    kind       TEXT NOT NULL,          -- 'same' | 'different'
    created_at TEXT NOT NULL,
    PRIMARY KEY (face_a, face_b)
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

-- ---- Phase 7: multimodal semantic search ---------------------------------

-- Gemini Embedding 2 vectors are normalized float32 arrays stored locally.
-- One row also records skipped/failed inputs, making an indexing run resumable
-- without silently uploading the same unsupported file over and over.
CREATE TABLE IF NOT EXISTS semantic_embeddings (
    file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL,
    model         TEXT NOT NULL,
    dimensions    INTEGER NOT NULL,
    embedding     BLOB,
    status        TEXT NOT NULL,     -- indexed | skipped | error
    input_kind    TEXT,
    error         TEXT,
    indexer_version TEXT,
    indexed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_semantic_embeddings_status
    ON semantic_embeddings(status);

-- Local Gemini quota accounting. Reservations use the model's maximum input
-- size until the response supplies its actual token count, so quota checks are
-- safe even for multimodal inputs whose token count cannot be known locally.
CREATE TABLE IF NOT EXISTS semantic_api_usage (
    id           INTEGER PRIMARY KEY,
    requested_at REAL NOT NULL,
    usage_day    TEXT NOT NULL,
    token_count  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_semantic_api_usage_time
    ON semantic_api_usage(requested_at);
CREATE INDEX IF NOT EXISTS idx_semantic_api_usage_day
    ON semantic_api_usage(usage_day);
