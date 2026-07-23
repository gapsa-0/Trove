"""Local web server for the archive management app (stdlib only).

Binds to localhost. Files are served by database id (path looked up in the DB,
never taken from the request), so there is no path-traversal surface.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from ..config import Config
from ..db import database as db
from . import queries, thumbs, icons
from .jobs import JobManager

_INDEX = Path(__file__).with_name("index.html")
_CHUNK = 256 * 1024

_MANIFEST = {
    "name": "organize_archive", "short_name": "archive",
    "start_url": "/", "scope": "/", "display": "standalone",
    "background_color": "#14161a", "theme_color": "#14161a",
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
                self._json({"archives": queries.archives(self.cfg.db_path)})
            elif path == "/api/freshness":
                self._json(queries.freshness(self.cfg.db_path, one("root", int)))
            elif path == "/api/summary":
                self._json(queries.summary(self.cfg.db_path, one("root", int)))
            elif path == "/api/timeline":
                self._json(queries.timeline(
                    self.cfg.db_path, root_id=one("root", int),
                    bucket=one("bucket", str, "month"), year=one("year"),
                    month=one("month"), person_ids=many("person"),
                    cluster_id=one("place", int)))
            elif path == "/api/dates/sources":
                self._json(queries.date_sources(self.cfg.db_path, one("root", int)))
            elif path == "/api/map/clusters":
                self._json(queries.place_clusters(self.cfg.db_path, one("root", int)))
            elif path.startswith("/api/map/cluster/"):
                c = queries.place_cluster_members(self.cfg.db_path, int(path.rsplit("/", 1)[1]))
                self._json(c) if c else self._json({"error": "not found"}, 404)
            elif path == "/api/faces/summary":
                self._json(queries.face_summary(self.cfg.db_path, one("root", int)))
            elif path == "/api/faces/persons":
                self._json(queries.face_persons(
                    self.cfg.db_path, one("root", int),
                    limit=min(one("limit", int, 120), 500), offset=one("offset", int, 0)))
            elif path == "/api/faces/suggestions":
                self._json(queries.person_suggestions(
                    self.cfg.db_path, one("root", int), limit=min(one("limit", int, 40), 200)))
            elif path.startswith("/api/faces/person/"):
                p2 = queries.face_person(
                    self.cfg.db_path, int(path.rsplit("/", 1)[1]), one("root", int),
                    limit=min(one("limit", int, 120), 500), offset=one("offset", int, 0))
                self._json(p2) if p2 else self._json({"error": "not found"}, 404)
            elif path == "/api/dups/summary":
                self._json(queries.dup_summary(self.cfg.db_path, one("root", int)))
            elif path == "/api/dups":
                self._json(queries.dup_groups(
                    self.cfg.db_path, one("root", int),
                    limit=min(one("limit", int, 60), 200), offset=one("offset", int, 0)))
            elif path == "/api/media":
                self._json(queries.media(
                    self.cfg.db_path, root_id=one("root", int),
                    year=one("year"), month=one("month"), mtype=one("type"),
                    person_ids=many("person"), cluster_id=one("place", int),
                    limit=min(one("limit", int, 120), 500), offset=one("offset", int, 0)))
            elif path == "/api/browse/filters":
                self._json(queries.browse_filters(self.cfg.db_path, one("root", int)))
            elif path == "/api/folders":
                self._json(queries.folders(self.cfg.db_path, one("root", int),
                                           limit=min(one("limit", int, 120), 500)))
            elif path == "/api/browse/semantic/status":
                from . import semantic
                status = queries.semantic_summary(self.cfg.db_path, one("root", int))
                status["configured"] = semantic.api_key_available()
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
                    vectors = semantic.embed_queries(
                        self.cfg, search_queries, self.cfg.db_path)
                    self._json(queries.semantic_search(
                        self.cfg.db_path, vectors[0], root_id=one("root", int),
                        year=one("year"), month=one("month"), mtype=one("type"),
                        person_ids=many("person"), cluster_id=one("place", int),
                        min_similarity=max(-1.0, min(1.0, float(
                            self.cfg.semantic_search_min_similarity))),
                        limit=min(one("limit", int, 120), 500), offset=one("offset", int, 0),
                        alternate_vectors=[(vector, 0.01) for vector in vectors[1:]]))
            elif path.startswith("/api/item/"):
                it = queries.item(self.cfg.db_path, int(path.rsplit("/", 1)[1]))
                self._json(it) if it else self._json({"error": "not found"}, 404)
            elif path == "/api/jobs":
                self._json({"jobs": self.jobs.list(one("root", int))})
            elif path.startswith("/api/job/"):
                j = self.jobs.get(int(path.rsplit("/", 1)[1]))
                self._json(j) if j else self._json({"error": "not found"}, 404)
            elif path.startswith("/thumb/"):
                self._serve_thumb(int(path.rsplit("/", 1)[1]))
            elif path.startswith("/faceThumb/"):
                self._serve_face_thumb(int(path.rsplit("/", 1)[1]))
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
                res = queries.add_archive(self.cfg.db_path, body.get("path", ""))
                if "error" in res:
                    self._json(res, 400)
                else:
                    if res["path"] not in self.cfg.roots:
                        self.cfg.roots.append(res["path"])
                        self.cfg.save()
                    self._json(res)
            elif path == "/api/archive/open":
                root_id = body.get("root_id")
                if not isinstance(root_id, int):
                    self._json({"error": "root_id is required"}, 400)
                elif not any(a["id"] == root_id and a["exists"]
                             for a in queries.archives(self.cfg.db_path)):
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
                    res = queries.remove_archive(self.cfg.db_path, self.cfg.cache_dir, root_id)
                    if "error" not in res:
                        # `roots` is the user-facing registration list as well
                        # as scan's default input; removing the DB row alone
                        # would otherwise register it again at the next scan.
                        removed = Path(res["path"])
                        self.cfg.roots = [p for p in self.cfg.roots
                                          if Path(p).expanduser().resolve() != removed]
                        self.cfg.save()
                    self._json(res, 400 if "error" in res else 200)
            elif path == "/api/map/cluster/rename":
                res = queries.rename_place_cluster(
                    self.cfg.db_path, body.get("cluster_id"), (body.get("name") or "").strip())
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/person/rename":
                res = queries.rename_person(
                    self.cfg.db_path, body.get("person_id"), (body.get("name") or "").strip())
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/reassign":
                res = queries.reassign_face(
                    self.cfg.db_path, body.get("face_id"), body.get("person_id"))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/merge":
                res = queries.merge_persons(self.cfg.db_path, body.get("a"), body.get("b"))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/different":
                res = queries.set_persons_different(self.cfg.db_path, body.get("a"), body.get("b"))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/skip":
                res = queries.set_persons_skip(self.cfg.db_path, body.get("a"), body.get("b"))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/faces/hide":
                res = queries.hide_person(self.cfg.db_path, body.get("person_id"))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/date":
                res = queries.set_date(
                    self.cfg.db_path, body.get("file_id"), body.get("datetime"))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/item/place":
                if body.get("clear"):
                    res = queries.clear_place(self.cfg.db_path, body.get("file_id"))
                else:
                    res = queries.set_place(
                        self.cfg.db_path, body.get("file_id"), body.get("place_id"))
                self._json(res, 400 if "error" in res else 200)
            elif path == "/api/places/create":
                res = queries.create_place(
                    self.cfg.db_path, body.get("root"), body.get("name"),
                    body.get("lat"), body.get("lon"), body.get("file_id"))
                self._json(res, 400 if "error" in res else 200)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # -- media serving ----------------------------------------------------
    def _serve_thumb(self, fid: int):
        info = queries.thumb_source(self.cfg.db_path, fid)
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256 = info
        tp = thumbs.thumb_for(self.cfg.cache_dir, fid, src, sha256=sha256)
        self._send_file(tp if tp else src)

    def _serve_face_thumb(self, face_id: int):
        info = queries.face_crop_source(self.cfg.db_path, face_id)
        if info is None:
            return self._json({"error": "not found"}, 404)
        src, sha256, box = info
        tp = thumbs.face_thumb_for(self.cfg.cache_dir, face_id, src, box, sha256=sha256)
        self._send_file(tp if tp else src)

    def _serve_original(self, fid: int):
        src = queries.file_location(self.cfg.db_path, fid)
        if src is None:
            return self._json({"error": "not found"}, 404)
        self._send_file(src)


def serve(cfg: Config, host="127.0.0.1", port=8756):
    # Apply schema migrations before JobManager starts its automatic scheduler.
    # The scheduler's first tick can query newly added tables/columns.
    cfg.ensure_dirs()
    conn = db.connect(cfg.db_path)
    try:
        db.init_db(conn)
    finally:
        conn.close()
    jm = JobManager(cfg)
    handler = type("BoundHandler", (Handler,), {"cfg": cfg, "jobs": jm})
    return ThreadingHTTPServer((host, port), handler)
