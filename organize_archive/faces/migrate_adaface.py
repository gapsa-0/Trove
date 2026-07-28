"""One-time migration: re-extract every face for the AdaFace embedder.

Why a migration is needed at all. The face embedder changed (buffalo_l ArcFace →
AdaFace ir101), and embeddings from two different models share no vector space:
the cosine between an ArcFace vector and an AdaFace vector is noise. Every stored
embedding is therefore invalid and must be recomputed, which means re-running
detection over the whole archive.

Why that is dangerous, and what this module protects. Detection rewrites rows: the
fused pass deletes a file's `faces`, `animal_detections`, `nonhuman_detections`
and `orientation` rows before writing fresh ones. Every piece of *user-authored*
identity is anchored to those row ids —

  * a person's name, held by whichever `persons` row its faces point at,
  * ``faces.manual_person`` pins ("this face is Mari, keep it that way"),
  * ``faces.not_person`` / ``nonhuman_kind`` ("this is a doll, not a person"),
  * ``face_links`` — the durable "same"/"different" answers from review,
  * and on the pets side, ``pet_links`` and ``animal_detections.manual_pet``.

— so a naive re-extract would silently discard hundreds of hand-made decisions.

The carry-over. Detection is deterministic and unchanged (same SCRFD weights, same
``det_size``, same decode path), so a re-detected face lands on essentially the
same pixels as before. That makes the box itself a durable identifier: this module
snapshots every identity-bearing fact keyed by ``(file_id, box, frame_offset)``
before the wipe, and re-attaches it afterwards by matching each new row to the old
one with the best box overlap above ``MIN_IOU``. Anything that fails to match is
reported rather than guessed at.

Usage is two calls around the re-extract, because the re-extract itself is the
long, resumable, interruptible part:

    snapshot_and_wipe(conn, cfg)      # back up, snapshot, clear, invalidate scans
    ...run the normal detect/extract pipeline to completion...
    reattach(conn, cfg)               # restore names, pins, links; then recluster

``reattach`` is idempotent and safe to run more than once.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..db import database as db

# Box overlap required to call a new detection "the same face" as an old one.
# Generous on purpose: identical detector + identical input means boxes normally
# match to within a pixel, so anything at 0.5 is the same face, and demanding
# more would only lose pins to rounding.
MIN_IOU = 0.5

_CARRY_FACES = "face_identity_carry"
_CARRY_LINKS = "face_link_carry"
_CARRY_PETS = "pet_identity_carry"
_CARRY_PET_LINKS = "pet_link_carry"


@dataclass
class MigrationStats:
    faces_snapshotted: int = 0
    links_snapshotted: int = 0
    pets_snapshotted: int = 0
    pet_links_snapshotted: int = 0
    faces_reattached: int = 0
    names_restored: int = 0
    pins_restored: int = 0
    not_person_restored: int = 0
    links_restored: int = 0
    links_dropped: int = 0
    pets_reattached: int = 0
    pet_links_restored: int = 0
    unmatched: int = 0
    backup_path: str = ""
    notes: list = field(default_factory=list)


def _ensure_carry_tables(conn) -> None:
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {_CARRY_FACES} (
            old_face_id  INTEGER PRIMARY KEY,
            file_id      INTEGER NOT NULL,
            box_x        INTEGER NOT NULL,
            box_y        INTEGER NOT NULL,
            box_w        INTEGER NOT NULL,
            box_h        INTEGER NOT NULL,
            frame_offset TEXT,
            person_name  TEXT,
            manual_person TEXT,
            not_person   INTEGER NOT NULL DEFAULT 0,
            nonhuman_kind TEXT,
            nonhuman_source TEXT,
            new_face_id  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_{_CARRY_FACES}_file
            ON {_CARRY_FACES}(file_id);
        CREATE TABLE IF NOT EXISTS {_CARRY_LINKS} (
            old_face_a INTEGER NOT NULL,
            old_face_b INTEGER NOT NULL,
            kind       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (old_face_a, old_face_b)
        );
        CREATE TABLE IF NOT EXISTS {_CARRY_PETS} (
            old_det_id   INTEGER PRIMARY KEY,
            file_id      INTEGER NOT NULL,
            box_x        INTEGER NOT NULL,
            box_y        INTEGER NOT NULL,
            box_w        INTEGER NOT NULL,
            box_h        INTEGER NOT NULL,
            frame_offset TEXT,
            pet_name     TEXT,
            manual_pet   TEXT,
            new_det_id   INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_{_CARRY_PETS}_file
            ON {_CARRY_PETS}(file_id);
        CREATE TABLE IF NOT EXISTS {_CARRY_PET_LINKS} (
            old_det_a  INTEGER NOT NULL,
            old_det_b  INTEGER NOT NULL,
            kind       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (old_det_a, old_det_b)
        );
    """)


