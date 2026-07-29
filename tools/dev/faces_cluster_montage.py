#!/usr/bin/env python3
"""Render one big PNG montage of every face cluster (person).

Each cluster becomes a small tile: a 2x2 mini-collage of its clearest,
most-varied faces (highest detection score, preferring distinct source photos,
cover face first) with an `#id ·N` caption. Tiles are sorted largest cluster
first and packed into a near-square grid, so the whole population is visible at
once and the same identity showing up as several clusters is easy to spot.

Read-only over originals: face crops are produced by the same cached crop path
the GUI uses (thumbs.face_thumb_for). Run from the repo root:

    python3 tools/dev/faces_cluster_montage.py [out.png] [--per 4] [--crop 96] [--cols N]
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from math import ceil, sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from organize_archive.config import Config          # noqa: E402
from organize_archive.db import database as db       # noqa: E402
from organize_archive.gui import thumbs              # noqa: E402


def _worker(args):
    """Generate (or reuse cached) one face crop; returns (face_id, path|None)."""
    cache_dir, face_id, src, box, sha = args
    try:
        tp = thumbs.face_thumb_for(cache_dir, face_id, Path(src), box,
                                   sha256=sha, size=200)
        return face_id, (str(tp) if tp else None)
    except Exception:
        return face_id, None


def _pick_representatives(faces, cover_id, per):
    """Up to `per` face rows for a cluster: cover first, then highest det_score
    preferring distinct source photos, then fill with the rest."""
    faces = sorted(faces, key=lambda r: -(r["det_score"] or 0.0))
    chosen, seen_files = [], set()
    # cover face first if we have it
    for r in faces:
        if cover_id is not None and r["fid"] == cover_id:
            chosen.append(r)
            seen_files.add(r["file_id"])
            break
    # then diverse (distinct file) high-score faces
    for r in faces:
        if len(chosen) >= per:
            break
        if r in chosen or r["file_id"] in seen_files:
            continue
        chosen.append(r)
        seen_files.add(r["file_id"])
    # backfill if a cluster's faces all come from few photos
    for r in faces:
        if len(chosen) >= per:
            break
        if r not in chosen:
            chosen.append(r)
    return chosen[:per]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="faces_clusters.png")
    ap.add_argument("--per", type=int, default=4, help="faces per cluster tile (2x2=4)")
    ap.add_argument("--crop", type=int, default=96, help="sub-crop px in the tile")
    ap.add_argument("--cols", type=int, default=0, help="tile columns (0=auto near-square)")
    ap.add_argument("--min-faces", type=int, default=0, help="skip clusters below this size")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    from PIL import Image, ImageDraw, ImageFont

    cfg = Config.load()
    conn = db.open_readonly(cfg.db_path)

    persons = conn.execute(
        """SELECT id, name, face_count, cover_face_id
           FROM persons WHERE face_count >= ?
           ORDER BY face_count DESC, id""", (args.min_faces,)).fetchall()
    if not persons:
        print("No clusters found.")
        return 1

    # All assigned faces with the info needed to crop, in one pass.
    rows = conn.execute(
        """SELECT fa.id AS fid, fa.file_id, fa.det_score, fa.person_id,
                  fa.box_x, fa.box_y, fa.box_w, fa.box_h,
                  f.sha256, r.path AS root, f.rel_path
           FROM faces fa
           JOIN files f ON f.id = fa.file_id
           JOIN roots r ON r.id = f.root_id
           WHERE fa.person_id IS NOT NULL AND f.hidden = 0""").fetchall()

    by_person: dict[int, list] = {}
    for r in rows:
        by_person.setdefault(r["person_id"], []).append(r)

    # Choose representative faces per cluster, collect the crop jobs.
    plan = []          # list of (person_row, [face_row, ...])
    jobs = {}          # face_id -> (cache_dir, fid, src, box, sha)
    for p in persons:
        faces = by_person.get(p["id"], [])
        if not faces:
            continue
        reps = _pick_representatives(faces, p["cover_face_id"], args.per)
        plan.append((p, reps))
        for r in reps:
            src = str(Path(r["root"]) / r["rel_path"])
            box = (r["box_x"], r["box_y"], r["box_w"], r["box_h"])
            jobs[r["fid"]] = (cfg.cache_dir, r["fid"], src, box, r["sha256"])

    print(f"{len(plan)} clusters · {len(jobs)} face crops to render "
          f"(min-faces={args.min_faces}) …")

    # Generate crops in parallel (cached on disk; reused across runs).
    crops: dict[int, str] = {}
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for fid, path in ex.map(_worker, jobs.values(), chunksize=16):
            if path:
                crops[fid] = path
            done += 1
            if done % 500 == 0:
                print(f"  crops {done}/{len(jobs)}")

    # ---- lay out the grid -------------------------------------------------
    per = args.per
    sub = args.crop
    mini_cols = 2 if per >= 2 else 1
    mini_rows = ceil(per / mini_cols)
    pad = 3
    cap_h = 20
    tile_w = mini_cols * sub + (mini_cols + 1) * pad
    tile_h = mini_rows * sub + (mini_rows + 1) * pad + cap_h

    n = len(plan)
    cols = args.cols or max(1, round(sqrt(n * tile_h / tile_w)))
    grid_rows = ceil(n / cols)
    W = cols * tile_w
    H = grid_rows * tile_h
    print(f"canvas {W}x{H}px  ({cols} cols x {grid_rows} rows, tile {tile_w}x{tile_h})")

    bg = (18, 18, 20)
    canvas = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except Exception:
        font = ImageFont.load_default()

    def hue_color(pid):
        # stable pseudo-color per cluster id for the caption bar
        import colorsys
        h = ((pid * 47) % 360) / 360.0
        r, g, b = colorsys.hsv_to_rgb(h, 0.55, 0.85)
        return (int(r * 255), int(g * 255), int(b * 255))

    placeholder = Image.new("RGB", (sub, sub), (40, 40, 44))

    for idx, (p, reps) in enumerate(plan):
        gx = (idx % cols) * tile_w
        gy = (idx // cols) * tile_h
        # caption bar
        col = hue_color(p["id"])
        draw.rectangle([gx, gy, gx + tile_w - 1, gy + cap_h - 1], fill=col)
        label = p["name"] if p["name"] else f"#{p['id']}"
        draw.text((gx + 4, gy + 3), f"{label}  ·{p['face_count']}",
                  fill=(15, 15, 15), font=font)
        # face crops
        for k in range(per):
            mr, mc = divmod(k, mini_cols)
            cx = gx + pad + mc * (sub + pad)
            cy = gy + cap_h + pad + mr * (sub + pad)
            if k < len(reps) and reps[k]["fid"] in crops:
                try:
                    im = Image.open(crops[reps[k]["fid"]]).convert("RGB")
                    im.thumbnail((sub, sub))
                    off_x = cx + (sub - im.width) // 2
                    off_y = cy + (sub - im.height) // 2
                    canvas.paste(im, (off_x, off_y))
                except Exception:
                    canvas.paste(placeholder, (cx, cy))
            else:
                canvas.paste(placeholder, (cx, cy))
        if (idx + 1) % 200 == 0:
            print(f"  laid out {idx + 1}/{n}")

    out = Path(args.out)
    canvas.save(out, "PNG")
    print(f"wrote {out.resolve()}  ({out.stat().st_size / 1e6:.1f} MB, {W}x{H})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
