"""Local web server for the archive management app (stdlib only).

Binds to localhost. Files are served by database id (path looked up in the DB,
never taken from the request), so there is no path-traversal surface.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import thumbnails
from ..config import Config, discard_superseded_secrets
from ..db import database as db
from ..services import archives, browse, dups, overview, people, pets, places, search
from . import routes
from .jobs import JobManager

logger = logging.getLogger(__name__)

_CHUNK = 256 * 1024


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
        self._bytes(json.dumps(obj).encode(), "application/json", status, cache_control="no-store")

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
            # The client went away mid-response -- a cancelled image load, a
            # closed tab. Routine enough that logging it would be noise, and
            # there is nobody left to send an error to.
            pass

    def _send_file(self, path: Path, content_type=None, cache_control=None):
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
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

    # -- routing ------------------------------------------------------------
    # The route tables in ``routes/`` are consulted first and the if/elif chains
    # below are the fallback, so routes can move a domain at a time and the ones
    # that have not moved keep working untouched.
    #
    # ⚠️ A prefix route may only move into ``GET_PREFIX_ROUTES`` together with
    # every exact route that sits underneath it -- ``/api/map/cluster/`` with
    # ``/api/map/cluster/merge-preview``, ``/api/pet/`` with
    # ``/api/pet/detections``. Split them across two commits and the prefix
    # swallows the exact route for one commit, which no test would catch because
    # both answer 404.
    def _build_request(self, method: str, body: dict) -> routes.Request:
        u = urlparse(self.path)
        return routes.Request(
            method=method,
            path=u.path,
            query=parse_qs(u.query),
            body=body,
            cfg=self.cfg,
            jobs=self.jobs,
        )

    def _respond(self, result) -> None:
        """Serialise whatever a handler returned. A bare object is JSON 200;
        the three wrappers exist for everything else."""
        if isinstance(result, routes.FileBody):
            self._send_file(result.path, result.content_type, result.cache_control)
        elif isinstance(result, routes.Raw):
            self._bytes(result.body, result.content_type, result.status, result.cache_control)
        elif isinstance(result, routes.Json):
            self._json(result.body, result.status)
        else:
            self._json(result)

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
            handler = routes.GET_ROUTES.get(path) or routes.prefix_handler(
                routes.GET_PREFIX_ROUTES, path
            )
            if handler is not None:
                self._respond(handler(self._build_request("GET", {})))
            elif path == "/api/archives":
                self._json({"archives": archives.archives(self.cfg)})
            elif path == "/api/settings":
                self._json({})
            elif path == "/api/timeline":
                rid = one("root", int)
                self._json(
                    overview.timeline(
                        self._db(rid),
                        root_id=rid,
                        bucket=one("bucket", str, "month"),
                        year=one("year"),
                        month=one("month"),
                        person_ids=many("person"),
                        cluster_id=one("place", int),
                    )
                )
            elif path == "/api/dates/sources":
                rid = one("root", int)
                self._json(overview.date_sources(self._db(rid), rid))
            elif path == "/api/dups/summary":
                rid = one("root", int)
                self._json(dups.dup_summary(self._db(rid), rid))
            elif path == "/api/dups":
                rid = one("root", int)
                self._json(
                    dups.dup_groups(
                        self._db(rid),
                        rid,
                        limit=min(one("limit", int, 60), 200),
                        offset=one("offset", int, 0),
                    )
                )
            elif path == "/api/media":
                rid = one("root", int)
                # Absent means "no filter"; only the two explicit values narrow
                # the grid, so a stray ?indexed=maybe cannot silently hide media
                # the user asked to see.
                indexed = {"yes": True, "no": False}.get(one("indexed"))
                located = {"yes": True, "no": False}.get(one("located"))
                self._json(
                    browse.media(
                        self._db(rid),
                        root_id=rid,
                        year=one("year"),
                        month=one("month"),
                        mtype=one("type"),
                        person_ids=many("person"),
                        cluster_id=one("place", int),
                        sort="oldest" if one("sort") == "oldest" else "newest",
                        limit=min(one("limit", int, 120), 500),
                        offset=one("offset", int, 0),
                        indexed=indexed,
                        located=located,
                    )
                )
            elif path == "/api/browse/filters":
                rid = one("root", int)
                self._json(browse.browse_filters(self._db(rid), rid))
            elif path == "/api/folders":
                rid = one("root", int)
                self._json(
                    browse.folders(self._db(rid), rid, limit=min(one("limit", int, 120), 500))
                )
            elif path == "/api/browse/semantic/status":
                from ..services import semantic

                rid = one("root", int)
                status = search.semantic_summary(self._db(rid), rid)
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
                    from ..services import semantic

                    rid = one("root", int)
                    db_path = self._db(rid)
                    vectors = semantic.embed_queries(self.cfg, search_queries)
                    self._json(
                        search.semantic_search(
                            db_path,
                            vectors[0],
                            root_id=rid,
                            year=one("year"),
                            month=one("month"),
                            mtype=one("type"),
                            person_ids=many("person"),
                            cluster_id=one("place", int),
                            min_similarity=max(
                                -1.0, min(1.0, float(self.cfg.semantic_search_min_similarity))
                            ),
                            relative_floor=max(
                                0.0, min(1.0, float(self.cfg.semantic_search_relative_floor))
                            ),
                            sort=(
                                one("sort") if one("sort") in ("newest", "oldest") else "relevance"
                            ),
                            limit=min(one("limit", int, 120), 500),
                            offset=one("offset", int, 0),
                            located={"yes": True, "no": False}.get(one("located")),
                            alternate_vectors=[
                                (vector, search.ALTERNATE_VECTOR_PENALTY) for vector in vectors[1:]
                            ],
                        )
                    )
            elif path == "/api/pipeline":
                # Single source of truth for pipeline status: the same resolved
                # stage list the scheduler acts on, so cards never disagree with
                # what's actually running.
                from . import pipeline

                rid = one("root", int)
                arch = next((a for a in archives.archives(self.cfg) if a["id"] == rid), None)
                if arch is None:
                    self._json({"error": "unknown archive"}, 404)
                else:
                    self._json(pipeline.snapshot(self.cfg, self.jobs, rid, arch["path"]))
            elif path.startswith("/archivethumb/"):
                parts = path.split("/")  # ['', 'archivethumb', root_id, file_id]
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
            logger.exception("unhandled error serving %s %s", self.command, self.path)
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                # The client is already gone (broken pipe, closed tab): there is
                # nowhere left to report this failure to, so stay silent.
                pass

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        try:
            body = self._read_json_body()
            if path == "/api/archives":
                res = archives.add_archive(self.cfg, body.get("path", ""))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/archive/open":
                root_id = body.get("root_id")
                if not isinstance(root_id, int):
                    self._json({"error": "root_id is required"}, 400)
                elif not any(
                    a["id"] == root_id and a["exists"] for a in archives.archives(self.cfg)
                ):
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
                    res = archives.remove_archive(self.cfg, root_id)
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
                    self._json(
                        {
                            "paused": self.jobs.paused(),
                            "paused_stages": sorted(self.jobs.paused_stages()),
                        }
                    )
            # The mutations below act on an id (person, cluster, face, pet...)
            # rather than a file the caller already knows the root of, and the
            # frontend never sends one for them. They resolve against whichever
            # archive is currently open, same as thumbnail/original serving.
            elif path == "/api/map/cluster/rename":
                res = db.write_with_retry(
                    lambda: places.rename_place_cluster(
                        self._db(self.jobs.current_root_id()),
                        body.get("cluster_id"),
                        (body.get("name") or "").strip(),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/map/cluster/merge":
                res = db.write_with_retry(
                    lambda: places.merge_place_clusters(
                        self._db(self.jobs.current_root_id()),
                        body.get("a"),
                        body.get("b"),
                        body.get("name"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/map/cluster/unmerge":
                # Unlike /api/faces/unmerge and /api/pets/unmerge, no job is
                # started here: places are durable (see place_merges' schema
                # comment), so unmerge_place_clusters is already a complete
                # restore, not a "delete a constraint and recluster" that
                # needs a background pass to finish the job.
                res = db.write_with_retry(
                    lambda: places.unmerge_place_clusters(
                        self._db(self.jobs.current_root_id()), body.get("merge_id")
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/person/rename":
                res = db.write_with_retry(
                    lambda: people.rename_person(
                        self._db(self.jobs.current_root_id()),
                        body.get("person_id"),
                        (body.get("name") or "").strip(),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/reassign":
                res = db.write_with_retry(
                    lambda: people.reassign_face(
                        self._db(self.jobs.current_root_id()),
                        body.get("face_id"),
                        body.get("person_id"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/merge":
                res = db.write_with_retry(
                    lambda: people.merge_persons(
                        self._db(self.jobs.current_root_id()),
                        body.get("a"),
                        body.get("b"),
                        body.get("name"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/unmerge":
                res = db.write_with_retry(
                    lambda: people.unmerge_persons(
                        self._db(self.jobs.current_root_id()), body.get("merge_id")
                    )
                )
                if res.get("recluster") and self.jobs.current_root_id():
                    self.jobs.start("face_cluster", self.jobs.current_root_id())
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/detach":
                res = db.write_with_retry(
                    lambda: people.detach_file_from_person(
                        self._db(self.jobs.current_root_id()),
                        body.get("person_id"),
                        body.get("file_id"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/different":
                res = db.write_with_retry(
                    lambda: people.set_persons_different(
                        self._db(self.jobs.current_root_id()), body.get("a"), body.get("b")
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/skip":
                res = db.write_with_retry(
                    lambda: people.set_persons_skip(
                        self._db(self.jobs.current_root_id()), body.get("a"), body.get("b")
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/hide":
                res = db.write_with_retry(
                    lambda: people.hide_person(
                        self._db(self.jobs.current_root_id()),
                        body.get("person_id"),
                        body.get("kind", "false_detection"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/pet/rename":
                res = db.write_with_retry(
                    lambda: pets.rename_pet(
                        self._db(self.jobs.current_root_id()),
                        body.get("pet_id"),
                        (body.get("name") or "").strip(),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/pets/merge":
                res = db.write_with_retry(
                    lambda: pets.merge_pets(
                        self._db(self.jobs.current_root_id()),
                        body.get("a"),
                        body.get("b"),
                        body.get("name"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/pets/unmerge":
                res = db.write_with_retry(
                    lambda: pets.unmerge_pets(
                        self._db(self.jobs.current_root_id()), body.get("merge_id")
                    )
                )
                if res.get("recluster") and self.jobs.current_root_id():
                    self.jobs.start("pet_cluster", self.jobs.current_root_id())
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/nonhuman/review":
                res = db.write_with_retry(
                    lambda: pets.review_nonhuman(
                        self._db(self.jobs.current_root_id()),
                        body.get("detection_id"),
                        body.get("verdict", "confirmed"),
                    )
                )
                if res.get("status") == "human" and res.get("root_id"):
                    self.jobs.start("face_cluster", res["root_id"])
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/date":
                res = db.write_with_retry(
                    lambda: browse.set_date(
                        self._db(self.jobs.current_root_id()),
                        body.get("file_id"),
                        body.get("datetime"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/place":
                db_path = self._db(self.jobs.current_root_id())
                if body.get("clear"):
                    res = db.write_with_retry(
                        lambda: places.clear_place(db_path, body.get("file_id"))
                    )
                else:
                    res = db.write_with_retry(
                        lambda: places.set_place(db_path, body.get("file_id"), body.get("place_id"))
                    )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/person/add":
                res = db.write_with_retry(
                    lambda: people.add_person_to_file(
                        self._db(self.jobs.current_root_id()),
                        body.get("person_id"),
                        body.get("file_id"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/person/remove":
                res = db.write_with_retry(
                    lambda: people.remove_person_from_file(
                        self._db(self.jobs.current_root_id()),
                        body.get("person_id"),
                        body.get("file_id"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/pet/add":
                res = db.write_with_retry(
                    lambda: pets.add_pet_to_file(
                        self._db(self.jobs.current_root_id()),
                        body.get("pet_id"),
                        body.get("file_id"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/pet/remove":
                res = db.write_with_retry(
                    lambda: pets.remove_pet_from_file(
                        self._db(self.jobs.current_root_id()),
                        body.get("pet_id"),
                        body.get("file_id"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/places/create":
                res = db.write_with_retry(
                    lambda: places.create_place(
                        self._db(body.get("root")),
                        body.get("root"),
                        body.get("name"),
                        body.get("lat"),
                        body.get("lon"),
                        body.get("file_id"),
                    )
                )
                self._json(res, 400 if "error" in res else 200)
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            logger.exception("unhandled error serving %s %s", self.command, self.path)
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
        info = browse.media_source(db_path, fid) if db_path else None
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, rotate = info
        tp = thumbnails.thumb_for(cache_dir, fid, src, sha256=sha256, rotate=rotate)
        self._send_file(tp if tp else src)

    def _serve_archive_thumb(self, root_id: int, fid: int):
        # Root-scoped thumbnail for the start-page cover mosaic: the picker shows
        # several archives at once with none "open", so the id alone (as /thumb/
        # uses) is not enough, the archive is named explicitly here.
        if self.cfg.archive_path(root_id) is None:
            return self._json({"error": "not found"}, 404)
        db_path, cache_dir = self._db(root_id), self._cache(root_id)
        info = browse.media_source(db_path, fid)
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, rotate = info
        tp = thumbnails.thumb_for(cache_dir, fid, src, sha256=sha256, rotate=rotate)
        self._send_file(tp if tp else src)

    def _serve_face_thumb(self, face_id: int):
        db_path, cache_dir = self._open_db_and_cache()
        info = people.face_crop_source(db_path, face_id) if db_path else None
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, box, rotate, frame_offset, _media_type, file_id = info
        if frame_offset is not None:
            # A video detection's box was measured in the extracted keyframe,
            # already upright (ffmpeg applies container rotation), so the
            # crop is cut from that same re-derived frame, not the video file
            # itself, and never rotated again.
            frame = thumbnails.detect_frame_for(
                cache_dir, file_id, src, frame_offset, self.cfg.detect_video_frame_px, sha256=sha256
            )
            if frame is None:
                return self._json({"error": "frame unavailable"}, 404)
            tp = thumbnails.face_thumb_for(
                cache_dir, face_id, frame, box, sha256=sha256, rotate=0, variant=frame_offset
            )
            return self._send_file(tp if tp else frame)
        tp = thumbnails.face_thumb_for(cache_dir, face_id, src, box, sha256=sha256, rotate=rotate)
        self._send_file(tp if tp else src)

    def _serve_animal_thumb(self, detection_id: int):
        db_path, cache_dir = self._open_db_and_cache()
        info = pets.animal_crop_source(db_path, detection_id) if db_path else None
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, box, rotate, frame_offset, _media_type, file_id = info
        if frame_offset is not None:
            frame = thumbnails.detect_frame_for(
                cache_dir, file_id, src, frame_offset, self.cfg.detect_video_frame_px, sha256=sha256
            )
            if frame is None:
                return self._json({"error": "frame unavailable"}, 404)
            tp = thumbnails.face_thumb_for(
                cache_dir, detection_id, frame, box, sha256=sha256, rotate=0, variant=frame_offset
            )
            return self._send_file(tp if tp else frame)
        tp = thumbnails.face_thumb_for(
            cache_dir, detection_id, src, box, sha256=sha256, rotate=rotate
        )
        self._send_file(tp if tp else src)

    def _serve_original(self, fid: int):
        db_path, cache_dir = self._open_db_and_cache()
        info = browse.media_source(db_path, fid) if db_path else None
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, rotate = info
        # A photo stored sideways is served from an upright re-encode; every
        # other file is served as its own untouched bytes.
        up = thumbnails.upright_for(cache_dir, fid, src, rotate, sha256=sha256)
        self._send_file(up if up else src)


def serve(cfg: Config, host="127.0.0.1", port=8756):
    # Only shared, non-archive resources (ML models, the app icon) live at the
    # top level now; each archive's own database is created when it's added
    # (archives.add_archive) or opened for the first time by the scheduler.
    cfg.ensure_dirs()
    cfg.migrate_legacy_archive()
    discard_superseded_secrets()
    from ..services import semantic

    # Loading the 283 MB text tower takes a second or two. Do it off the serving
    # thread now so the user's first search doesn't wait for it; a search that
    # arrives sooner simply blocks on the same lock and gets the warmed session.
    threading.Thread(
        target=semantic.warm_text_model, args=(cfg,), name="semantic-warm", daemon=True
    ).start()
    jm = JobManager(cfg)
    handler = type("BoundHandler", (Handler,), {"cfg": cfg, "jobs": jm})
    return ThreadingHTTPServer((host, port), handler)
