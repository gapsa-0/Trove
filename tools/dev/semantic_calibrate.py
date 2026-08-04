#!/usr/bin/env python3
"""Calibrate the semantic-search relevance cuts against a real archive.

Semantic search hides weak matches with two numbers (``config.py``):

    semantic_search_min_similarity   absolute floor, noise guard only
    semantic_search_relative_floor   fraction of the query's own best score

They cannot be reasoned out, because SigLIP's cosine similarities are both
compressed *and* query-dependent. Measured on this archive: a good match sits
around 0.10-0.15 and the median image-query pair sits at 0.05, but the best match
for "fireworks" (0.097) scores lower than an irrelevant best match for "an
underwater submarine" (0.092) — so an absolute threshold cannot separate
relevant from irrelevant at all. The relative floor is what does the work.

This tool prints the evidence needed to re-tune both, on whatever is actually
indexed:

    python3 tools/dev/semantic_calibrate.py                    # default archive 1
    python3 tools/dev/semantic_calibrate.py --archive 2 --sample 20000

It reads the catalogue read-only and writes nothing. Add ``--queries FILE`` (one
per line) to calibrate against your own searches rather than the built-in set —
which is the point, since the right cut depends on what you actually ask for.

See plan/local-semantic-embeddings.md §0 and §7.
"""

from __future__ import annotations

import argparse
import random
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from organize_archive.config import Config
from organize_archive.db import database as db

# English, but only by convention: SigLIP 2's text tower is multilingual, and
# the GUI now embeds whatever the user typed (the local Spanish->English
# translation it used to run was removed). Pass --queries to calibrate against
# the language and phrasing you actually search in.
DEFAULT_QUERIES = [
    "birthday cake",
    "a wedding",
    "the beach",
    "snowy mountains",
    "a dog",
    "a cat",
    "food on a table",
    "a screenshot",
    "a scanned document",
    "a newborn baby",
    "a car",
    "flowers",
    "fireworks",
    "a sunset",
    "people dancing",
    "a lake",
    "a barbecue",
    "a christmas tree",
    "a graduation ceremony",
    "a selfie",
    "swimming in the pool",
    "a football match",
    "snow",
    "an old building",
    "a painting",
    "people in a restaurant",
    "a motorcycle",
    "a forest",
    "a party at night",
    "a horse",
]

# Subjects this kind of family archive does not contain. A usable setting has to
# return little for these at the same time as it returns plenty for the above --
# they are the control group, and the reason the absolute floor cannot be the
# only cut.
ABSENT_QUERIES = [
    "an underwater submarine",
    "a chemistry laboratory with test tubes",
    "the surface of mars",
    "an aerial view of a skyscraper city at night",
]


def load_vectors(db_path, root_id, sample, seed=20260729):
    """(N, 768) float32 matrix of the archive's current embeddings."""
    import numpy as np

    conn = db.open_readonly(db_path)
    try:
        rows = conn.execute(
            """SELECT e.file_id, e.embedding, e.dimensions
                 FROM semantic_embeddings e JOIN files f ON f.id=e.file_id
                WHERE f.present=1 AND f.hidden=0 AND e.status='indexed'
                  AND e.embedding IS NOT NULL AND f.root_id=?
                  AND COALESCE(e.indexer_version,'')=?""",
            (root_id, _indexer_version()),
        ).fetchall()
    finally:
        conn.close()
    if sample and len(rows) > sample:
        rows = random.Random(seed).sample(rows, sample)
    out, ids = [], []
    for row in rows:
        dims = row["dimensions"] or 0
        try:
            out.append(struct.unpack(f"<{dims}f", row["embedding"]))
        except struct.error:
            continue  # a vector from a different model; not comparable
        ids.append(row["file_id"])
    if not out:
        raise SystemExit(
            "nothing indexed with the current embedder yet — run the semantic "
            "stage first (open the app and let the pipeline drain)"
        )
    return np.asarray(out, dtype="float32"), ids


def _indexer_version():
    from organize_archive.services import semantic

    return semantic.INDEXER_VERSION


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--archive", type=int, default=1, help="archive id (default 1)")
    ap.add_argument(
        "--sample", type=int, default=0, help="score only N random embeddings (0 = all)"
    )
    ap.add_argument(
        "--queries", type=Path, help="file of queries, one per line, replacing the defaults"
    )
    args = ap.parse_args()

    import numpy as np

    from organize_archive.embeddings import backend as eb

    cfg = Config.load()
    db_path = cfg.archive_db_path(args.archive)
    if not Path(db_path).is_file():
        raise SystemExit(f"no archive database at {db_path}")

    vectors, _ids = load_vectors(db_path, args.archive, args.sample)
    print(f"{len(vectors)} embeddings, {vectors.shape[1]}-d, from {db_path}")
    print(
        f"current settings: min_similarity="
        f"{cfg.semantic_search_min_similarity}  relative_floor="
        f"{cfg.semantic_search_relative_floor}\n"
    )

    queries = (
        [q.strip() for q in args.queries.read_text("utf-8").splitlines() if q.strip()]
        if args.queries
        else DEFAULT_QUERIES
    )
    backend = eb.SiglipBackend(cfg.cache_dir)
    backend.load_text(log=print)
    real = vectors @ backend.embed_texts(queries).T
    absent = vectors @ backend.embed_texts(ABSENT_QUERIES).T

    flat = real.reshape(-1)
    print("Score distribution over every query-image pair:")
    for p in (1, 25, 50, 75, 90, 95, 99, 99.9):
        print(f"  p{p:<6}{np.percentile(flat, p):8.4f}")
    print(f"  max    {flat.max():8.4f}    min {flat.min():8.4f}\n")

    tops, absent_tops = real.max(axis=0), absent.max(axis=0)
    print(
        f"Best score per query — real:   min {tops.min():.4f}  "
        f"median {np.median(tops):.4f}  max {tops.max():.4f}"
    )
    print(
        f"                       absent: min {absent_tops.min():.4f}  "
        f"median {np.median(absent_tops):.4f}  max {absent_tops.max():.4f}"
    )
    if absent_tops.max() >= tops.min():
        print(
            "  -> the two overlap, so no absolute floor can separate them;\n"
            "     the relative floor is doing the real work.\n"
        )

    print("Median results kept per query, for score >= max(floor, ratio*best):")
    ratios = (0.70, 0.75, 0.80, 0.85, 0.90)
    print("  floor   " + "".join(f"{r:>12.2f}" for r in ratios))
    for floor in (0.0, 0.04, 0.05, 0.06, 0.07):
        cells = []
        for ratio in ratios:
            keeps = [
                (real[:, i] >= max(floor, ratio * float(real[:, i].max()))).sum()
                for i in range(real.shape[1])
            ]
            drops = [
                (absent[:, i] >= max(floor, ratio * float(absent[:, i].max()))).sum()
                for i in range(absent.shape[1])
            ]
            cells.append(f"{int(np.median(keeps)):>6d}/{int(np.median(drops)):<5d}")
        print(f"  {floor:.2f}    " + "".join(f"{c:>12s}" for c in cells))
    print(
        "\n  each cell is  real/absent  — want the left number comfortably "
        "large\n  and the right number small. Set both in config.json."
    )


if __name__ == "__main__":
    main()
