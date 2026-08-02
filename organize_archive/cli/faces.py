"""The `oa faces` command: detect faces locally and cluster them into people."""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..db import database as db
from .progress import ScanProgress


def add_parser(sub) -> None:
    sp = sub.add_parser("faces", help="Detect faces (local) and cluster them into people")
    sp.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only scan this many pending images this run (resumable)",
    )
    sp.add_argument(
        "--recluster",
        action="store_true",
        help="Skip detection; just re-cluster existing faces into people",
    )
    sp.add_argument(
        "--quality-report",
        action="store_true",
        help="Show persisted face quality/rejection diagnostics and exit",
    )
    sp.add_argument(
        "--calibrate",
        type=int,
        nargs="?",
        const=100,
        metavar="N",
        help="Dry-run current quality gates on N pending images (default 100)",
    )
    sp.add_argument(
        "--recalibrate-fiqa",
        action="store_true",
        help="Recompute the FIQA calibration from all stored feature "
        "norms and re-tier every face (no re-embedding). Run "
        "after changing faces_fiqa_* thresholds, then --recluster",
    )
    sp.add_argument(
        "--migrate-adaface",
        action="store_true",
        help="Back up the database, preserve names/pins/links, and "
        "clear the old embeddings so the next run re-extracts "
        "with AdaFace (required once after the embedder change)",
    )
    sp.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    sp.set_defaults(func=run)


def _progress(args, label: str):
    """A progress bar for one phase, or None when --no-progress is given."""
    if args.no_progress:
        return None
    return ScanProgress(None, show_bytes=False, label=label)


def _stage_adaface_migration(conn, cfg: Config) -> int:
    """--migrate-adaface: back up, preserve identities, clear the embeddings."""
    from ..faces import migrate_adaface

    st = migrate_adaface.snapshot_and_wipe(conn, cfg, db_path=cfg.db_path, log=print)
    conn.close()
    print("\nAdaFace migration staged:")
    print(f"  backup             : {st.backup_path}")
    print(
        f"  identities kept    : {st.faces_snapshotted:,} faces, "
        f"{st.links_snapshotted:,} links, {st.pets_snapshotted:,} pets"
    )
    print(
        "\nNow re-run `oa faces` (or the GUI pipeline) to re-extract with "
        "AdaFace, then `oa faces --recluster` to restore names and cluster."
    )
    return 0


def _recalibrate_fiqa(conn, cfg: Config) -> int:
    """--recalibrate-fiqa: re-tier every face from its stored feature norm."""
    from ..faces import fiqa

    counts = fiqa.recalibrate(conn, cfg, log=print)
    conn.close()
    print("\nRe-tiered every face from its stored feature norm:")
    for tier in fiqa.TIERS:
        print(f"  {tier:<12}: {counts[tier]:,}")
    print("\nRun `oa faces --recluster` to rebuild people from the new tiers.")
    return 0


def _quality_report(conn, fx) -> int:
    """--quality-report: the persisted detection/rejection diagnostics."""
    report = fx.quality_summary(conn)
    conn.close()
    print("Face extraction quality:")
    print(f"  images scanned     : {report['images']:,}")
    print(f"  detector candidates: {report['candidates']:,}")
    print(f"  faces accepted     : {report['accepted']:,}")
    print(f"  cluster noise      : {report['cluster_noise'] or 0:,} unassigned")
    for reason in ("score", "size", "focus", "exposure", "clipped", "nonhuman"):
        print(f"  rejected {reason:<8}: {report['rejected_' + reason]:,}")
    if report["avg_quality"] is not None:
        print(f"  mean quality score : {report['avg_quality']:.3f}")
        print(f"  mean focus score   : {report['avg_focus']:.1f}")
        print(f"  mean brightness    : {report['avg_brightness']:.1f}")
    # LOW_QUALITY faces are hidden throughout the GUI, so this report is the
    # one place their number stays visible.
    print("  FIQA tiers:")
    for tier, n in report["tiers"].items():
        print(f"    {tier:<12}: {n:,}")
    if report.get("avg_fiqa") is not None:
        print(f"  mean FIQA score    : {report['avg_fiqa']:.3f}")
        print(f"  mean feature norm  : {report['avg_fiqa_norm']:.2f}")
    return 0


