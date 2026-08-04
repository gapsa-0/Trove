"""Builders for the fixtures nearly every test needs: a catalogue, and rows in it.

Twelve test modules each had their own ``_catalog(tmp_path)`` doing the same four
things -- connect, init_db, insert a root, loop inserting files -- with the
column list retyped every time. That is the duplication these replace.

The rule every builder follows: **default everything, and let the caller override
only the field the test is about.** A test is readable when the two lines that
differ from normal are the two lines you can see, and a builder that demands six
positional arguments buries them. So `add_file(conn)` is a complete call, and
`add_file(conn, media_type="video")` is a test about videos.

Deliberately NOT in the package: test helpers are not shipped code. Importable
from all three tiers via pytest's `pythonpath` setting (see pyproject.toml).
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

from trove.db import database as db

# One fixed timestamp for every row, so an assertion on ordering is about the
# ordering the test set up rather than about how fast the machine ran.
FIXED_TIME = "2026-01-01T00:00:00"


def make_db(tmp_path: Path, name: str = "archive.db") -> sqlite3.Connection:
    """An initialised catalogue at the current schema version, with one root.

    Returns the open connection. The root is id 1 at ``tmp_path/"photos"``,
    which is what almost every existing test hardcoded.
    """
    root_dir = tmp_path / "photos"
    root_dir.mkdir(exist_ok=True)

    conn = db.connect(tmp_path / name)
    db.init_db(conn)
    conn.execute(
        "INSERT INTO roots(id, path, added_at) VALUES(1, ?, ?)", (str(root_dir), FIXED_TIME)
    )
    conn.commit()
    return conn


def make_memory_db(root_path: str = "/archive") -> tuple[sqlite3.Connection, int]:
    """An in-memory catalogue, for tests that never need a file on disk.

    Returns ``(conn, root_id)``. Uses get_or_create_root rather than a literal
    id so the caller cannot assume 1 and be wrong later.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn, db.get_or_create_root(conn, root_path)


def add_file(
    conn: sqlite3.Connection,
    file_id: int | None = None,
    root_id: int = 1,
    rel_path: str | None = None,
    *,
    media_type: str = "image",
    size: int = 4,
    mtime: float = 0,
    ext: str | None = None,
    sha256: str | None = None,
    fast_hash: str | None = None,
    present: int = 1,
    hidden: int = 0,
) -> int:
    """Insert one row in `files` and return its id.

    ``rel_path`` defaults to ``<id>.jpg``, which keeps the UNIQUE(root_id,
    rel_path) constraint satisfied without the caller having to think about it.
    """
    if file_id is None:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM files").fetchone()
        file_id = row[0]
    if rel_path is None:
        rel_path = f"{file_id}.jpg"
    if ext is None:
        ext = rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path else None

    conn.execute(
        """INSERT INTO files(id, root_id, rel_path, ext, size, mtime, media_type,
                             fast_hash, sha256, first_seen, last_seen, present, hidden)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            file_id,
            root_id,
            rel_path,
            ext,
            size,
            mtime,
            media_type,
            fast_hash,
            sha256,
            FIXED_TIME,
            FIXED_TIME,
            present,
            hidden,
        ),
    )
    return file_id


def add_files(conn: sqlite3.Connection, count: int, **kw) -> list[int]:
    """``count`` files with default everything. Returns their ids, in order."""
    return [add_file(conn, **kw) for _ in range(count)]


def add_date(
    conn: sqlite3.Connection,
    file_id: int,
    best_datetime: str = FIXED_TIME,
    source: str = "exif",
    confidence: float = 0.9,
) -> None:
    """Give a file a resolved date, with the provenance columns the schema requires."""
    conn.execute(
        """INSERT INTO dates(file_id, best_datetime, date_source, date_confidence)
           VALUES(?, ?, ?, ?)""",
        (file_id, best_datetime, source, confidence),
    )


def add_geo(
    conn: sqlite3.Connection,
    file_id: int,
    lat: float = -41.13,
    lon: float = -71.31,
    source: str = "exif",
) -> None:
    """Give a file a location. The default is Bariloche, an actual archive location."""
    conn.execute(
        "INSERT INTO geo(file_id, lat, lon, alt, geo_source) VALUES(?, ?, ?, NULL, ?)",
        (file_id, lat, lon, source),
    )


def vector(*values: float) -> bytes:
    """A float32 embedding blob, in the packing the schema stores."""
    return struct.pack(f"<{len(values)}f", *values)


def add_person(
    conn: sqlite3.Connection,
    name: str | None = "Ana",
    person_id: int | None = None,
    faces: int = 0,
    file_id: int | None = None,
) -> int:
    """A person, optionally with ``faces`` faces already assigned to it.

    Faces need a file to hang off. Pass ``file_id`` to reuse one; otherwise each
    call creates its own, because a face's file is rarely what a person test is
    about.
    """
    if person_id is None:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM persons").fetchone()
        person_id = row[0]
    conn.execute(
        "INSERT INTO persons(id, name, face_count, created_at) VALUES(?, ?, ?, ?)",
        (person_id, name, faces, FIXED_TIME),
    )
    if faces and file_id is None:
        file_id = add_file(conn)
    for _ in range(faces):
        add_face(conn, file_id, person_id=person_id)
    return person_id


def add_face(
    conn: sqlite3.Connection,
    file_id: int,
    face_id: int | None = None,
    *,
    person_id: int | None = None,
    box: tuple[int, int, int, int] = (10, 10, 60, 60),
    det_score: float = 0.9,
    embedding: bytes | None = None,
    quality_tier: str = "HIGH",
    manual_person: str | None = None,
    not_person: int = 0,
    nonhuman_kind: str | None = None,
    nonhuman_source: str | None = None,
) -> int:
    """One detected face. ``embedding`` is NOT NULL in the schema, so it defaults.

    The review columns are here rather than left to a follow-up UPDATE because
    they are the subject of whole test files: `manual_person` is the pin that
    survives a recluster, and `not_person`/`nonhuman_kind`/`nonhuman_source`
    record a "that is a doll, not a person" answer. A fixture that sets them in
    two statements reads as though the second one is the thing being tested.
    """
    if face_id is None:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM faces").fetchone()
        face_id = row[0]
    conn.execute(
        """INSERT INTO faces(id, file_id, box_x, box_y, box_w, box_h, det_score,
                             embedding, person_id, quality_tier, manual_person,
                             not_person, nonhuman_kind, nonhuman_source, created_at)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            face_id,
            file_id,
            *box,
            det_score,
            embedding if embedding is not None else vector(1.0, 0.0),
            person_id,
            quality_tier,
            manual_person,
            not_person,
            nonhuman_kind,
            nonhuman_source,
            FIXED_TIME,
        ),
    )
    return face_id


