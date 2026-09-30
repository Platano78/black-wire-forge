"""Shared by the W3 download suites (tests/test_setup_downloads.py, tests/test_setup_downloads_ui.py): a
local stand-in for huggingface.co and its file CDN. Never a real download: server.py is pointed here
through its test-only, 127.0.0.1-pinned hooks BWF_TEST_HF_DOWNLOAD / BWF_TEST_HF_CDN.

Every file's bytes are generated from its path (so a resumed download can be compared byte for byte),
and every request is recorded with its headers. A path can be given a behaviour in `routes`:
  "ok" (default)  the file, honouring "Range: bytes=N-" with a 206
  "slow"          the same, trickled (so a test can stop the server mid-file)
  "more"          Content-Length says the size, but a chunked body sends 100 bytes more
  "badlen"        Content-Length and body are 5 bytes longer than the size
  "nolen"         no Content-Length at all
  "403"           a gated repo
  "redirect"      302 to the same path on `redirect_to` (an origin like http://127.0.0.1:N)
  "norange"       ignores Range: always 200 with the whole file
  "416"           any Range request gets 416; without Range, the whole file
  "hold"          like "ok", but first sets `arrived` and waits for `release` (a test acts in between)
"""
import hashlib
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def content(path, size):
    """Deterministic bytes for a path: a 251-byte seed repeated (251 is prime, so an offset
    error shows up as different bytes)."""
    seed = (hashlib.sha256(path.encode()).digest() * 8)[:251]
    return (seed * (size // 251 + 1))[:size]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        path = self.path.split("?")[0]
        srv.log.append({"path": path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        mode = srv.routes.get(path, "ok")
        if mode == "hold":
            srv.arrived.set()
            srv.release.wait(20)
        size = srv.sizes.get(path, srv.default_size)
        if mode == "403":
            body = b"Access to this repo is restricted."
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", srv.redirect_to + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = content(path, size)
        if mode == "416" and self.headers.get("Range"):
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "more":
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(data) + 100, 1 << 16):
                chunk = (data + b"X" * 100)[i:i + (1 << 16)]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            self.wfile.write(b"0\r\n\r\n")
            return
        if mode == "badlen":
            data = data + b"EXTRA"
        start = 0
        m = re.match(r"bytes=(\d+)-$", self.headers.get("Range") or "")
        if m and mode in ("ok", "slow", "hold") and int(m.group(1)) < size:
            start = int(m.group(1))
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, size - 1, size))
        else:
            self.send_response(200)
        if mode == "nolen":
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(len(data) - start))
        self.end_headers()
        step = 1 << 18
        try:
            for i in range(start, len(data), step):
                self.wfile.write(data[i:i + step])
                if mode == "slow":
                    time.sleep(srv.slow_sleep)
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(default_size=4096):
    """-> a running fake (its .origin is http://127.0.0.1:<port>)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.routes, srv.sizes, srv.log = {}, {}, []
    srv.default_size, srv.redirect_to, srv.slow_sleep = default_size, "", 0.03
    srv.arrived, srv.release = threading.Event(), threading.Event()
    srv.origin = "http://127.0.0.1:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def file_path(repo, file):
    return "/%s/resolve/main/%s" % (repo, file)
