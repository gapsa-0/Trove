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
    method            TEXT NOT NULL,        -- 'exact' | 'perceptual'
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

-- Perceptual fingerprints are deliberately kept separate from file hashes: the
-- source SHA makes a changed file automatically eligible to be fingerprinted
-- again without trusting an old visual signature.
CREATE TABLE IF NOT EXISTS perceptual_hashes (
    file_id       INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    source_sha256 TEXT NOT NULL,
    algorithm     TEXT NOT NULL,
    hash          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_perceptual_hashes_algorithm
    ON perceptual_hashes(algorithm);

-- One row per archive root: the file population covered by the last
-- successful wholesale dedup rebuild. A rebuild has no per-file backlog to
-- count the way enrich/faces/semantic do, so the GUI scheduler's "is a
-- rebuild owed" question (jobs.dedup_needed / db.dedup_needed) is answered by
-- comparing `covered_files`/`covered_max_file_id` against a fresh count of
-- present, hashed files for the root -- entirely catalog-derived, so it
-- survives an app restart instead of depending on an in-memory flag that
-- defaulted to "dirty" on every process start. No row (never rebuilt yet, or
-- explicitly invalidated after a scan/enrich that could change what dedup
-- reads) means a rebuild is owed.
CREATE TABLE IF NOT EXISTS dedup_runs (
    root_id             INTEGER PRIMARY KEY REFERENCES roots(id) ON DELETE CASCADE,
    covered_files       INTEGER NOT NULL,
    covered_max_file_id INTEGER,
    run_at              TEXT NOT NULL
);

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

-- One drag-to-merge of two places, recorded so it can be undone. Unlike
-- person_merges/pet_merges, this needs no durable must-link constraint:
-- place_clusters rows are never rebuilt wholesale (cluster_places is a
-- one-time bootstrap and assign_unplaced only ever adds), so a merge is a
-- permanent move-and-delete and undo is a true restore of the dropped row --
-- everything it takes to recreate that row byte-for-byte is captured here.
CREATE TABLE IF NOT EXISTS place_merges (
    id             INTEGER PRIMARY KEY,
    survivor_id    INTEGER NOT NULL,       -- place_clusters.id kept by the merge
    survivor_name  TEXT,                   -- survivor's name at merge time (post-merge)
    survivor_name_before TEXT,             -- ...and the name it carried BEFORE, which an
                                           -- explicit-name merge of two differently-named
                                           -- places overwrites; undo puts it back
    dropped_name   TEXT,                   -- name the losing place carried, restored on undo
    dropped_lat    REAL NOT NULL,          -- the losing place's row, in full, so undo
    dropped_lon    REAL NOT NULL,          -- can re-INSERT it exactly as it was
    dropped_pinned INTEGER NOT NULL DEFAULT 0,
    survivor_lat   REAL NOT NULL,          -- survivor's centroid BEFORE the merge
    survivor_lon   REAL NOT NULL,          -- (undo restores these verbatim)
    file_ids       TEXT NOT NULL,          -- JSON [[file_id, source], ...] the merge moved
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_place_merges_survivor ON place_merges(survivor_id);

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

-- One detected face. Embedding is a 512-d float32 vector (AdaFace ir101),
-- L2-normalized, so cosine similarity is a dot product. Box is in ORIGINAL-image
-- pixel coords (detection may run on a downscaled copy) so face crops stay sharp.
CREATE TABLE IF NOT EXISTS faces (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    box_x      INTEGER NOT NULL,
    box_y      INTEGER NOT NULL,
    box_w      INTEGER NOT NULL,
    box_h      INTEGER NOT NULL,
    det_score  REAL,
    focus_score REAL,               -- Laplacian variance on aligned 112px crop
    brightness REAL,                -- mean grayscale value (0..255)
    extreme_fraction REAL,          -- fraction near-black or near-white
    clipped_fraction REAL,          -- bbox area outside decoded image
    quality_score REAL,             -- normalized composite, 0..1
    quality_source TEXT,            -- metric algorithm/version provenance
    -- FIQA (faces/fiqa.py). fiqa_norm is the RAW AdaFace feature norm, kept so
    -- the archive can be re-tiered under new thresholds without re-embedding.
    -- quality_tier routes the face: HIGH seeds cluster cores, BORDERLINE may only
    -- attach to an existing core, LOW_QUALITY is excluded from clustering and
    -- hidden everywhere in the GUI (never deleted, so the call stays reviewable).
    fiqa_norm  REAL,
    fiqa_score REAL,                -- fiqa_norm mapped to 0..1 vs fiqa_calibration
    fiqa_source TEXT,               -- scorer algorithm/version provenance
    quality_tier TEXT,              -- HIGH | BORDERLINE | LOW_QUALITY
    embedding  BLOB NOT NULL,
    person_id  INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    manual_person TEXT,               -- pinned person NAME (survives recluster); NULL = auto
    not_person INTEGER NOT NULL DEFAULT 0,  -- 1 = user marked "not a person" (doll/animal/cartoon); excluded from clustering
    nonhuman_kind TEXT,              -- animal | toy | cartoon | false_detection
    nonhuman_source TEXT,            -- manual | animal-overlap:<model>
    -- Videos are detected on a handful of sampled keyframes, not on the file
    -- itself, so a box means nothing without knowing WHICH frame it came from.
    -- Holds the ffmpeg -ss offset ("00:00:07") the crop must be taken from;
    -- NULL for photos, where the file IS the image.
    frame_offset TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_faces_file   ON faces(file_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
-- idx_faces_tier is created in db/database.py instead, right after the migration
-- that adds faces.quality_tier: on an existing database the CREATE TABLE above
-- is a no-op, so an index over a column this file cannot add would fail here.

-- The FIQA score is a raw feature norm mapped through population statistics, so
-- those statistics must be FIXED and shared by every face -- otherwise a face's
-- tier would depend on which incremental batch it happened to be extracted in,
-- and re-running extraction would silently re-tier the archive. One row per
-- scorer model holds the calibration, written once from the first sample of
-- faces and thereafter only by an explicit recalibrate.
-- Small key/value store for facts about the archive itself rather than its
-- files. Currently one key: `faces_embedder`, the id of the model whose vectors
-- the `faces` table holds. Embeddings from two different models share no vector
-- space, so when that id stops matching the running code the archive must be
-- re-extracted — see faces/migrate_adaface.py, which reads this to decide.
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fiqa_calibration (
    model      TEXT PRIMARY KEY,   -- scorer id, e.g. 'adaface-norm-v1'
    mean       REAL NOT NULL,      -- mean of the raw norm over the sample
    std        REAL NOT NULL,      -- stddev of the raw norm over the sample
    n_faces    INTEGER NOT NULL,   -- how many faces the statistics came from
    updated_at TEXT NOT NULL
);

-- Which files have been face-scanned. Presence means "scanned" even when
-- n_faces = 0, so extraction is resumable and a photo with no faces is not
-- retried forever (mirrors how a `dates` row means "enriched").
CREATE TABLE IF NOT EXISTS face_scan (
    file_id    INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    n_faces    INTEGER NOT NULL DEFAULT 0,
    n_candidates INTEGER NOT NULL DEFAULT 0,
    rejected_score INTEGER NOT NULL DEFAULT 0,
    rejected_size INTEGER NOT NULL DEFAULT 0,
    rejected_focus INTEGER NOT NULL DEFAULT 0,
    rejected_exposure INTEGER NOT NULL DEFAULT 0,
    rejected_clipped INTEGER NOT NULL DEFAULT 0,
    rejected_nonhuman INTEGER NOT NULL DEFAULT 0,
    scanned_at TEXT NOT NULL
);

-- ---- Phase 9: pets and non-human review ---------------------------------

CREATE TABLE IF NOT EXISTS pets (
    id             INTEGER PRIMARY KEY,
    name           TEXT,
    species        TEXT NOT NULL,
    cover_detection_id INTEGER,
    detection_count INTEGER NOT NULL DEFAULT 0,
    centroid       BLOB,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS animal_detections (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    species        TEXT NOT NULL,
    box_x          INTEGER NOT NULL,
    box_y          INTEGER NOT NULL,
    box_w          INTEGER NOT NULL,
    box_h          INTEGER NOT NULL,
    det_score      REAL NOT NULL,
    embedding      BLOB,
    pet_id         INTEGER REFERENCES pets(id) ON DELETE SET NULL,
    manual_pet     TEXT,
    model_source   TEXT NOT NULL,
    frame_offset   TEXT,            -- sampled-keyframe offset for videos; NULL for photos (see faces.frame_offset)
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_animals_file ON animal_detections(file_id);
CREATE INDEX IF NOT EXISTS idx_animals_pet ON animal_detections(pet_id);
CREATE INDEX IF NOT EXISTS idx_animals_species ON animal_detections(species);

CREATE TABLE IF NOT EXISTS pet_scan (
    file_id        INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    n_animals      INTEGER NOT NULL DEFAULT 0,
    source_sha256  TEXT,
    model_source   TEXT NOT NULL,
    scanned_at     TEXT NOT NULL
);

-- Photos whose PIXELS are stored sideways while their EXIF orientation says
-- otherwise (or says nothing) — rife in this archive, where Google re-exports
-- and old camera dumps disagree about the same shot. EXIF is applied first and
-- for free; this records the leftover turn that only the detectors can see, so
-- the app can show the photo upright. Only corrections are stored: no row means
-- EXIF alone was right (or nothing was detectable). Rotation is CLOCKWISE
-- degrees to apply on top of the EXIF-corrected image, and every box in `faces`
-- and `animal_detections` for that file is already in this rotated frame.
CREATE TABLE IF NOT EXISTS orientation (
    file_id        INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    rotate_deg     INTEGER NOT NULL,     -- 90 / 180 / 270, clockwise
    source         TEXT NOT NULL,        -- which evidence resolved it
    confidence     REAL NOT NULL,
    created_at     TEXT NOT NULL
);

-- Face-like regions suppressed because they overlap an animal/toy detection.
-- These are reviewable evidence, not People and not pet identities.
CREATE TABLE IF NOT EXISTS nonhuman_detections (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    animal_detection_id INTEGER REFERENCES animal_detections(id) ON DELETE SET NULL,
    box_x          INTEGER NOT NULL,
    box_y          INTEGER NOT NULL,
    box_w          INTEGER NOT NULL,
    box_h          INTEGER NOT NULL,
    kind           TEXT NOT NULL,       -- animal | toy
    confidence     REAL NOT NULL,
    source         TEXT NOT NULL,
    source_sha256  TEXT,
    embedding      BLOB,
    det_score      REAL,
    focus_score    REAL,
    brightness     REAL,
    extreme_fraction REAL,
    clipped_fraction REAL,
    quality_score  REAL,
    quality_source TEXT,
    review_status  TEXT NOT NULL DEFAULT 'pending', -- pending | confirmed | human
    restored_face_id INTEGER REFERENCES faces(id) ON DELETE SET NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nonhuman_file ON nonhuman_detections(file_id);

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

-- User answers to "are these two pets the same animal?" — durable constraints
-- that steer cluster_pets, mirroring face_links above. Anchored to DETECTION
-- ids (not pet ids): cluster_pets DELETEs and rebuilds `pets` from scratch on
-- every detect chunk (see pets/cluster.py), so a pet id is ephemeral within a
-- run while its member detections survive across rebuilds. 'same' is the only
-- kind needed so far (no "different" review queue exists for pets yet).
CREATE TABLE IF NOT EXISTS pet_links (
    det_a      INTEGER NOT NULL REFERENCES animal_detections(id) ON DELETE CASCADE,
    det_b      INTEGER NOT NULL REFERENCES animal_detections(id) ON DELETE CASCADE, -- det_a < det_b
    kind       TEXT NOT NULL,          -- 'same' (must-link)
    created_at TEXT NOT NULL,
    PRIMARY KEY (det_a, det_b)
);

-- One drag-to-merge, recorded so it can be undone. Merging is destructive on
-- the losing side (its faces move, its persons row is deleted), so undo needs
-- the losing side written down: which faces to move back, what it was called,
-- and which face_links row the merge wrote (deleting that row is what stops
-- the next recluster from silently re-merging them).
CREATE TABLE IF NOT EXISTS person_merges (
    id           INTEGER PRIMARY KEY,
    survivor_id  INTEGER NOT NULL,        -- persons.id kept by the merge (may be reclustered away later)
    survivor_name TEXT,                   -- name at merge time, to re-find the survivor after a recluster
    dropped_name TEXT,                    -- name the losing cluster carried, restored on undo
    face_ids     TEXT NOT NULL,           -- JSON array of the faces the merge moved
    link_a       INTEGER,                 -- the face_links row this merge wrote
    link_b       INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_person_merges_survivor ON person_merges(survivor_id);

-- Same, for pets. Undo is simpler here: cluster_pets rebuilds `pets` wholesale
-- from pet_links, so deleting the recorded link and reclustering is enough --
-- no detections need moving by hand.
CREATE TABLE IF NOT EXISTS pet_merges (
    id           INTEGER PRIMARY KEY,
    survivor_id  INTEGER NOT NULL,
    survivor_name TEXT,
    dropped_name TEXT,
    det_ids      TEXT NOT NULL,           -- JSON array of the detections folded in
    link_a       INTEGER,
    link_b       INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pet_merges_survivor ON pet_merges(survivor_id);

-- "This person is in this photo" stated by hand, for media where no face was
-- detected at all (a back of a head, a video still, a group shot the detector
-- missed). Deliberately NOT a face row: there is no box and no embedding, so it
-- must never reach clustering.
--
-- Anchored by NAME as well as id, for the same reason faces.manual_person is:
-- faces/cluster.py deletes and recreates persons rows, so an id alone would rot
-- on the next pass. Only NAMED people can be tagged this way (the endpoint
-- enforces it), which is what makes the name a usable anchor -- see
-- faces/manual_tags.py's repair_manual_person_files.
CREATE TABLE IF NOT EXISTS person_files (
    person_id   INTEGER NOT NULL,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    person_name TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (person_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_person_files_file ON person_files(file_id);
CREATE INDEX IF NOT EXISTS idx_person_files_name ON person_files(person_name);

-- Same, for pets. The name anchor is load-bearing here rather than defensive:
-- cluster_pets DELETEs the whole `pets` table on every detect chunk.
CREATE TABLE IF NOT EXISTS pet_files (
    pet_id     INTEGER NOT NULL,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    pet_name   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (pet_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_pet_files_file ON pet_files(file_id);
CREATE INDEX IF NOT EXISTS idx_pet_files_name ON pet_files(pet_name);

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

-- Semantic-embedding vectors: normalized float32 arrays, computed locally by the
-- SigLIP 2 towers (organize_archive/embeddings). One row also records
-- skipped/failed inputs, making an indexing run resumable without re-deriving
-- the same unsupported file over and over. indexer_version identifies the vector
-- space: rows from an earlier embedder are not comparable to a current query,
-- which is why the Browse grid checks it before calling a file searchable.
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

-- Reserved for provider quota accounting. Existing archives may retain the
-- historical Gemini usage records; Voyage does not use this table.
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