def _backup(db_path: str, log=None) -> str:
    """Copy the database next to itself before anything destructive happens.

    Not a nicety: the wipe below is irreversible, and the whole point of the
    carry tables is to preserve work that took a human a long time to produce.
    """
    src = Path(db_path)
    dst = src.with_name(f"{src.stem}.pre-adaface{src.suffix}")
    n = 1
    while dst.exists():
        n += 1
        dst = src.with_name(f"{src.stem}.pre-adaface-{n}{src.suffix}")
    if log:
        log(f"backing up {src.name} → {dst.name}")
    shutil.copy2(src, dst)
    return str(dst)


def snapshot_and_wipe(conn, cfg: Config, db_path: str | None = None,
                      log=None) -> MigrationStats:
    """Preserve identity, clear the invalid embeddings, re-arm the scanners."""
    db.init_db(conn)
    stats = MigrationStats()
    _ensure_carry_tables(conn)

    if db_path:
        stats.backup_path = _backup(db_path, log=log)

    # -- snapshot ----------------------------------------------------------
    conn.execute(f"DELETE FROM {_CARRY_FACES}")
    conn.execute(f"DELETE FROM {_CARRY_LINKS}")
    conn.execute(f"DELETE FROM {_CARRY_PETS}")
    conn.execute(f"DELETE FROM {_CARRY_PET_LINKS}")
    conn.execute(f"""
        INSERT INTO {_CARRY_FACES}
            (old_face_id, file_id, box_x, box_y, box_w, box_h, frame_offset,
             person_name, manual_person, not_person, nonhuman_kind, nonhuman_source)
        SELECT fa.id, fa.file_id, fa.box_x, fa.box_y, fa.box_w, fa.box_h,
               fa.frame_offset, NULLIF(TRIM(COALESCE(p.name, '')), ''),
               fa.manual_person, COALESCE(fa.not_person, 0),
               fa.nonhuman_kind, fa.nonhuman_source
        FROM faces fa LEFT JOIN persons p ON p.id = fa.person_id
        -- Only rows that carry something a human decided. An ordinary
        -- auto-clustered face needs no carry: clustering will re-derive it.
        WHERE NULLIF(TRIM(COALESCE(p.name, '')), '') IS NOT NULL
           OR fa.manual_person IS NOT NULL
           OR COALESCE(fa.not_person, 0) = 1
           -- Link endpoints must be carried even when the face itself holds
           -- nothing else: face_links is remapped THROUGH this table, so a
           -- link whose endpoints are absent here could only be dropped.
           OR fa.id IN (SELECT face_a FROM face_links
                        UNION SELECT face_b FROM face_links)""")
    stats.faces_snapshotted = conn.execute(
        f"SELECT COUNT(*) FROM {_CARRY_FACES}").fetchone()[0]

    conn.execute(f"""
        INSERT INTO {_CARRY_LINKS} (old_face_a, old_face_b, kind, created_at)
        SELECT face_a, face_b, kind, created_at FROM face_links""")
    stats.links_snapshotted = conn.execute(
        f"SELECT COUNT(*) FROM {_CARRY_LINKS}").fetchone()[0]

    # Pets ride along. The fused detect pass deletes and rewrites
    # animal_detections for every file it revisits, so pet names — anchored to
    # detection ids through pet_links — would be collateral damage of a
    # faces-only migration.
    conn.execute(f"""
        INSERT INTO {_CARRY_PETS}
            (old_det_id, file_id, box_x, box_y, box_w, box_h, frame_offset,
             pet_name, manual_pet)
        SELECT a.id, a.file_id, a.box_x, a.box_y, a.box_w, a.box_h,
               a.frame_offset, NULLIF(TRIM(COALESCE(p.name, '')), ''), a.manual_pet
        FROM animal_detections a LEFT JOIN pets p ON p.id = a.pet_id
        WHERE NULLIF(TRIM(COALESCE(p.name, '')), '') IS NOT NULL
           OR a.manual_pet IS NOT NULL""")
    stats.pets_snapshotted = conn.execute(
        f"SELECT COUNT(*) FROM {_CARRY_PETS}").fetchone()[0]
    conn.execute(f"""
        INSERT INTO {_CARRY_PET_LINKS} (old_det_a, old_det_b, kind, created_at)
        SELECT det_a, det_b, kind, created_at FROM pet_links""")
    stats.pet_links_snapshotted = conn.execute(
        f"SELECT COUNT(*) FROM {_CARRY_PET_LINKS}").fetchone()[0]

    # -- wipe --------------------------------------------------------------
    # What gets destroyed here is only what the embedder change INVALIDATED:
    # the face vectors, and the clusters built out of them. Animal detections,
    # pets and non-human review verdicts are NOT deleted — the pet embedder did
    # not change, so those vectors are still meaningful, and the detect pass
    # replaces each file's rows as it revisits them anyway. That keeps the Pets
    # view populated during the re-run instead of blanking it for hours.
    #
    # person_files is deliberately NOT cleared either: it is anchored by person
    # NAME (person_files.person_name) and gui.queries.repair_manual_person_files
    # re-points it after clustering, which is exactly the path a normal
    # recluster already takes.
    conn.execute("UPDATE faces SET person_id=NULL")
    conn.execute("DELETE FROM persons")
    conn.execute("DELETE FROM faces")
    # BOTH scan markers, always cleared together — the same invariant the fused
    # detect pass keeps when it writes them together. Detection is one shared
    # decode per file that runs people *and* animals, so clearing only face_scan
    # would queue every file for re-detection while pet_scan still claimed those
    # files were done: the Pets card would show a stale "28,794 scanned" for work
    # that is in fact about to be redone.
    conn.execute("DELETE FROM face_scan")
    conn.execute("DELETE FROM pet_scan")
    # The FIQA calibration describes the OLD embedder's norms and is meaningless
    # for AdaFace vectors; drop it so the next extract re-derives it.
    conn.execute("DELETE FROM fiqa_calibration")
    conn.commit()
    if log:
        log(f"snapshotted {stats.faces_snapshotted} identity-bearing faces, "
            f"{stats.links_snapshotted} links, {stats.pets_snapshotted} pets; "
            "cleared faces and re-armed the detector")
    return stats


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter == 0:
        return 0.0
    return inter / float(aw * ah + bw * bh - inter)


