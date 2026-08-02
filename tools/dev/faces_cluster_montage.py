#!/usr/bin/env python3
"""Render one big PNG montage of every face cluster (person).

Each cluster becomes a small tile: a 2x2 mini-collage of its clearest,
most-varied faces (highest detection score, preferring distinct source photos,
cover face first) with an `#id ·N` caption. Tiles are sorted largest cluster
first and packed into a near-square grid, so the whole population is visible at
once and the same identity showing up as several clusters is easy to spot.

Read-only over originals: face crops are produced by the same cached crop path
the GUI uses (thumbnails.face_thumb_for). Run from the repo root:

    python3 tools/dev/faces_cluster_montage.py [out.png] [--per 4] [--crop 96] [--cols N]
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from math import ceil, sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from organize_archive import thumbnails
from organize_archive.config import Config
from organize_archive.db import database as db


def _worker(args):
    """Generate (or reuse cached) one face crop; returns (face_id, path|None)."""
    cache_dir, face_id, src, box, sha = args
    try:
        tp = thumbnails.face_thumb_for(cache_dir, face_id, Path(src), box, sha256=sha, size=200)
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


def _parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="faces_clusters.png")
    ap.add_argument("--per", type=int, default=4, help="faces per cluster tile (2x2=4)")
    ap.add_argument("--crop", type=int, default=96, help="sub-crop px in the tile")
    ap.add_argument("--cols", type=int, default=0, help="tile columns (0=auto near-square)")
    ap.add_argument("--min-faces", type=int, default=0, help="skip clusters below this size")
    ap.add_argument("--workers", type=int, default=8)
    return ap.parse_args()


def _build_plan(conn, cfg, args):
    """``(plan, jobs)``: which faces represent each cluster, and the crops to render.

    ``plan`` is ``[(person_row, [face_row, ...]), ...]`` in display order;
    ``jobs`` maps face id to the crop arguments ``_worker`` needs. Returns
    ``(None, None)`` when there is nothing to draw.
    """
    persons = conn.execute(
        """SELECT id, name, face_count, cover_face_id
           FROM persons WHERE face_count >= ?
           ORDER BY face_count DESC, id""",
        (args.min_faces,),
    ).fetchall()
    if not persons:
        return None, None

    # All assigned faces with the info needed to crop, in one pass.
    rows = conn.execute(
        """SELECT fa.id AS fid, fa.file_id, fa.det_score, fa.person_id,
                  fa.box_x, fa.box_y, fa.box_w, fa.box_h,
                  f.sha256, r.path AS root, f.rel_path
           FROM faces fa
           JOIN files f ON f.id = fa.file_id
           JOIN roots r ON r.id = f.root_id
           WHERE fa.person_id IS NOT NULL AND f.hidden = 0"""
    ).fetchall()
    by_person: dict[int, list] = {}
    for r in rows:
        by_person.setdefault(r["person_id"], []).append(r)

    plan = []
    jobs = {}
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
    return plan, jobs


def _render_crops(jobs, workers: int) -> dict[int, str]:
    """Generate the face crops in parallel. Cached on disk, reused across runs."""
    crops: dict[int, str] = {}
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for fid, path in ex.map(_worker, jobs.values(), chunksize=16):
            if path:
                crops[fid] = path
            done += 1
            if done % 500 == 0:
                print(f"  crops {done}/{len(jobs)}")
    return crops


def _geometry(n: int, args):
    """Tile and canvas dimensions for ``n`` clusters, as a dict of measurements."""
    per, sub, pad, cap_h = args.per, args.crop, 3, 20
    mini_cols = 2 if per >= 2 else 1
    mini_rows = ceil(per / mini_cols)
    tile_w = mini_cols * sub + (mini_cols + 1) * pad
    tile_h = mini_rows * sub + (mini_rows + 1) * pad + cap_h
    cols = args.cols or max(1, round(sqrt(n * tile_h / tile_w)))
    grid_rows = ceil(n / cols)
    return {
        "sub": sub,
        "pad": pad,
        "cap_h": cap_h,
        "mini_cols": mini_cols,
        "tile_w": tile_w,
        "tile_h": tile_h,
        "cols": cols,
        "W": cols * tile_w,
        "H": grid_rows * tile_h,
        "grid_rows": grid_rows,
    }


def _hue_color(pid: int):
    """Stable pseudo-colour per cluster id, for the caption bar."""
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(((pid * 47) % 360) / 360.0, 0.55, 0.85)
    return (int(r * 255), int(g * 255), int(b * 255))


def _draw_tile(canvas, draw, Image, font, placeholder, p, reps, crops, g, idx, per):
    """One cluster's caption bar and its grid of face crops."""
    gx = (idx % g["cols"]) * g["tile_w"]
    gy = (idx // g["cols"]) * g["tile_h"]
    sub, pad, cap_h = g["sub"], g["pad"], g["cap_h"]

    draw.rectangle([gx, gy, gx + g["tile_w"] - 1, gy + cap_h - 1], fill=_hue_color(p["id"]))
    label = p["name"] if p["name"] else f"#{p['id']}"
    draw.text((gx + 4, gy + 3), f"{label}  ·{p['face_count']}", fill=(15, 15, 15), font=font)

    for k in range(per):
        mr, mc = divmod(k, g["mini_cols"])
        cx = gx + pad + mc * (sub + pad)
        cy = gy + cap_h + pad + mr * (sub + pad)
        if k < len(reps) and reps[k]["fid"] in crops:
            try:
                im = Image.open(crops[reps[k]["fid"]]).convert("RGB")
                im.thumbnail((sub, sub))
                canvas.paste(im, (cx + (sub - im.width) // 2, cy + (sub - im.height) // 2))
                continue
            except Exception:
                pass  # unreadable crop: fall through to the placeholder
        canvas.paste(placeholder, (cx, cy))


def main():
    args = _parse_args()
    from PIL import Image, ImageDraw, ImageFont

    cfg = Config.load()
    conn = db.open_readonly(cfg.db_path)
    plan, jobs = _build_plan(conn, cfg, args)
    if plan is None:
        print("No clusters found.")
        return 1

    print(f"{len(plan)} clusters · {len(jobs)} face crops to render (min-faces={args.min_faces}) …")
    crops = _render_crops(jobs, args.workers)

    n = len(plan)
    g = _geometry(n, args)
    print(
        f"canvas {g['W']}x{g['H']}px  ({g['cols']} cols x {g['grid_rows']} rows, "
        f"tile {g['tile_w']}x{g['tile_h']})"
    )

    canvas = Image.new("RGB", (g["W"], g["H"]), (18, 18, 20))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except Exception:
        font = ImageFont.load_default()
    placeholder = Image.new("RGB", (g["sub"], g["sub"]), (40, 40, 44))

    for idx, (p, reps) in enumerate(plan):
        _draw_tile(canvas, draw, Image, font, placeholder, p, reps, crops, g, idx, args.per)
        if (idx + 1) % 200 == 0:
            print(f"  laid out {idx + 1}/{n}")

    out = Path(args.out)
    canvas.save(out, "PNG")
    print(f"wrote {out.resolve()}  ({out.stat().st_size / 1e6:.1f} MB, {g['W']}x{g['H']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
