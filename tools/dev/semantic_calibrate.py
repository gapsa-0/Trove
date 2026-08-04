#!/usr/bin/env python3
"""Calibrate the semantic-search relevance cuts against a real archive.

Semantic search hides weak matches with two numbers (``config.py``):

    semantic_search_min_similarity   binds where a query's best score is low
    semantic_search_relative_floor   fraction of the query's own best score

They cannot be reasoned out, because SigLIP's cosine similarities are both
compressed *and* query-dependent, which leaves the two populations overlapping:
"a dog" tops out at 0.0916 on an archive full of dogs while "the surface of
mars", which it holds none of, reaches 0.0948. No single absolute threshold
separates those. The pair survives it by binding on different populations --
see ``config.py`` for which does what.

This tool prints the evidence needed to re-tune both, on whatever is actually
indexed:

    python3 tools/dev/semantic_calibrate.py                    # default archive 1
    python3 tools/dev/semantic_calibrate.py --archive 2 --sample 20000
    python3 tools/dev/semantic_calibrate.py --no-center        # uncentered scale

Scores are centered by default, matching ``semantic_search_center_embeddings``.
That matters: centering roughly triples the score range, so a floor read off an
uncentered run is not comparable to the shipped one.

It reads the catalogue read-only and writes nothing. Add ``--queries FILE`` (one
per line) to calibrate against your own searches rather than the built-in set —
which is the point, since the right cut depends on what you actually ask for.
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

# English, because the GUI translates a Spanish query to English before
# embedding it (``localEnglishTranslation`` in web/static/js/search.js). Keep
# them English: measuring in a language the search never actually receives
# would tune the floors against the wrong distribution -- and the gap is not
# small. SigLIP 2 is multilingual but trained 90% on English, so the same
# subject scores far lower asked in Spanish ("forest" 0.348 vs "bosque" 0.130
# on a real archive, an article recovering almost none of it). That magnitude
# gap is the second reason the translator exists, alongside the screenshot
# hijack documented at its definition.
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
#
# Choose these carefully: an earlier list included "a chemistry laboratory with
# test tubes", which on a real archive returned genuine chromatography plates, a
# petri dish and a page of an immunology textbook, because the owner had studied
# biology. A control that the archive quietly *does* contain scores its own
# signal as noise and makes every floor below it look better than it is. Prefer
# subjects no domestic camera plausibly captures.
ABSENT_QUERIES = [
    "a polar bear on ice",
    "a coral reef with tropical fish",
    "a hot air balloon festival",
    "an astronaut spacewalk",
    "a sumo wrestling match",
    "a tank in a war zone",
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
    ap.add_argument(
        "--no-center",
        action="store_true",
        help="score without modality-gap centering (a different, narrower scale)",
    )
    args = ap.parse_args()

    import numpy as np

    from organize_archive.services import semantic

    cfg = Config.load()
    db_path = cfg.archive_db_path(args.archive)
    if not Path(db_path).is_file():
        raise SystemExit(f"no archive database at {db_path}")

    vectors, _ids = load_vectors(db_path, args.archive, args.sample)
    centering = not args.no_center
    print(f"{len(vectors)} embeddings, {vectors.shape[1]}-d, from {db_path}")
    print(
        f"current settings: min_similarity="
        f"{cfg.semantic_search_min_similarity}  relative_floor="
        f"{cfg.semantic_search_relative_floor}"
        f"  centering={'on' if centering else 'OFF (--no-center)'}\n"
    )

    queries = (
        [q.strip() for q in args.queries.read_text("utf-8").splitlines() if q.strip()]
        if args.queries
        else DEFAULT_QUERIES
    )
    # The process-wide singleton rather than a fresh backend: semantic.text_center
    # below goes through it too, and the text tower is 283 MB to load twice.
    backend = semantic.backend(cfg)
    backend.load_text(log=print)

    def embed(texts):
        """Text vectors on the same scale the shipped search scores on."""
        matrix = backend.embed_texts(texts)
        if centering:
            matrix = matrix - np.asarray(semantic.text_center(cfg), dtype="float32")
        return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)

    vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    if centering:
        # Same shift the read path applies (services/search.py archive_center);
        # recomputed here from whatever this run sampled rather than imported,
        # so --sample stays self-consistent.
        vectors = vectors - vectors.mean(axis=0)
        vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    real = vectors @ embed(queries).T
    absent = vectors @ embed(ABSENT_QUERIES).T

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
            "  -> the two overlap, so no single absolute floor separates them.\n"
            "     Each cut binds on a different population instead: the floor\n"
            "     where the best score is low, the ratio where it is high.\n"
        )

    print("Median results kept per query, for score >= max(floor, ratio*best):")
    # The two modes live on different scales -- centering roughly triples the
    # range -- so sweep the floors that are actually near the decision here.
    ratios = (0.55, 0.60, 0.65, 0.70, 0.75) if centering else (0.70, 0.75, 0.80, 0.85, 0.90)
    floors = (0.0, 0.15, 0.18, 0.20, 0.22) if centering else (0.0, 0.04, 0.05, 0.06, 0.07)
    print("  floor   " + "".join(f"{r:>12.2f}" for r in ratios))
    for floor in floors:
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
