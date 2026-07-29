"""Local web server for the archive management app (stdlib only).

Binds to localhost. Files are served by database id (path looked up in the DB,
never taken from the request), so there is no path-traversal surface.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from ..config import Config, discard_superseded_secrets
from ..db import database as db
from . import queries, thumbs, icons
from .jobs import JobManager

_INDEX = Path(__file__).with_name("index.html")
_CHUNK = 256 * 1024

_MANIFEST = {
    "name": "Trove", "short_name": "Trove",
    "start_url": "/", "scope": "/", "display": "standalone",
    "background_color": "#f5f5f7", "theme_color": "#f5f5f7",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
    ],
}
_SW = ("self.addEventListener('install',e=>self.skipWaiting());\n"
       "self.addEventListener('activate',e=>self.clients.claim());\n"
       "self.addEventListener('fetch',e=>{});\n")


class Handler(BaseHTTPRequestHandler):
    cfg: Config = None
    jobs: JobManager = None

    def log_message(self, fmt, *args):
        pass

    # -- response helpers -------------------------------------------------
    def _json(self, obj, status=200):
        # Archive state changes through POST requests, so serving a heuristic
        # browser cache entry here makes a completed add/remove appear to have
        # done nothing until another navigation happens.
        self._bytes(json.dumps(obj).encode(), "application/json", status,
                    cache_control="no-store")

    def _bytes(self, body: bytes, content_type: str, status=200, cache_control=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, path: Path, content_type=None, cache_control=None):
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1): start = int(m.group(1))
                if m.group(2): end = min(int(m.group(2)), size - 1)
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # -- per-archive resolution ---------------------------------------------
    # Each archive is a fully separate database and cache dir; every request
    # that touches archive content must say which one. Content routes that
    # take no ``root`` query param (thumbnails, the original file) instead
    # resolve against whichever archive is currently open in the GUI, since
    # only one is ever browsed at a time.
    def _db(self, root_id) -> str:
        if root_id is None:
            raise ValueError("root is required")
        return self.cfg.archive_db_path(root_id)

    def _cache(self, root_id) -> str:
        if root_id is None:
            raise ValueError("root is required")
        return self.cfg.archive_cache_dir(root_id)

    # -- GET --------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        def one(k, cast=str, default=None):
            v = q.get(k, [None])[0]
            return cast(v) if v not in (None, "") else default

        def many(k, cast=int):
            """Read repeatable or comma-separated query values, preserving order."""
            out = []
            for value in q.get(k, []):
                for part in value.split(","):
                    if part and (item := cast(part)) not in out:
                        out.append(item)
            return out

        try:
            if path == "/api/health":
                from .. import __version__
                self._json({"ok": True, "version": __version__,
                            "commit": os.environ.get("ARCHIVE_BUILD_COMMIT", "dev")})
            elif path in ("/", "/index.html"):
                # Never cache the app shell, so a server update takes effect on a
                # plain reload (no hard-refresh needed to shake off stale JS).
                self._send_file(_INDEX, "text/html; charset=utf-8",
                                cache_control="no-store")
            elif path == "/manifest.webmanifest":
                self._bytes(json.dumps(_MANIFEST).encode(), "application/manifest+json")
            elif path == "/sw.js":
                self._bytes(_SW.encode(), "text/javascript")
            elif path.startswith("/icon-"):
                size = 512 if "512" in path else 192
                self._bytes(icons.app_icon(self.cfg.cache_dir, size), "image/png")
            elif path.startswith("/vendor/"):
                name = path.rsplit("/", 1)[1]
                vf = (Path(__file__).with_name("vendor") / name)
                if vf.is_file() and ".." not in name:
                    self._send_file(vf)
                else:
                    self._json({"error": "not found"}, 404)
            elif path == "/api/archives":
                self._json({"archives": queries.archives(self.cfg)})
            elif path == "/api/settings":
                self._json({})
            elif path == "/api/summary":
                rid = one("root", int)
                self._json(queries.summary(self._db(rid), rid))
            elif path == "/api/timeline":
                rid = one("root", int)
                self._json(queries.timeline(
                    self._db(rid), root_id=rid,
                    bucket=one("bucket", str, "month"), year=one("year"),
                    month=one("month"), person_ids=many("person"),
                    cluster_id=one("place", int)))
            elif path == "/api/dates/sources":
                rid = one("root", int)
                self._json(queries.date_sources(self._db(rid), rid))
            elif path == "/api/map/clusters":
                rid = one("root", int)
                self._json(queries.place_clusters(
                    self._db(rid), rid, self.cfg.place_min_media))
            elif path == "/api/map/cluster/merge-preview":
                # GET, not POST: this mutates nothing, it only answers "how
                # spread out would this merge be" so the GUI can decide
                # whether to warn before the user confirms the drag-merge.
                rid = one("root", int)
                res = queries.place_merge_preview(
                    self._db(rid), one("a", int), one("b", int),
                    self.cfg.place_merge_warn_km)
                self._json(res, 400 if "error" in res else 200)
            elif path.startswith("/api/map/cluster/"):
                rid = one("root", int)
                c = queries.place_cluster_members(
                    self._db(rid), int(path.rsplit("/", 1)[1]),
                    limit=min(one("limit", int, 120), 500),
                    offset=one("offset", int, 0))
                self._json(c) if c else self._json({"error": "not found"}, 404)
            elif path == "/api/faces/summary":
                rid = one("root", int)
                self._json(queries.face_summary(
                    self._db(rid), rid, self.cfg.detect_video_frames))
            elif path == "/api/pets/summary":
                from ..pets.extract import scan_source as pet_scan_source
                rid = one("root", int)
                self._json(queries.pet_summary(
                    self._db(rid), rid, pet_scan_source(self.cfg),
                    self.cfg.detect_video_frames))
            elif path == "/api/pets":
                rid = one("root", int)
                self._json(queries.pet_groups(
                    self._db(rid), rid,
                    limit=min(one("limit", int, 120), 500),
                    offset=one("offset", int, 0)))
            elif path == "/api/pet/detections":
                rid = one("root", int)
                self._json(queries.animal_gallery(
                    self._db(rid), rid,
                    limit=min(one("limit", int, 120), 500),
                    offset=one("offset", int, 0),
                    unassigned=bool(one("unassigned", int, 0))))
            elif path == "/api/nonhuman":
                rid = one("root", int)
                self._json(queries.nonhuman_review(
                    self._db(rid), rid,
                    limit=min(one("limit", int, 120), 500),
                    offset=one("offset", int, 0)))
            elif path.startswith("/api/pet/"):
                rid = one("root", int)
                result = queries.pet_group(
                    self._db(rid), int(path.rsplit("/", 1)[1]),
                    rid, limit=min(one("limit", int, 120), 500),
                    offset=one("offset", int, 0))
                self._json(result) if result else self._json({"error": "not found"}, 404)
            elif path == "/api/faces/persons":
                rid = one("root", int)
                self._json(queries.face_persons(
                    self._db(rid), rid,
                    limit=min(one("limit", int, 120), 500), offset=one("offset", int, 0)))
            elif path == "/api/faces/suggestions":
                rid = one("root", int)
                self._json(queries.person_suggestions(
                    self._db(rid), rid, limit=min(one("limit", int, 40), 200)))
            elif path.startswith("/api/faces/person/"):
                rid = one("root", int)
                p2 = queries.face_person(
                    self._db(rid), int(path.rsplit("/", 1)[1]), rid,
                    limit=min(one("limit", int, 120), 500), offset=one("offset", int, 0))
                self._json(p2) if p2 else self._json({"error": "not found"}, 404)
            elif path == "/api/dups/summary":
                rid = one("root", int)
                self._json(queries.dup_summary(self._db(rid), rid))
            elif path == "/api/dups":
                rid = one("root", int)
                self._json(queries.dup_groups(
                    self._db(rid), rid,
                    limit=min(one("limit", int, 60), 200), offset=one("offset", int, 0)))
            elif path == "/api/media":
                rid = one("root", int)
                # Absent means "no filter"; only the two explicit values narrow
                # the grid, so a stray ?indexed=maybe cannot silently hide media
                # the user asked to see.
                indexed = {"yes": True, "no": False}.get(one("indexed"))
                located = {"yes": True, "no": False}.get(one("located"))
                self._json(queries.media(
                    self._db(rid), root_id=rid,
                    year=one("year"), month=one("month"), mtype=one("type"),
                    person_ids=many("person"), cluster_id=one("place", int),
                    sort="oldest" if one("sort") == "oldest" else "newest",
                    limit=min(one("limit", int, 120), 500), offset=one("offset", int, 0),
                    indexed=indexed, located=located))
            elif path == "/api/browse/filters":
                rid = one("root", int)
                self._json(queries.browse_filters(self._db(rid), rid))
            elif path == "/api/folders":
                rid = one("root", int)
                self._json(queries.folders(self._db(rid), rid,
                                           limit=min(one("limit", int, 120), 500)))
            elif path == "/api/browse/semantic/status":
                from . import semantic
                rid = one("root", int)
                status = queries.semantic_summary(self._db(rid), rid)
                # Nothing left to configure — the stage runs as soon as the
                # dependencies are importable, and downloads its own weights.
                status["configured"] = semantic.available()
                self._json(status)
            elif path == "/api/browse/semantic/search":
                search_queries = []
                for value in q.get("q", []):
                    value = value.strip()
                    if value and value not in search_queries:
                        search_queries.append(value)
                # The first query is the user's wording.  At most one locally
                # translated expansion is accepted to keep ranking predictable.
                search_queries = search_queries[:2]
                if not search_queries:
                    self._json({"error": "A search query is required"}, 400)
                else:
                    from . import semantic
                    rid = one("root", int)
                    db_path = self._db(rid)
                    vectors = semantic.embed_queries(self.cfg, search_queries)
                    self._json(queries.semantic_search(
                        db_path, vectors[0], root_id=rid,
                        year=one("year"), month=one("month"), mtype=one("type"),
                        person_ids=many("person"), cluster_id=one("place", int),
                        min_similarity=max(-1.0, min(1.0, float(
                            self.cfg.semantic_search_min_similarity))),
                        relative_floor=max(0.0, min(1.0, float(
                            self.cfg.semantic_search_relative_floor))),
                        sort=(one("sort") if one("sort") in ("newest", "oldest")
                              else "relevance"),
                        limit=min(one("limit", int, 120), 500), offset=one("offset", int, 0),
                        located={"yes": True, "no": False}.get(one("located")),
                        alternate_vectors=[
                            (vector, queries.ALTERNATE_VECTOR_PENALTY)
                            for vector in vectors[1:]]))
            elif path.startswith("/api/item/"):
                rid = self.jobs.current_root_id()
                it = queries.item(self._db(rid), int(path.rsplit("/", 1)[1]),
                                  self.cfg.place_min_media) if rid else None
                self._json(it) if it else self._json({"error": "not found"}, 404)
            elif path == "/api/pipeline":
                # Single source of truth for pipeline status: the same resolved
                # stage list the scheduler acts on, so cards never disagree with
                # what's actually running.
                from . import pipeline
                rid = one("root", int)
                arch = next((a for a in queries.archives(self.cfg)
                             if a["id"] == rid), None)
                if arch is None:
                    self._json({"error": "unknown archive"}, 404)
                else:
                    self._json(pipeline.snapshot(self.cfg, self.jobs, rid, arch["path"]))
            elif path.startswith("/archivethumb/"):
                parts = path.split("/")   # ['', 'archivethumb', root_id, file_id]
                if len(parts) == 4 and parts[2].isdigit() and parts[3].isdigit():
                    self._serve_archive_thumb(int(parts[2]), int(parts[3]))
                else:
                    self._json({"error": "not found"}, 404)
            elif path.startswith("/thumb/"):
                self._serve_thumb(int(path.rsplit("/", 1)[1]))
            elif path.startswith("/faceThumb/"):
                self._serve_face_thumb(int(path.rsplit("/", 1)[1]))
            elif path.startswith("/animalThumb/"):
                self._serve_animal_thumb(int(path.rsplit("/", 1)[1]))
            elif path.startswith("/file/"):
                self._serve_original(int(path.rsplit("/", 1)[1]))
            else:
                self._json({"error": "not found"}, 404)
        except (ValueError, BrokenPipeError):
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        try:
            body = self._read_json_body()
            if path == "/api/archives":
                res = queries.add_archive(self.cfg, body.get("path", ""))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/archive/open":
                root_id = body.get("root_id")
                if not isinstance(root_id, int):
                    self._json({"error": "root_id is required"}, 400)
                elif not any(a["id"] == root_id and a["exists"]
                             for a in queries.archives(self.cfg)):
                    self._json({"error": "archive not found or unavailable"}, 404)
                else:
                    self.jobs.open_archive(root_id)
                    self._json({"ok": True})
            elif path == "/api/archive/close":
                root_id = body.get("root_id")
                self.jobs.close_archive(root_id if isinstance(root_id, int) else None)
                self._json({"ok": True})
            elif path == "/api/archive/remove":
                root_id = body.get("root_id")
                if not isinstance(root_id, int):
                    self._json({"error": "root_id is required"}, 400)
                elif not self.jobs.stop_archive(root_id):
                    self._json({"error": "archive is still stopping; try again shortly"}, 409)
                else:
                    res = queries.remove_archive(self.cfg, root_id)
                    self._json(res, 400 if "error" in res else 200)
            elif path == "/api/pipeline/pause":
                # Without "stage" this is the whole-pipeline switch; with one it
                # pauses that single card (scan/dedup/detect/places/semantic) and
                # leaves the rest of the pipeline running.
                from . import pipeline
                paused = body.get("paused")
                stage = body.get("stage")
                if not isinstance(paused, bool):
                    self._json({"error": "paused (bool) is required"}, 400)
                elif stage is not None and stage not in pipeline.CARD_ORDER:
                    self._json({"error": f"unknown stage: {stage}"}, 400)
                elif stage is None:
                    self.jobs.set_paused(paused)
                    self._json({"paused": self.jobs.paused()})
                else:
                    self.jobs.set_stage_paused(stage, paused)
                    self._json({"paused": self.jobs.paused(),
                                "paused_stages": sorted(self.jobs.paused_stages())})
            # The mutations below act on an id (person, cluster, face, pet...)
            # rather than a file the caller already knows the root of, and the
            # frontend never sends one for them. They resolve against whichever
            # archive is currently open, same as thumbnail/original serving.
            elif path == "/api/map/cluster/rename":
                res = db.write_with_retry(lambda: queries.rename_place_cluster(
                    self._db(self.jobs.current_root_id()),
                    body.get("cluster_id"), (body.get("name") or "").strip()))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/map/cluster/merge":
                res = db.write_with_retry(lambda: queries.merge_place_clusters(
                    self._db(self.jobs.current_root_id()), body.get("a"), body.get("b"),
                    body.get("name")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/map/cluster/unmerge":
                # Unlike /api/faces/unmerge and /api/pets/unmerge, no job is
                # started here: places are durable (see place_merges' schema
                # comment), so unmerge_place_clusters is already a complete
                # restore, not a "delete a constraint and recluster" that
                # needs a background pass to finish the job.
                res = db.write_with_retry(lambda: queries.unmerge_place_clusters(
                    self._db(self.jobs.current_root_id()), body.get("merge_id")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/person/rename":
                res = db.write_with_retry(lambda: queries.rename_person(
                    self._db(self.jobs.current_root_id()),
                    body.get("person_id"), (body.get("name") or "").strip()))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/reassign":
                res = db.write_with_retry(lambda: queries.reassign_face(
                    self._db(self.jobs.current_root_id()),
                    body.get("face_id"), body.get("person_id")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/merge":
                res = db.write_with_retry(lambda: queries.merge_persons(
                    self._db(self.jobs.current_root_id()), body.get("a"), body.get("b"),
                    body.get("name")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/unmerge":
                res = db.write_with_retry(lambda: queries.unmerge_persons(
                    self._db(self.jobs.current_root_id()), body.get("merge_id")))
                if res.get("recluster") and self.jobs.current_root_id():
                    self.jobs.start("face_cluster", self.jobs.current_root_id())
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/detach":
                res = db.write_with_retry(lambda: queries.detach_file_from_person(
                    self._db(self.jobs.current_root_id()),
                    body.get("person_id"), body.get("file_id")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/different":
                res = db.write_with_retry(lambda: queries.set_persons_different(
                    self._db(self.jobs.current_root_id()), body.get("a"), body.get("b")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/skip":
                res = db.write_with_retry(lambda: queries.set_persons_skip(
                    self._db(self.jobs.current_root_id()), body.get("a"), body.get("b")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/hide":
                res = db.write_with_retry(lambda: queries.hide_person(
                    self._db(self.jobs.current_root_id()), body.get("person_id"),
                    body.get("kind", "false_detection")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/pet/rename":
                res = db.write_with_retry(lambda: queries.rename_pet(
                    self._db(self.jobs.current_root_id()), body.get("pet_id"),
                    (body.get("name") or "").strip()))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/pets/merge":
                res = db.write_with_retry(lambda: queries.merge_pets(
                    self._db(self.jobs.current_root_id()), body.get("a"), body.get("b"),
                    body.get("name")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/pets/unmerge":
                res = db.write_with_retry(lambda: queries.unmerge_pets(
                    self._db(self.jobs.current_root_id()), body.get("merge_id")))
                if res.get("recluster") and self.jobs.current_root_id():
                    self.jobs.start("pet_cluster", self.jobs.current_root_id())
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/nonhuman/review":
                res = db.write_with_retry(lambda: queries.review_nonhuman(
                    self._db(self.jobs.current_root_id()), body.get("detection_id"),
                    body.get("verdict", "confirmed")))
                if res.get("status") == "human" and res.get("root_id"):
                    self.jobs.start("face_cluster", res["root_id"])
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/date":
                res = db.write_with_retry(lambda: queries.set_date(
                    self._db(self.jobs.current_root_id()),
                    body.get("file_id"), body.get("datetime")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/place":
                db_path = self._db(self.jobs.current_root_id())
                if body.get("clear"):
                    res = db.write_with_retry(
                        lambda: queries.clear_place(db_path, body.get("file_id")))
                else:
                    res = db.write_with_retry(lambda: queries.set_place(
                        db_path, body.get("file_id"), body.get("place_id")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/person/add":
                res = db.write_with_retry(lambda: queries.add_person_to_file(
                    self._db(self.jobs.current_root_id()),
                    body.get("person_id"), body.get("file_id")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/person/remove":
                res = db.write_with_retry(lambda: queries.remove_person_from_file(
                    self._db(self.jobs.current_root_id()),
                    body.get("person_id"), body.get("file_id")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/pet/add":
                res = db.write_with_retry(lambda: queries.add_pet_to_file(
                    self._db(self.jobs.current_root_id()),
                    body.get("pet_id"), body.get("file_id")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/pet/remove":
                res = db.write_with_retry(lambda: queries.remove_pet_from_file(
                    self._db(self.jobs.current_root_id()),
                    body.get("pet_id"), body.get("file_id")))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/places/create":
                res = db.write_with_retry(lambda: queries.create_place(
                    self._db(body.get("root")), body.get("root"), body.get("name"),
                    body.get("lat"), body.get("lon"), body.get("file_id")))
                self._json(res, 400 if "error" in res else 200)
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # -- media serving ----------------------------------------------------
    # Thumbnails and originals are requested by bare id with no ``root``
    # (the frontend never sends one for these), so they resolve against
    # whichever single archive is currently open, the GUI never browses two
    # archives' content at once.
    def _open_db_and_cache(self):
        rid = self.jobs.current_root_id()
        if rid is None:
            return None, None
        return self._db(rid), self._cache(rid)

    def _serve_thumb(self, fid: int):
        db_path, cache_dir = self._open_db_and_cache()
        info = queries.media_source(db_path, fid) if db_path else None
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, rotate = info
        tp = thumbs.thumb_for(cache_dir, fid, src, sha256=sha256, rotate=rotate)
        self._send_file(tp if tp else src)

    def _serve_archive_thumb(self, root_id: int, fid: int):
        # Root-scoped thumbnail for the start-page cover mosaic: the picker shows
        # several archives at once with none "open", so the id alone (as /thumb/
        # uses) is not enough, the archive is named explicitly here.
        if self.cfg.archive_path(root_id) is None:
            return self._json({"error": "not found"}, 404)
        db_path, cache_dir = self._db(root_id), self._cache(root_id)
        info = queries.media_source(db_path, fid)
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, rotate = info
        tp = thumbs.thumb_for(cache_dir, fid, src, sha256=sha256, rotate=rotate)
        self._send_file(tp if tp else src)

    def _serve_face_thumb(self, face_id: int):
        db_path, cache_dir = self._open_db_and_cache()
        info = queries.face_crop_source(db_path, face_id) if db_path else None
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, box, rotate, frame_offset, media_type, file_id = info
        if frame_offset is not None:
            # A video detection's box was measured in the extracted keyframe,
            # already upright (ffmpeg applies container rotation), so the
            # crop is cut from that same re-derived frame, not the video file
            # itself, and never rotated again.
            frame = thumbs.detect_frame_for(
                cache_dir, file_id, src, frame_offset,
                self.cfg.detect_video_frame_px, sha256=sha256)
            if frame is None:
                return self._json({"error": "frame unavailable"}, 404)
            tp = thumbs.face_thumb_for(cache_dir, face_id, frame, box, sha256=sha256,
                                       rotate=0, variant=frame_offset)
            return self._send_file(tp if tp else frame)
        tp = thumbs.face_thumb_for(cache_dir, face_id, src, box, sha256=sha256,
                                   rotate=rotate)
        self._send_file(tp if tp else src)

    def _serve_animal_thumb(self, detection_id: int):
        db_path, cache_dir = self._open_db_and_cache()
        info = queries.animal_crop_source(db_path, detection_id) if db_path else None
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, box, rotate, frame_offset, media_type, file_id = info
        if frame_offset is not None:
            frame = thumbs.detect_frame_for(
                cache_dir, file_id, src, frame_offset,
                self.cfg.detect_video_frame_px, sha256=sha256)
            if frame is None:
                return self._json({"error": "frame unavailable"}, 404)
            tp = thumbs.face_thumb_for(
                cache_dir, detection_id, frame, box, sha256=sha256, rotate=0,
                variant=frame_offset)
            return self._send_file(tp if tp else frame)
        tp = thumbs.face_thumb_for(
            cache_dir, detection_id, src, box, sha256=sha256, rotate=rotate)
        self._send_file(tp if tp else src)

    def _serve_original(self, fid: int):
        db_path, cache_dir = self._open_db_and_cache()
        info = queries.media_source(db_path, fid) if db_path else None
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, rotate = info
        # A photo stored sideways is served from an upright re-encode; every
        # other file is served as its own untouched bytes.
        up = thumbs.upright_for(cache_dir, fid, src, rotate, sha256=sha256)
        self._send_file(up if up else src)


def serve(cfg: Config, host="127.0.0.1", port=8756):
    # Only shared, non-archive resources (ML models, the app icon) live at the
    # top level now; each archive's own database is created when it's added
    # (queries.add_archive) or opened for the first time by the scheduler.
    cfg.ensure_dirs()
    cfg.migrate_legacy_archive()
    discard_superseded_secrets()
    from . import semantic
    # Loading the 283 MB text tower takes a second or two. Do it off the serving
    # thread now so the user's first search doesn't wait for it; a search that
    # arrives sooner simply blocks on the same lock and gets the warmed session.
    threading.Thread(target=semantic.warm_text_model, args=(cfg,),
                     name="semantic-warm", daemon=True).start()
    jm = JobManager(cfg)
    handler = type("BoundHandler", (Handler,), {"cfg": cfg, "jobs": jm})
    return ThreadingHTTPServer((host, port), handler)