def _match_by_box(carry_rows, new_rows) -> dict[int, int]:
    """Greedy best-overlap matching of old rows to new rows within one file.

    Greedy is right here rather than optimal assignment: re-detection reproduces
    the same boxes, so the best pair is unambiguous and a globally optimal
    matcher would buy nothing. Frame offset must agree, so a video's faces can
    never migrate between sampled frames.
    """
    pairs = []
    for old in carry_rows:
        for new in new_rows:
            if (old["frame_offset"] or None) != (new["frame_offset"] or None):
                continue
            score = _iou(
                (old["box_x"], old["box_y"], old["box_w"], old["box_h"]),
                (new["box_x"], new["box_y"], new["box_w"], new["box_h"]))
            if score >= MIN_IOU:
                pairs.append((score, old["old_id"], new["id"]))
    pairs.sort(reverse=True)
    used_old, used_new, out = set(), set(), {}
    for _score, old_id, new_id in pairs:
        if old_id in used_old or new_id in used_new:
            continue
        used_old.add(old_id)
        used_new.add(new_id)
        out[old_id] = new_id
    return out


def _remap(conn, carry_table: str, id_col: str, new_table: str,
           new_id_col: str = "id") -> dict[int, int]:
    """Fill carry.new_* by matching boxes file by file. Returns old→new."""
    mapping: dict[int, int] = {}
    file_ids = [r[0] for r in conn.execute(
        f"SELECT DISTINCT file_id FROM {carry_table}")]
    for fid in file_ids:
        carry_rows = [
            {"old_id": r[id_col], "box_x": r["box_x"], "box_y": r["box_y"],
             "box_w": r["box_w"], "box_h": r["box_h"],
             "frame_offset": r["frame_offset"]}
            for r in conn.execute(
                f"SELECT * FROM {carry_table} WHERE file_id=?", (fid,))]
        new_rows = [
            {"id": r[new_id_col], "box_x": r["box_x"], "box_y": r["box_y"],
             "box_w": r["box_w"], "box_h": r["box_h"],
             "frame_offset": r["frame_offset"]}
            for r in conn.execute(
                f"SELECT * FROM {new_table} WHERE file_id=?", (fid,))]
        matched = _match_by_box(carry_rows, new_rows)
        mapping.update(matched)
    new_col = "new_face_id" if carry_table == _CARRY_FACES else "new_det_id"
    conn.executemany(
        f"UPDATE {carry_table} SET {new_col}=? WHERE {id_col}=?",
        [(new_id, old_id) for old_id, new_id in mapping.items()])
    return mapping