def add_pet(
    conn: sqlite3.Connection,
    name: str | None = "Rocco",
    pet_id: int | None = None,
    *,
    species: str = "dog",
    detections: int = 0,
    file_id: int | None = None,
) -> int:
    """A pet, optionally with ``detections`` animal detections assigned to it."""
    if pet_id is None:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM pets").fetchone()
        pet_id = row[0]
    conn.execute(
        """INSERT INTO pets(id, name, species, detection_count, created_at)
           VALUES(?, ?, ?, ?, ?)""",
        (pet_id, name, species, detections, FIXED_TIME),
    )
    if detections and file_id is None:
        file_id = add_file(conn)
    for _ in range(detections):
        add_animal_detection(conn, file_id, pet_id=pet_id, species=species)
    return pet_id


def add_animal_detection(
    conn: sqlite3.Connection,
    file_id: int,
    detection_id: int | None = None,
    *,
    species: str = "dog",
    pet_id: int | None = None,
    box: tuple[int, int, int, int] = (10, 10, 60, 60),
    det_score: float = 0.9,
    embedding: bytes | None = None,
    model_source: str = "test",
) -> int:
    """One animal detection, the pets-side counterpart of `add_face`."""
    if detection_id is None:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM animal_detections").fetchone()
        detection_id = row[0]
    conn.execute(
        """INSERT INTO animal_detections(id, file_id, species, box_x, box_y, box_w, box_h,
                                         det_score, embedding, pet_id, model_source, created_at)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            detection_id,
            file_id,
            species,
            *box,
            det_score,
            embedding if embedding is not None else vector(1.0, 0.0),
            pet_id,
            model_source,
            FIXED_TIME,
        ),
    )
    return detection_id


def add_place(
    conn: sqlite3.Connection,
    name: str | None = "Bariloche",
    cluster_id: int | None = None,
    *,
    root_id: int = 1,
    lat: float = -41.13,
    lon: float = -71.31,
    file_ids: list[int] | None = None,
    pinned: int = 0,
) -> int:
    """A place cluster with its members. `member_count` is kept consistent with them."""
    if cluster_id is None:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM place_clusters").fetchone()
        cluster_id = row[0]
    members = file_ids or []
    conn.execute(
        """INSERT INTO place_clusters(id, root_id, name, lat, lon, member_count, pinned,
                                      created_at)
           VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
        (cluster_id, root_id, name, lat, lon, len(members), pinned, FIXED_TIME),
    )
    for file_id in members:
        conn.execute(
            "INSERT INTO place_cluster_members(cluster_id, file_id, source) VALUES(?, ?, 'auto')",
            (cluster_id, file_id),
        )
    return cluster_id


def make_archive(tmp_path: Path, files: dict[str, bytes] | None = None) -> Path:
    """A fake source root on disk: ``{"a/b.jpg": b"..."}`` written under tmp_path.

    Content only has to be bytes for anything that hashes or stats the file. A
    test that needs a *decodable* image should write a real one with Pillow
    instead -- these are deliberately not valid JPEGs, so a test that silently
    depends on decoding fails loudly rather than passing for the wrong reason.
    """
    root = tmp_path / "source"
    root.mkdir(exist_ok=True)
    for rel_path, content in (files or {}).items():
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return root
