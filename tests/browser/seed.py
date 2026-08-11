"""What is in the archive every browser test drives.

Split out of ``conftest.py``, which had grown to hold two unrelated jobs: how to
get a browser and a server (the fixtures) and what to put in front of them (this
file). They change for different reasons -- a new screen needs a row seeded here
and nothing there -- and only one of them is worth reading when a test's data
looks wrong.

Every screen needs content: one rendering its empty state is indistinguishable,
from the outside, from one whose renderer threw, and "the empty state renders"
is not what this tier is checking.
"""

from __future__ import annotations

import os
from pathlib import Path

import factories

from trove.db import database as db

# One more than the Library grid's own page size (GRID_PAGE_SIZE = 120 in
# static/js/library.js), so there is a second page to fetch and paging can be
# tested at all. Every test pays for these, but they are 32x32 JPEGs -- the
# archive rebuild stays a fraction of the browser start it sits behind. If
# GRID_PAGE_SIZE ever changes, this has to stay above it or the paging test
# silently stops testing paging.
MEDIA_COUNT = 130


def write_jpeg(path: Path, color: tuple[int, int, int] = (120, 140, 160)) -> None:
    """A real, small, decodable JPEG, so thumbnail serving takes its real path."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 32), color).save(path, "JPEG")


def seed_documents(conn, root_id: int, source_dir: Path, photo_id: int) -> int:
    """Text already read out of two documents *and* one photograph, so Browse
    has a second result group and that group holds more than one kind of file.
    Returns the first document's id.

    The photograph is not decoration. Both text features write into the same
    passages, so this group is mixed on any archive running both -- and
    while the fixture held documents alone, a thumbnail with nothing bounding its
    height went unseen here: it grew to fill the column and stretched every
    document beside it into a cell four times the size of its own text.

    Written straight into the tables rather than run through the stage: what
    this tier checks is the screen, and a real extraction pass would put PDF
    parsing in front of every browser test.
    """
    first = 0
    for name, page, body in (
        # English on purpose: a Spanish query sends the composer through the
        # local translator, which downloads 23 MB on first use. What this tier
        # checks is the screen, not the model behind the other group.
        ("lease.pdf", 2, "The lease agreement was signed in Bariloche"),
        ("receipt.txt", None, "Receipt for the lease, paid in March"),
    ):
        # An explicit hash: doc_text.source_sha256 is NOT NULL, and it is the
        # anchor that says the text matches the bytes it was read from.
        digest = f"sha-{name}"
        fid = factories.add_file(
            conn,
            root_id=root_id,
            rel_path=name,
            media_type="document",
            ext=name.rsplit(".", 1)[1],
            sha256=digest,
        )
        (source_dir / name).write_text(body, encoding="utf-8")
        conn.execute(
            """INSERT INTO doc_text(file_id, source_sha256, wanted, extractor, status,
                                    chars, n_chunks, text_version, extracted_at)
               VALUES(?, ?, 'documents', 'pdf-text', 'extracted', ?, 1, 'doctext-v1', ?)""",
            (fid, digest, len(body), factories.FIXED_TIME),
        )
        cur = conn.execute(
            "INSERT INTO doc_chunks(file_id, ordinal, page_first, page_last, chars) "
            "VALUES(?, 0, ?, ?, ?)",
            (fid, page, page, len(body)),
        )
        conn.execute("INSERT INTO doc_chunk_fts(rowid, text) VALUES(?, ?)", (cur.lastrowid, body))
        first = first or fid

    # ...and the writing read off a photograph. It shares "receipt" with the
    # text file above, so one search can return both kinds of file and the group
    # can be asserted on for holding them at the same size; it deliberately
    # shares nothing with "lease", which is what the searches that count only
    # documents look for.
    photo_text = "RECEIPT / RECIBO DE COMPRA -- TOTAL 43,50 EUR"
    # The seeded photographs carry no hash, and `doc_text.source_sha256` is the
    # anchor saying the text matches the bytes it was read from, so give this
    # one the hash a real scan would have written.
    digest = f"sha-ocr-{photo_id}"
    conn.execute("UPDATE files SET sha256=? WHERE id=?", (digest, photo_id))
    conn.execute(
        """INSERT INTO doc_text(file_id, source_sha256, wanted, extractor, status,
                                chars, n_chunks, text_version, extracted_at)
           VALUES(?, ?, 'ocr', 'ocr', 'extracted', ?, 1, 'doctext-v1', ?)""",
        (photo_id, digest, len(photo_text), factories.FIXED_TIME),
    )
    cur = conn.execute(
        "INSERT INTO doc_chunks(file_id, ordinal, page_first, page_last, chars) "
        "VALUES(?, 0, NULL, NULL, ?)",
        (photo_id, len(photo_text)),
    )
    conn.execute("INSERT INTO doc_chunk_fts(rowid, text) VALUES(?, ?)", (cur.lastrowid, photo_text))

    return first


def seed_duplicates(conn, canonical: int, copy: int) -> int:
    """One duplicate group, so Duplicates has a row rather than its empty state.

    A visual match rather than a byte-identical pair -- distinct sha256s, so the
    copy's tile renders the "Visual match" tag, which is the case with something
    to draw. Returns the group id.
    """
    for fid, sha in ((canonical, "d" * 64), (copy, "e" * 64)):
        conn.execute("UPDATE files SET sha256=? WHERE id=?", (sha, fid))
    cur = conn.execute(
        """INSERT INTO dup_groups(method, canonical_file_id, member_count,
                                  size_each, redundant_bytes, created_at)
           VALUES('perceptual', ?, 2, 4, 4, ?)""",
        (canonical, factories.FIXED_TIME),
    )
    group_id = cur.lastrowid
    for fid, role in ((canonical, "canonical"), (copy, "duplicate")):
        conn.execute(
            "INSERT INTO dup_members(group_id, file_id, role) VALUES(?, ?, ?)",
            (group_id, fid, role),
        )
    return group_id


# A folder name that is hostile to markup built by string concatenation, and an
# entirely ordinary thing to have: Google Photos album names become Takeout
# folder names, and people put quotation marks in them. The Duplicates tile used
# to interpolate this straight into `title="..."`, which ended the attribute
# early, turned the rest into junk attributes, swallowed the tile's onclick and
# printed the leftover as visible text.
#
# Windows forbids `"` and `<` in a path, so a developer running this tier there
# gets the half of the name its filesystem allows. `&` is legal everywhere and
# is the character a real archive is most likely to carry.
HOSTILE_FOLDER = 'Fotos de "Mama" & Papa <2015>' if os.name != "nt" else "Fotos de Mama & Papa"


def seed_hostile_names(conn, root_id: int, source_dir: Path, canonical: int) -> int:
    """A second duplicate group whose copy lives somewhere awkwardly named.

    Its own group rather than a rename of the existing pair, so every test that
    already counts on that pair keeps counting on the same thing.
    """
    copy = factories.add_file(
        conn,
        root_id=root_id,
        rel_path=f'{HOSTILE_FOLDER}/retrato "de la abuela".jpg'
        if os.name != "nt"
        else f"{HOSTILE_FOLDER}/retrato de la abuela.jpg",
        sha256="f" * 64,
    )
    write_jpeg(
        source_dir / conn.execute("SELECT rel_path FROM files WHERE id=?", (copy,)).fetchone()[0]
    )
    conn.execute("UPDATE files SET sha256=? WHERE id=?", ("f" * 64, canonical))
    cur = conn.execute(
        """INSERT INTO dup_groups(method, canonical_file_id, member_count,
                                  size_each, redundant_bytes, created_at)
           VALUES('exact', ?, 2, 4, 4, ?)""",
        (canonical, factories.FIXED_TIME),
    )
    group_id = cur.lastrowid
    for fid, role in ((canonical, "canonical"), (copy, "duplicate")):
        conn.execute(
            "INSERT INTO dup_members(group_id, file_id, role) VALUES(?, ?, ?)",
            (group_id, fid, role),
        )
    return copy


def seed(conn, root_id: int, source_dir: Path) -> dict:
    """A small archive with something on every screen.

    Every screen needs content: one rendering its empty state is
    indistinguishable, from the outside, from one whose renderer threw -- and
    "the empty state renders" is not what this tier is checking.
    """
    ids: dict[str, int] = {}

    def _file(rel_path: str, **kw) -> int:
        fid = factories.add_file(conn, root_id=root_id, rel_path=rel_path, **kw)
        write_jpeg(source_dir / rel_path)
        return fid

    file_ids = []
    for i in range(MEDIA_COUNT):
        fid = _file(f"2024/photo{i:03d}.jpg")
        factories.add_date(conn, fid, best_datetime=f"2024-{1 + i % 9:02d}-15T10:00:00")
        file_ids.append(fid)
    ids["first_file"] = file_ids[0]

    # Two of each: a merge needs a second card to drop onto, so one of anything
    # cannot exercise the interaction at all.
    for name, fid in (("Ada", file_ids[0]), ("Grace", file_ids[1])):
        person_id = factories.add_person(conn, name=name)
        factories.add_face(conn, fid, person_id=person_id)
        conn.execute("UPDATE persons SET face_count=1 WHERE id=?", (person_id,))
        ids[f"person_{name.lower()}"] = person_id
    for name, fid in (("Kira", file_ids[2]), ("Rex", file_ids[3])):
        pet_id = factories.add_pet(conn, name=name)
        factories.add_animal_detection(conn, fid, pet_id=pet_id)
        conn.execute("UPDATE pets SET detection_count=1 WHERE id=?", (pet_id,))
        ids[f"pet_{name.lower()}"] = pet_id

    # Places: geotagged members under one named cluster.
    geotagged = file_ids[:12]
    for fid in geotagged:
        factories.add_geo(conn, fid)
    ids["place"] = factories.add_place(conn, name="Bariloche", root_id=root_id, file_ids=geotagged)

    canonical, copy = file_ids[4], file_ids[5]
    ids["dup_group"] = seed_duplicates(conn, canonical, copy)
    ids["dup_kept"], ids["dup_copy"] = canonical, copy
    ids["hostile_copy"] = seed_hostile_names(conn, root_id, source_dir, file_ids[6])

    # A photograph none of the other fixtures has claimed: not the one the item
    # panel opens, not a person's, not a pet's, not half of the duplicate pair.
    ids["ocr_photo"] = file_ids[20]
    ids["document"] = seed_documents(conn, root_id, source_dir, ids["ocr_photo"])

    # The run that produced the group above. Dedup writes no per-file row -- a
    # file with no copies is simply in no group -- so this marker is the whole
    # of what says a file has been compared, and the viewer's Duplicates section
    # reads it to tell "no copies" from "not compared yet". Written last, so it
    # covers everything seeded; a group no run ever made is a state no real
    # archive can be in.
    db.dedup_mark_done(conn, root_id, *db.dedup_coverage(conn, root_id))

    # Without this, opening the archive treats every person seeded above as
    # stale identity data from a retired embedder and wipes them -- see the
    # same call in tests/gui/live_archive.py, where it cost a debugging session.
    from trove.faces import backend as face_backend
    from trove.faces import migrate_adaface

    migrate_adaface.mark_embedder(conn, face_backend.EMBEDDER_VERSION)
    return ids