def reattach(conn, cfg: Config, log=None) -> MigrationStats:
    """Restore names, pins and links onto the freshly extracted faces."""
    db.init_db(conn)
    stats = MigrationStats()
    _ensure_carry_tables(conn)
    now = db.now_iso()

    # -- faces -------------------------------------------------------------
    face_map = _remap(conn, _CARRY_FACES, "old_face_id", "faces")
    stats.faces_reattached = len(face_map)
    stats.unmatched = conn.execute(
        f"SELECT COUNT(*) FROM {_CARRY_FACES} WHERE new_face_id IS NULL"
    ).fetchone()[0]

    # A person's name is re-applied as a manual_person PIN rather than by
    # recreating `persons` rows. Pins are the mechanism clustering already
    # honours on every rebuild (_apply_manual_pins), so the name lands on the
    # right faces no matter how the new AdaFace vectors happen to cluster —
    # which is the whole point, since they will not cluster identically.
    for row in conn.execute(
            f"""SELECT new_face_id, person_name, manual_person, not_person,
                       nonhuman_kind, nonhuman_source
                FROM {_CARRY_FACES} WHERE new_face_id IS NOT NULL"""):
        pin = row["manual_person"] or row["person_name"]
        if pin:
            conn.execute("UPDATE faces SET manual_person=? WHERE id=?",
                         (pin, row["new_face_id"]))
            stats.pins_restored += 1
            if row["person_name"]:
                stats.names_restored += 1
        if row["not_person"]:
            conn.execute(
                """UPDATE faces SET not_person=1, nonhuman_kind=?,
                       nonhuman_source=? WHERE id=?""",
                (row["nonhuman_kind"], row["nonhuman_source"], row["new_face_id"]))
            stats.not_person_restored += 1

    for link in conn.execute(f"SELECT * FROM {_CARRY_LINKS}"):
        a = face_map.get(link["old_face_a"])
        b = face_map.get(link["old_face_b"])
        if a is None or b is None:
            # One end no longer exists (the detector did not reproduce that
            # face). Dropping is the only honest option: a link is a statement
            # about two specific faces, and half of one means nothing.
            stats.links_dropped += 1
            continue
        lo, hi = (a, b) if a < b else (b, a)
        conn.execute(
            """INSERT OR IGNORE INTO face_links(face_a, face_b, kind, created_at)
               VALUES(?,?,?,?)""", (lo, hi, link["kind"], link["created_at"]))
        stats.links_restored += 1

    # -- pets --------------------------------------------------------------
    pet_map = _remap(conn, _CARRY_PETS, "old_det_id", "animal_detections")
    stats.pets_reattached = len(pet_map)
    for row in conn.execute(
            f"""SELECT new_det_id, pet_name, manual_pet FROM {_CARRY_PETS}
                WHERE new_det_id IS NOT NULL"""):
        pin = row["manual_pet"] or row["pet_name"]
        if pin:
            conn.execute("UPDATE animal_detections SET manual_pet=? WHERE id=?",
                         (pin, row["new_det_id"]))
    for link in conn.execute(f"SELECT * FROM {_CARRY_PET_LINKS}"):
        a = pet_map.get(link["old_det_a"])
        b = pet_map.get(link["old_det_b"])
        if a is None or b is None:
            continue
        lo, hi = (a, b) if a < b else (b, a)
        conn.execute(
            """INSERT OR IGNORE INTO pet_links(det_a, det_b, kind, created_at)
               VALUES(?,?,?,?)""", (lo, hi, link["kind"], link["created_at"]))
        stats.pet_links_restored += 1

    conn.commit()
    if log:
        log(f"reattached {stats.faces_reattached} faces "
            f"({stats.names_restored} named, {stats.pins_restored} pinned, "
            f"{stats.not_person_restored} not-a-person), "
            f"{stats.links_restored} links restored / {stats.links_dropped} dropped, "
            f"{stats.pets_reattached} pet detections; {stats.unmatched} unmatched")
    _ = now
    return stats


