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
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from ..config import Config, discard_superseded_secrets
from ..errors import TroveError
from ..pipeline.manager import JobManager
from . import routes

logger = logging.getLogger(__name__)

_CHUNK = 256 * 1024

# Anchored, and one range only. Unanchored, ``bytes=0-1,5-6`` matched as
# ``0-1``: the server answered 206 for a multi-range request while sending a
# single range, which is a different response from the one that was asked for.
# A list is legal to refuse, so it is refused rather than half-answered.
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)\Z")


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Resolve one ``bytes=`` range against a known size, as an inclusive pair.

    ``None`` means "not a single byte range this server will honour" -- another
    unit, a list of ranges, a backwards pair, or unparseable. RFC 9110 lets a
    server ignore a Range it does not understand and send the whole body, which
    is what the caller does with it.

    A *suffix* range (``bytes=-500``, meaning the LAST 500 bytes) resolves here
    too. It used to be read as ``0-500``: the reply was a 206 claiming
    ``Content-Range: bytes 0-500`` and carrying the head of the file instead of
    its tail. An MP4 with a trailing ``moov`` atom is exactly the file a player
    probes that way, so the wrong bytes arrived silently.

    The pair may still be *unsatisfiable* -- a start at or past the end of the
    file, which includes every range against an empty one. That is a 416 for
    the caller to send rather than a parse failure, so it is deliberately not
    folded into ``None``: ``start >= size`` is the one test that catches it,
    for suffix and explicit ranges alike.
    """
    m = _RANGE_RE.match(header.strip())
    if m is None:
        return None
    first, last = m.group(1), m.group(2)
    if not first and not last:
        return None  # "bytes=-" names nothing
    if not first:
        # The last N bytes. N=0 puts start on size, i.e. unsatisfiable, which
        # is what RFC 9110 asks for and what the caller's one test reports.
        return max(0, size - int(last)), size - 1
    if not last:
        return int(first), size - 1
    if int(last) < int(first):
        return None  # backwards: the whole header is invalid, so ignore it
    return int(first), min(int(last), size - 1)


class Handler(BaseHTTPRequestHandler):
    # Declared, not assigned: serve() binds both by subclassing this with them
    # in the namespace, and this class is never instantiated directly. A `= None`
    # placeholder would type every use below as optional and buy nothing -- an
    # unbound Handler is a programming error either way, and this way it says so
    # at the attribute rather than several frames later inside a route.
    cfg: ClassVar[Config]
    jobs: ClassVar[JobManager]

    # `format` shadows the builtin because BaseHTTPRequestHandler names it that,
    # and an override that renames a positional parameter is a type error.
    def log_message(self, format: str, *args: Any) -> None:
        pass

    # -- response helpers -------------------------------------------------
    def _json(self, obj: Any, status: int = 200) -> None:
        # Archive state changes through POST requests, so serving a heuristic
        # browser cache entry here makes a completed add/remove appear to have
        # done nothing until another navigation happens.
        self._bytes(json.dumps(obj).encode(), "application/json", status, cache_control="no-store")

    def _bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        cache_control: str | None = None,
    ) -> None:
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

    def _range_not_satisfiable(self, size: int) -> None:
        """416 for a range starting at or past the end of the file.

        The ``Content-Range`` is the whole point of the reply: it tells the
        client how long the file actually is, so it can ask again for a range
        that exists. Answering 206 here instead sent a negative
        ``Content-Length`` and no body -- a malformed response a strict client
        is entitled to fail on.
        """
        self.send_response(416)
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_file(
        self, path: Path, content_type: str | None = None, cache_control: str | None = None
    ) -> None:
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        header = self.headers.get("Range")
        rng = _parse_range(header, size) if header else None
        start, end, status = 0, size - 1, 200
        if rng is not None:
            start, end = rng
            if start >= size:
                return self._range_not_satisfiable(size)
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
            parsed = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}
        # A body is untrusted input and JSON's top level may be any type; a
        # list here used to reach the routes as one and fail with a 500 on the
        # first .get(). Every caller wants an object, so anything else is no
        # more usable than an empty one.
        return parsed if isinstance(parsed, dict) else {}

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

    def _respond(self, result: object) -> None:
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

    def do_GET(self) -> None:
        self._answer("GET")

    def do_POST(self) -> None:
        self._answer("POST")


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8756) -> ThreadingHTTPServer:
    # Only shared, non-archive resources (ML models, the app icon) live at the
    # top level now; each archive's own database is created when it's added
    # (archives.add_archive) or opened for the first time by the scheduler.
    cfg.ensure_dirs()
    cfg.migrate_legacy_archive()
    discard_superseded_secrets()
    from ..services import meaning, semantic

    # Loading the 283 MB text tower takes a second or two. Do it off the serving
    # thread now so the user's first search doesn't wait for it; a search that
    # arrives sooner simply blocks on the same lock and gets the warmed session.
    threading.Thread(
        target=semantic.warm_text_model, args=(cfg,), name="semantic-warm", daemon=True
    ).start()
    # And the text encoder, for the same reason: a search that arrives sooner
    # blocks on the same lock and gets the warmed session rather than paying
    # for the load inside the request.
    threading.Thread(
        target=meaning.warm_model, args=(cfg,), name="meaning-warm", daemon=True
    ).start()
    jm = JobManager(cfg)
    handler = type("BoundHandler", (Handler,), {"cfg": cfg, "jobs": jm})
    return ThreadingHTTPServer((host, port), handler)
