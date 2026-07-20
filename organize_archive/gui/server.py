"""Minimal local web server for browsing the catalog (stdlib only).

Binds to localhost. Files are served by database id — the path is looked up in
the DB, never taken from the request — so there is no path-traversal surface.
"""

from __future__ import annotations

import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from ..config import Config
from . import queries, thumbs

_INDEX = Path(__file__).with_name("index.html")
_CHUNK = 256 * 1024


class Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # set by the factory

    # Quieter logging.
    def log_message(self, fmt, *args):
        pass

    # -- helpers ----------------------------------------------------------
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str | None = None):
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                end = min(end, size - 1)
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
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
            pass  # client navigated away / stopped a video

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)

        def one(key, cast=str, default=None):
            v = q.get(key, [None])[0]
            return cast(v) if v not in (None, "") else default

        try:
            if path == "/" or path == "/index.html":
                self._send_file(_INDEX, "text/html; charset=utf-8")
            elif path == "/api/summary":
                self._json(queries.summary(self.cfg.db_path))
            elif path == "/api/media":
                self._json(queries.media(
                    self.cfg.db_path,
                    year=one("year"), month=one("month"), mtype=one("type"),
                    limit=min(one("limit", int, 120), 500),
                    offset=one("offset", int, 0),
                ))
            elif path.startswith("/api/item/"):
                it = queries.item(self.cfg.db_path, int(path.rsplit("/", 1)[1]))
                self._json(it) if it else self._json({"error": "not found"}, 404)
            elif path.startswith("/thumb/"):
                self._serve_thumb(int(path.rsplit("/", 1)[1]))
            elif path.startswith("/file/"):
                self._serve_original(int(path.rsplit("/", 1)[1]))
            else:
                self._json({"error": "not found"}, 404)
        except (ValueError, BrokenPipeError):
            pass
        except Exception as e:  # keep the server alive on any single-request error
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def _serve_thumb(self, fid: int):
        src = queries.file_location(self.cfg.db_path, fid)
        if src is None:
            return self._json({"error": "not found"}, 404)
        tp = thumbs.thumb_for(self.cfg.cache_dir, fid, src)
        self._send_file(tp if tp else src)

    def _serve_original(self, fid: int):
        src = queries.file_location(self.cfg.db_path, fid)
        if src is None:
            return self._json({"error": "not found"}, 404)
        self._send_file(src)


def serve(cfg: Config, host="127.0.0.1", port=8756):
    handler = type("BoundHandler", (Handler,), {"cfg": cfg})
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
