"""Minimal DevTools Protocol client, plus the screenshot tool built on it.

Stdlib only -- no selenium or playwright -- which is what lets both the
screenshot tooling and the browser test tier drive a real Chrome without the
repository growing a browser-automation dependency or a build step (ADR 0002).

Three layers, lowest first:

* ``http_json`` / ``ws_connect`` / ``ws_send_text`` / ``WS`` -- the transport.
  Handles WS frame fragmentation and control frames per RFC 6455, and never
  blocks forever (a socket timeout raises rather than hanging silently).
* ``Tab`` / ``open_tab`` -- one open tab: ``call``, ``evaluate``, ``wait_for``,
  ``screenshot``, and a close that always runs. This is what
  ``tests/browser/`` asserts through and what ``shoot_all.py`` shoots with.
* ``main`` -- the one-shot screenshot CLI.

Usage: python3 tools/dev/cdp_shot.py <url> <outfile.png> [wait_seconds] [cdp_port]

Requires a headless Chrome already running with --remote-debugging-port, e.g.:
  chromium-browser --headless=new --disable-gpu --no-sandbox \\
    --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1 about:blank

See CONTRIBUTING.md, "How to look at a GUI change", for the traps -- in
particular why a plain ``--screenshot`` and ``--virtual-time-budget`` are both
the wrong tool here.
"""

import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from contextlib import contextmanager

CDP_HOST = "127.0.0.1"


def http_json(port, path, method="GET"):
    req = urllib.request.Request(f"http://{CDP_HOST}:{port}{path}", method=method)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def ws_connect(ws_url, timeout=15):
    host_port, path = ws_url.split("//", 1)[1].split("/", 1)
    host, port = host_port.split(":")
    s = socket.create_connection((host, int(port)), timeout=timeout)
    s.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET /{path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    s.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(4096)
    return s


def ws_send_text(s, text):
    payload = text.encode()
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else:
        header = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    s.sendall(header + mask + masked)


class WS:
    """Reads complete WS *messages*, transparently reassembling fragmented
    frames and answering control frames (ping), per RFC 6455."""

    def __init__(self, sock):
        self.s = sock
        self.buf = b""

    def _fill(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self.buf += chunk

    def _read_frame(self):
        self._fill(2)
        b0, b1 = self.buf[0], self.buf[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        plen = b1 & 0x7F
        hlen = 2
        if plen == 126:
            self._fill(4)
            plen = struct.unpack("!H", self.buf[2:4])[0]
            hlen = 4
        elif plen == 127:
            self._fill(10)
            plen = struct.unpack("!Q", self.buf[2:10])[0]
            hlen = 10
        if masked:
            hlen += 4
        self._fill(hlen + plen)
        payload = self.buf[hlen : hlen + plen]
        if masked:
            mkey = self.buf[hlen - 4 : hlen]
            payload = bytes(b ^ mkey[i % 4] for i, b in enumerate(payload))
        self.buf = self.buf[hlen + plen :]
        return fin, opcode, payload

    def recv_message(self):
        """Returns one complete text message as str, transparently handling
        fragmentation (opcode 0x0 continuations) and replying to pings."""
        parts = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:  # close
                raise ConnectionError("server closed connection")
            parts.append(payload)
            if fin:
                break
        return b"".join(parts).decode()

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        b0 = 0x80 | opcode
        if n < 126:
            header = struct.pack("!BB", b0, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", b0, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", b0, 0x80 | 127, n)
        self.s.sendall(header + mask + masked)


class CDPError(RuntimeError):
    """A CDP command failed, or the JS it ran threw."""


class Tab:
    """One open Chrome tab, driven over the DevTools Protocol.

    The socket, the message reader and the request-id counter are useless
    apart and were previously threaded through every call site as a
    ``(sock, ws, msgid_box)`` triple -- once in this file's ``main()`` and
    again, separately, in ``shoot_all.py``. Holding them together is what lets
    a caller write ``tab.evaluate(...)`` and lets a second caller (the browser
    test tier) exist at all without a third copy of the same plumbing.

    Not constructed directly: ``open_tab`` owns the lifecycle, because a tab
    that is not closed keeps polling the app in the background and starves the
    browser -- the trap CONTRIBUTING documents.
    """

    def __init__(self, port, tab_id, sock):
        self.port = port
        self.id = tab_id
        self.sock = sock
        self._ws = WS(sock)
        self._next_id = 0

    def call(self, method, params=None):
        """Send one CDP command and return its reply, skipping event messages.

        The protocol multiplexes events onto the same socket, so matching on
        the id we sent is what stops a ``Page.loadEventFired`` arriving
        mid-command from being read as the answer to it.
        """
        self._next_id += 1
        mid = self._next_id
        ws_send_text(self.sock, json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            obj = json.loads(self._ws.recv_message())
            if obj.get("id") != mid:
                continue
            if "error" in obj:
                raise CDPError(f"{method}: {obj['error']}")
            return obj

    def evaluate(self, expression):
        """Run JS in the page and return its value.

        Raises rather than returning None when the expression throws: a typo in
        a selector is a broken test, and silently reading it as "no elements
        matched" is how such a test passes without ever checking anything.
        """
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        result = res.get("result", {})
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise CDPError(f"JS threw: {text}\n  in: {expression}")
        return result.get("result", {}).get("value")

    def wait_for(self, expression, timeout=10.0, poll=0.05, what=None):
        """Poll a JS expression until it is truthy, then return its value.

        The alternative is a fixed sleep long enough for the slowest machine,
        which is the thing the screenshot tooling had to grow out of: too short
        is flaky, too long is paid on every run. On timeout the message names
        what was awaited rather than reporting whatever assertion came next.
        """
        deadline = time.monotonic() + timeout
        while True:
            value = self.evaluate(expression)
            if value:
                return value
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out after {timeout}s waiting for {what or expression}")
            time.sleep(poll)

    def screenshot(self):
        """The tab's current viewport as PNG bytes."""
        return base64.b64decode(
            self.call("Page.captureScreenshot", {"format": "png"})["result"]["data"]
        )

    def close(self):
        self.sock.close()
        try:
            http_json(self.port, f"/json/close/{self.id}")
        except Exception:
            # The tab may already be gone (browser shut down, crashed target).
            # Nothing left to clean up, and raising here would mask whatever
            # the caller was actually doing.
            pass


@contextmanager
def open_tab(url=None, port=9333):
    """Open a tab, yield it as a ``Tab``, and always close it.

    ``url=None`` opens ``about:blank``, which is what a caller wants when it
    must install something (a script, a request block) *before* the page's own
    startup code runs, and navigate afterwards.
    """
    query = f"?{url.replace('#', '%23')}" if url else ""
    info = http_json(port, f"/json/new{query}", method="PUT")
    tab = Tab(port, info["id"], ws_connect(info["webSocketDebuggerUrl"]))
    try:
        yield tab
    finally:
        tab.close()


def main():
    url = sys.argv[1]
    outfile = sys.argv[2]
    wait_s = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
    port = int(sys.argv[4]) if len(sys.argv) > 4 else 9333

    with open_tab(url, port) as tab:
        tab.call("Page.enable")
        tab.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1400, "height": 1200, "deviceScaleFactor": 1, "mobile": False},
        )
        time.sleep(wait_s)
        data = tab.screenshot()
        with open(outfile, "wb") as f:
            f.write(data)
        print("saved", outfile, len(data), "bytes")


if __name__ == "__main__":
    main()