def _calibrate(conn, cfg: Config, args, fx) -> int:
    """--calibrate N: dry-run the quality gates, leaving the database alone."""
    limit = max(1, args.calibrate)
    print(f"Dry-running face quality gates on up to {limit} pending image(s) …")
    progress = _progress(args, "calibrating")
    result = fx.calibrate_quality(conn, cfg, limit=limit, progress=progress)
    if progress is not None:
        progress.close()
    conn.close()
    print("\nCalibration dry run (database unchanged):")
    print(f"  images sampled     : {result.processed:,}")
    print(f"  detector candidates: {result.candidates:,}")
    print(f"  would accept       : {result.faces_found:,}")
    for reason in ("score", "size", "focus", "exposure", "clipped"):
        print(f"  reject {reason:<11}: {getattr(result, 'rejected_' + reason):,}")
    if result.errors:
        print(f"  errors             : {result.errors:,}")
    return 0


def _detect(conn, cfg: Config, args, fx) -> None:
    """The detection half of a normal run: re-extract whatever is pending."""
    from ..faces import backend, migrate_adaface

    if not backend.models_ready(cfg.cache_dir):
        print(f"Fetching face models (one-time, ~38 MB) into {cfg.cache_dir}/models …")
    # Same self-healing the GUI does when it opens an archive: if the stored
    # vectors came from a different embedder, stage the migration here so the
    # detection below refills the archive from zero. Explicit
    # --migrate-adaface stays available for staging it without detecting.
    migrate_adaface.run_if_needed(conn, cfg, db_path=cfg.db_path, log=print)
    pending = fx.pending_count(conn)
    if pending == 0:
        print("All images already face-scanned.")
        return
    cap = f" (limit {args.limit})" if args.limit else ""
    print(f"Detecting faces in {pending} image(s){cap} …")
    progress = _progress(args, "faces")
    es = fx.extract(conn, cfg, progress=progress, limit=args.limit)
    if progress is not None:
        progress.close()
    print(f"\n  images scanned    : {es.processed}")
    print(f"  faces detected    : {es.faces_found}")
    print(f"  photos with faces : {es.images_with_faces}")
    rejected = sum(
        getattr(es, f"rejected_{reason}")
        for reason in ("score", "size", "focus", "exposure", "clipped", "nonhuman")
    )
    if rejected:
        print(f"  quality rejections: {rejected}")
    if es.errors:
        print(f"  errors            : {es.errors}")
        for s in es.error_samples:
            print(f"      - {s}")


def _cluster(conn, cfg: Config, args, fc) -> int:
    """The clustering half of a normal run, plus the migration it completes."""
    # A staged AdaFace migration is completed here, once the re-extract that
    # precedes it has produced the new faces to reattach the old identities to.
    # Idempotent, so running it on every clustering pass is harmless.
    from ..faces import migrate_adaface

    if migrate_adaface.pending(conn):
        print("\nRestoring names, pins and links onto the re-extracted faces …")
        migrate_adaface.reattach(conn, cfg, log=print)

    print("\nClustering faces into people …")
    progress = _progress(args, "clustering")
    cs = fc.cluster_faces(conn, cfg, progress=progress)
    if progress is not None:
        progress.close()
    conn.close()
    print(f"\n  people found      : {cs.people}")
    print(f"  faces clustered   : {cs.clustered}")
    print(f"  unassigned faces  : {cs.noise}")
    print(f"  cores (high-q)    : {cs.cores} from {cs.high} faces")
    print(f"  borderline joined : {cs.border_assigned} of {cs.borderline}")
    print(f"  low-quality kept out: {cs.low_quality_excluded}")
    if cs.named:
        print(f"  names preserved   : {cs.named}")
    return 0


def run(args, cfg: Config) -> int:
    from ..faces import backend
    from ..faces import cluster as fc
    from ..faces import extract as fx

    if not Path(cfg.db_path).exists():
        print("No database yet. Run:  oa init  then  oa scan  then  oa enrich")
        return 1
    conn = db.connect(cfg.db_path)
    db.init_db(conn)

    # The exclusive modes, in the order they were reachable before: each one
    # closes the connection and returns rather than falling through.
    if getattr(args, "migrate_adaface", False):
        return _stage_adaface_migration(conn, cfg)
    if getattr(args, "recalibrate_fiqa", False):
        return _recalibrate_fiqa(conn, cfg)
    if args.quality_report:
        return _quality_report(conn, fx)

    if not backend.available():
        conn.close()
        print(
            "Face detection needs OpenCV's DNN face APIs. Install a modern "
            "opencv-python (the 'faces' extra) and retry."
        )
        return 1

    if args.calibrate is not None:
        return _calibrate(conn, cfg, args, fx)

    if not args.recluster:
        _detect(conn, cfg, args, fx)
    return _cluster(conn, cfg, args, fc)
