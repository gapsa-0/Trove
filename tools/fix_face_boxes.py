#!/usr/bin/env python3
"""One-time repair for face boxes stored before the load_bgr() draft-scale fix.

Bug: FaceBackend.load_bgr() calls Pillow's Image.draft() to let libjpeg
downscale large JPEGs during decode (a real speedup). draft() silently shrinks
`im.size` as a side effect — a 4032x3024 photo becomes 2016x1512 *before* the
old code computed its "original / detection" scale factor. That factor only
corrected for the second, explicit resize, not for draft()'s own shrink, so
every box stored for a photo where draft() kicked in landed at roughly half
the coordinates it should have. gui/thumbs.py crops the *actual* full-res
original with that wrong box, so the GUI's face gallery showed an
off-position, wrong-scale patch of the photo instead of the detected face.

backend.py is now fixed for *future* detections. This script repairs rows
already written to the database by re-deriving the missed draft ratio for
each file — a cheap header read + draft() call, no CNN inference — and
rescaling the stored box_x/box_y/box_w/box_h in place. Embeddings and person
clustering are untouched: they were computed from the detection-array
coordinates directly and were never affected by this bug.

Safe to interrupt and re-run *once* — but running it a second time after a
successful pass would re-derive the same ratio and apply it again, corrupting
already-fixed rows. It is not meant to be a recurring maintenance command; run
it once after deploying the backend.py fix, then forget about it.

Usage:
    python3 tools/fix_face_boxes.py [--dry-run] [/path/to/archive.db]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from organize_archive.config import Config                    # noqa: E402
from organize_archive.db import database as db                # noqa: E402


def draft_ratio(path: str, max_side: int) -> float:
    """true_original_max_side / post_draft_max_side, i.e. exactly the factor
    the old code failed to account for. 1.0 when draft() didn't shrink
    anything (small photos, non-JPEG)."""
    from PIL import Image
    with Image.open(path) as im:
        orig_side = max(im.size)
        try:
            im.draft("RGB", (max_side, max_side))
        except Exception:
            pass
        post_side = max(im.size)
    if not post_side:
        return 1.0
    return orig_side / post_side


def main(db_path: str, dry_run: bool) -> int:
    cfg = Config.load()
    conn = db.connect(db_path)
    rows = conn.execute(
        """SELECT f.id AS file_id, r.path AS root_path, f.rel_path
           FROM files f JOIN roots r ON r.id = f.root_id
           WHERE f.id IN (SELECT DISTINCT file_id FROM faces)"""
    ).fetchall()
    print(f"{len(rows)} face-scanned file(s) to check "
          f"{'(dry run — no writes)' if dry_run else ''}…")

    files_fixed = faces_fixed = files_missing = 0
    ratios_seen = {}
    for i, row in enumerate(rows):
        path = os.path.join(row["root_path"], row["rel_path"])
        if not os.path.isfile(path):
            files_missing += 1
            continue
        try:
            k = draft_ratio(path, cfg.faces_max_side)
        except Exception as e:
            print(f"  ! {row['rel_path']}: {e}")
            continue
        if abs(k - 1.0) < 1e-6:
            continue
        ratios_seen[round(k, 3)] = ratios_seen.get(round(k, 3), 0) + 1
        faces = conn.execute(
            "SELECT id, box_x, box_y, box_w, box_h FROM faces WHERE file_id=?",
            (row["file_id"],)).fetchall()
        if not dry_run:
            for fc in faces:
                conn.execute(
                    "UPDATE faces SET box_x=?, box_y=?, box_w=?, box_h=? WHERE id=?",
                    (round(fc["box_x"] * k), round(fc["box_y"] * k),
                     round(fc["box_w"] * k), round(fc["box_h"] * k), fc["id"]))
        faces_fixed += len(faces)
        files_fixed += 1
        if not dry_run and (i + 1) % 2000 == 0:
            conn.commit()
            print(f"  … {i + 1}/{len(rows)} checked, {files_fixed} file(s) corrected so far")
    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\n{'Would correct' if dry_run else 'Corrected'}: {files_fixed} file(s) / "
          f"{faces_fixed} face(s) ({files_missing} file(s) no longer on disk, skipped).")
    print(f"Ratio distribution (draft shrink factor -> file count): {ratios_seen}")

    if dry_run:
        return 0

    cache_faces_dir = os.path.join(cfg.cache_dir, "faces")
    if os.path.isdir(cache_faces_dir):
        shutil.rmtree(cache_faces_dir)
        print(f"Cleared stale cached crops at {cache_faces_dir} "
              f"(regenerate on next view, keyed by the corrected boxes).")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db_path", nargs="?", default=Config.load().db_path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    raise SystemExit(main(args.db_path, args.dry_run))
