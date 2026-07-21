#!/usr/bin/env python3
"""2D UMAP scatter of face embeddings, one dot per face, colored by cluster.

Projects the 128-d face embeddings to 2D with UMAP (cosine metric) and draws a
scatter where each point is a face and its color is its current cluster
(person_id). The same identity split across several clusters shows up as
adjacent blobs of *different* colors — the geometry you wanted to see.

Only assigned faces (person_id NOT NULL) are plotted by default. The 2D coords
are cached to a .npz keyed by the face-id set, so re-styling the plot is instant.

    python3 tools/faces_umap_plot.py [out.png] [--neighbors 15] [--min-dist 0.1]
                                     [--label-top 40] [--svg] [--all]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from organize_archive.config import Config          # noqa: E402
from organize_archive.db import database as db       # noqa: E402


def _color_for(pid: int):
    """Stable, well-spread RGB for a cluster id (golden-angle hue)."""
    import colorsys
    if pid is None:
        return (0.6, 0.6, 0.6)
    h = (pid * 0.61803398875) % 1.0            # golden ratio → spread hues
    # vary S/V a bit by id so neighboring hues still separate
    s = 0.55 + 0.35 * ((pid // 7) % 3) / 2.0
    v = 0.75 + 0.20 * ((pid // 3) % 2)
    return colorsys.hsv_to_rgb(h, s, v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default="faces_umap.png")
    ap.add_argument("--neighbors", type=int, default=15)
    ap.add_argument("--min-dist", type=float, default=0.1)
    ap.add_argument("--label-top", type=int, default=40,
                    help="annotate centroids of the N biggest clusters with #id")
    ap.add_argument("--all", action="store_true",
                    help="include unassigned faces too (drawn as faint gray)")
    ap.add_argument("--svg", action="store_true", help="also write an .svg")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--size", type=float, default=18.0, help="figure inches")
    ap.add_argument("--point", type=float, default=3.0, help="dot size")
    ap.add_argument("--recompute", action="store_true", help="ignore cached coords")
    args = ap.parse_args()

    import numpy as np

    cfg = Config.load()
    conn = db.open_readonly(cfg.db_path)

    where = "f.hidden = 0" if args.all else "f.hidden = 0 AND fa.person_id IS NOT NULL"
    rows = conn.execute(
        f"""SELECT fa.id AS fid, fa.person_id, fa.embedding
            FROM faces fa JOIN files f ON f.id = fa.file_id
            WHERE {where} ORDER BY fa.id""").fetchall()
    n = len(rows)
    if n == 0:
        print("No faces to plot.")
        return 1

    fids = np.array([r["fid"] for r in rows], dtype="int64")
    pids = np.array([r["person_id"] if r["person_id"] is not None else -1
                     for r in rows], dtype="int64")
    X = np.empty((n, 128), dtype="float32")
    for i, r in enumerate(rows):
        X[i] = np.frombuffer(r["embedding"], dtype="float32")
    X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9

    # ---- UMAP (cached by face-id set + params) ----------------------------
    key = hashlib.md5(
        fids.tobytes() + f"{args.neighbors}-{args.min_dist}".encode()).hexdigest()[:12]
    cache = Path(cfg.cache_dir) / f"umap_faces_{key}.npz"
    if cache.exists() and not args.recompute:
        emb = np.load(cache)["xy"]
        print(f"loaded cached UMAP coords: {cache.name}")
    else:
        import umap
        print(f"running UMAP on {n} faces (neighbors={args.neighbors}, "
              f"min_dist={args.min_dist}, cosine) …")
        emb = umap.UMAP(n_neighbors=args.neighbors, min_dist=args.min_dist,
                        metric="cosine", random_state=42).fit_transform(X)
        np.savez_compressed(cache, xy=emb)
        print(f"cached coords → {cache.name}")

    # ---- plot -------------------------------------------------------------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    counts: dict[int, int] = {}
    for p in pids:
        counts[int(p)] = counts.get(int(p), 0) + 1

    colors = np.array([_color_for(int(p)) if p >= 0 else (0.5, 0.5, 0.5)
                       for p in pids])

    fig, ax = plt.subplots(figsize=(args.size, args.size), dpi=args.dpi)
    ax.set_facecolor("#0f0f12")
    fig.patch.set_facecolor("#0f0f12")

    if args.all:
        noise = pids < 0
        ax.scatter(emb[noise, 0], emb[noise, 1], s=args.point * 0.6,
                   c="#333338", linewidths=0, alpha=0.5, rasterized=True)
        sel = ~noise
    else:
        sel = np.ones(n, dtype=bool)
    ax.scatter(emb[sel, 0], emb[sel, 1], s=args.point, c=colors[sel],
               linewidths=0, alpha=0.85, rasterized=True)

    # annotate the biggest clusters at their centroid
    top = sorted((c for c in counts if c >= 0), key=lambda p: -counts[p])[:args.label_top]
    for p in top:
        m = pids == p
        cx, cy = emb[m, 0].mean(), emb[m, 1].mean()
        ax.text(cx, cy, f"#{p}", color="white", fontsize=8, ha="center",
                va="center", weight="bold",
                path_effects=None,
                bbox=dict(boxstyle="round,pad=0.15", fc=(0, 0, 0, 0.55),
                          ec="none"))

    n_clusters = len([c for c in counts if c >= 0])
    ax.set_title(
        f"Face embeddings · UMAP 2D · {sel.sum():,} faces in {n_clusters:,} clusters "
        f"(color = cluster, #id = {args.label_top} biggest)",
        color="white", fontsize=13)
    ax.set_xticks([]); ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)
    fig.tight_layout()

    out = Path(args.out)
    fig.savefig(out, facecolor=fig.get_facecolor())
    print(f"wrote {out.resolve()}  ({out.stat().st_size/1e6:.1f} MB)")
    if args.svg:
        svg = out.with_suffix(".svg")
        fig.savefig(svg, facecolor=fig.get_facecolor())
        print(f"wrote {svg.resolve()}  ({svg.stat().st_size/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