_EMBEDDER_KEY = "faces_embedder"


def stored_embedder(conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM app_state WHERE key=?", (_EMBEDDER_KEY,)).fetchone()
    return row["value"] if row else None


def mark_embedder(conn, version: str) -> None:
    conn.execute(
        """INSERT INTO app_state(key, value, updated_at) VALUES(?,?,?)
           ON CONFLICT(key) DO UPDATE SET
               value=excluded.value, updated_at=excluded.updated_at""",
        (_EMBEDDER_KEY, version, db.now_iso()))


def run_if_needed(conn, cfg: Config, db_path: str | None = None,
                  log=None) -> MigrationStats | None:
    """Re-arm the archive for re-extraction when the embedder has changed.

    This is what makes the switch automatic: the app calls it when it opens an
    archive, and if the stored vectors came from a different model it snapshots
    the user's identity work, clears the faces, and clears the scan markers. The
    normal pipeline then sees a full detection backlog and refills it from zero —
    no command to run, and the progress bar starts where a user expects it to.

    One indexed lookup when there is nothing to do, so it is cheap to call on
    every open. Returns None when no migration was needed.
    """
    from . import backend
    db.init_db(conn)
    current = backend.EMBEDDER_VERSION
    if stored_embedder(conn) == current:
        return None

    # A brand-new (or already-empty) archive has no stale vectors to throw away:
    # just record which embedder its faces will be built with. This is also what
    # stops a fresh install from performing a pointless "migration" on first run.
    has_faces = conn.execute("SELECT 1 FROM faces LIMIT 1").fetchone() is not None
    if not has_faces:
        mark_embedder(conn, current)
        conn.commit()
        return None

    if log:
        log(f"face embedder changed → {current}; re-extracting this archive")
    stats = snapshot_and_wipe(conn, cfg, db_path=db_path, log=log)
    mark_embedder(conn, current)
    conn.commit()
    return stats


def pending(conn) -> bool:
    """True when a snapshot exists whose faces have not been reattached yet."""
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {_CARRY_FACES} WHERE new_face_id IS NULL"
        ).fetchone()
    except Exception:
        return False
    return bool(row and row[0])
