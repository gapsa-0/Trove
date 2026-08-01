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

from ..config import Config, discard_superseded_secrets
from ..errors import TroveError
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

    def _is_cross_origin(self) -> bool:
        """Whether this request was made by a page on some *other* site.

        This server listens on the loopback interface, which is not the barrier
        it looks like: a tab open on any website can post to 127.0.0.1, and the
        browser will send it. A JSON body normally forces a CORS preflight that
        we never answer, but ``fetch`` with a ``text/plain`` content type is a
        "simple request" -- no preflight, straight through -- and
        ``_read_json_body`` parses the body whatever the content type says. So
        without this check, any website could quietly drive /api/archive/remove.

        The test is ``Origin`` against ``Host``, which is enough because
        browsers set ``Origin`` on every cross-origin request and cannot be
        talked out of it. A missing ``Origin`` means the caller is not a browser
        at all -- curl, the test suite, a script -- and those are not the
        confused deputy this defends against. Comparing against ``Host`` rather
        than a fixed address is what keeps it working whether the user reached
        the app as 127.0.0.1 or localhost, on whatever port it got.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return False
        return urlparse(origin).netloc != self.headers.get("Host")

    # -- routing ------------------------------------------------------------
    # Every route this server answers is registered in ``routes/``; the two
    # methods below only translate. ``_build_request`` turns a socket into a
    # ``Request``, ``_answer`` turns whatever the handler returns -- or raises
    # -- back into a response.
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

    def _answer(self, method: str) -> None:
        """Run this request's handler and send exactly one response, whatever
        happens.

        Five outcomes, and which one a failure gets is the point of the split:

        * **Another site driving a mutation** -- 403, before the body is even
          looked at. GET is deliberately not checked: it changes nothing, and
          a cross-origin caller cannot read the reply anyway without the CORS
          headers this server never sends.
        * **No route** -- 404.
        * **The caller's fault** -- ``TroveError`` (raised deliberately, message
          written to be read by a person) or ``ValueError`` (a bad id in the
          path, a missing ``?root=``) becomes a 400 carrying that message.
        * **A dead client** -- nothing. The tab closed mid-request; there is
          nobody left to answer, and a traceback would only be noise.
        * **Ours** -- a logged traceback and a 500.

        GET used to swallow ``ValueError`` silently, so a root-scoped route
        called without ``?root=`` dropped the connection with no response while
        POST answered 400 for the same mistake. Handling both methods here is
        what makes that impossible to reintroduce.
        """
        try:
            if method == "POST" and self._is_cross_origin():
                return self._json({"error": "cross-origin request refused"}, 403)
            body = self._read_json_body() if method == "POST" else {}
            req = self._build_request(method, body)
            handler = routes.handler_for(method, req.path)
            if handler is None:
                return self._json({"error": "not found"}, 404)
            self._respond(handler(req))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (TroveError, ValueError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            logger.exception("unhandled error serving %s %s", self.command, self.path)
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                # The client is already gone (broken pipe, closed tab): there is
                # nowhere left to report this failure to, so stay silent.
                pass

    def do_GET(self):
        self._answer("GET")

    def do_POST(self):
        self._answer("POST")


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
