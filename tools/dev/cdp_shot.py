"""Minimal CDP screenshot tool (stdlib only — no selenium/playwright installed
in this environment). Loads a URL in an already-running headless Chrome via
the DevTools Protocol, waits a fixed real-wall-clock delay for JS/fetches to
settle, then captures a PNG. Used to visually verify GUI changes (see
CLAUDE.md "Verifying GUI changes").

Handles WS frame fragmentation and control frames per RFC 6455, and never
blocks forever (socket timeout raises instead of hanging silently).

Usage: python3 tools/dev/cdp_shot.py <url> <outfile.png> [wait_seconds] [cdp_port]

Requires a headless Chrome already running with --remote-debugging-port, e.g.:
  chromium-browser --headless=new --disable-gpu --no-sandbox \\
    --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1 about:blank
"""

import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

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


def main():
    url = sys.argv[1]
    outfile = sys.argv[2]
    wait_s = float(sys.argv[3]) if len(sys.argv) > 3 else 4.0
    port = int(sys.argv[4]) if len(sys.argv) > 4 else 9333

    safe_url = url.replace("#", "%23")
    tab = http_json(port, f"/json/new?{safe_url}", method="PUT")
    tab_id = tab["id"]
    ws_url = tab["webSocketDebuggerUrl"]
    sock = ws_connect(ws_url)
    ws = WS(sock)
    msgid = 1

    def call(method, params=None):
        nonlocal msgid
        mid = msgid
        msgid += 1
        ws_send_text(sock, json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            obj = json.loads(ws.recv_message())
            if obj.get("id") == mid:
                return obj

    try:
        call("Page.enable")
        call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1400, "height": 1200, "deviceScaleFactor": 1, "mobile": False},
        )
        time.sleep(wait_s)
        res = call("Page.captureScreenshot", {"format": "png"})
        data = res["result"]["data"]
        with open(outfile, "wb") as f:
            f.write(base64.b64decode(data))
        print("saved", outfile, len(data), "b64 chars")
    finally:
        # Close the tab so repeated runs don't pile up background polling tabs
        # that starve the browser and make later screenshots time out.
        sock.close()
        try:
            http_json(port, f"/json/close/{tab_id}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
