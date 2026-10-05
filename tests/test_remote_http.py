"""Acceptance gate for engines/_remote_http.py, the HTTP the remote-service
packs' clients share: the token only ever goes to the configured address,
redirects are never followed, replies are size-capped, path segments are
quoted, plain http:// off this machine needs an opt-in, and a bad token
setting is one readable sentence.

Two local http.server instances on two ports stand in for "the service" and
"some other host" (a different port is a different origin). Nothing leaves
this machine.

Run: python3 tests/test_remote_http.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
ENGINES = os.path.join(ROOT, "engines")

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


class Fake:
    """One local HTTP server: routes[(method, path)] -> (status, headers, body); logs every hit."""

    def __init__(self):
        self.routes = {}
        self.hits = []          # (method, raw path, Authorization header or None)
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                fake.hits.append((method, self.path, self.headers.get("Authorization")))
                status, headers, body = fake.routes.get((method, self.path), (404, {}, b'{"error": "no route"}'))
                if callable(body):
                    body = body()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if "Content-Length" not in headers:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def json(self, method, path, obj, status=200):
        self.routes[(method, path)] = (status, {"Content-Type": "application/json"},
                                       json.dumps(obj).encode())

    def reset(self):
        self.routes.clear()
        del self.hits[:]


A, B = Fake(), Fake()      # A = the configured service, B = another host
TMP = tempfile.mkdtemp(prefix="remote-http-")
TOKEN_FILE = os.path.join(TMP, "token")
with open(TOKEN_FILE, "w") as fh:
    fh.write("s3cret\n")
TOKEN = "Bearer s3cret"


def run_client(script, args):
    p = subprocess.run([sys.executable, os.path.join(ENGINES, script)] + args,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
    return p.returncode, p.stdout


def film_args(out):
    # Only flags the client has always taken, so the same call runs against
    # the client as first contributed (the RED check).
    return ["--base", A.base, "--token-file", TOKEN_FILE, "--model", "m", "--prompt", "p",
            "--seed", "1", "--duration", "10", "--output", out]


# ---------------------------------------------------------------------------
print("Film client: a finished job whose output_url is on ANOTHER host")
A.reset(); B.reset()
A.json("GET", "/health", {"ok": True})
A.json("POST", "/v1/video/generations", {"id": "j1"})
A.json("GET", "/jobs/j1", {"status": "completed", "output_url": B.base + "/steal.mp4"})
B.routes[("GET", "/steal.mp4")] = (200, {}, b"video")
out = os.path.join(TMP, "film.mp4")
rc, text = run_client("film_remote_client.py", film_args(out))
check("the other host never sees the token", not any(h[2] for h in B.hits), repr(B.hits))
check("the other host is not contacted at all", B.hits == [], repr(B.hits))
check("the run fails", rc != 0, text)
check("no output file", not os.path.exists(out))
check("one ERROR: sentence names the refusal",
      any(l.startswith("ERROR: ") and "another address" in l for l in text.splitlines()), text)

print("Film client: a poll answered with a 302 to another host")
A.reset(); B.reset()
A.json("GET", "/health", {"ok": True})
A.json("POST", "/v1/video/generations", {"id": "j2"})
A.routes[("GET", "/jobs/j2")] = (302, {"Location": B.base + "/jobs/j2"}, b"")
B.json("GET", "/jobs/j2", {"status": "completed", "output_url": "/x.mp4"})
rc, text = run_client("film_remote_client.py", film_args(os.path.join(TMP, "film2.mp4")))
check("redirect not followed: the other host got nothing", B.hits == [], repr(B.hits))
check("redirect: the run fails with an ERROR: line",
      rc != 0 and any(l.startswith("ERROR: ") and "redirect" in l for l in text.splitlines()), text)

print("Qwen client: a job id with / ? # is sent as ONE quoted path segment")
A.reset(); B.reset()
A.json("POST", "/v1/images/generations", {"id": "a/b?c#d"})
A.json("GET", "/jobs/a%2Fb%3Fc%23d", {"status": "completed", "output_url": "/files/x.png"})
A.routes[("GET", "/files/x.png")] = (200, {}, PNG)
out = os.path.join(TMP, "q.png")
rc, text = run_client("qwen_remote_client.py",
                      ["--base", A.base, "--token-file", TOKEN_FILE, "--prompt", "p", "--output", out])
polled = [h[1] for h in A.hits if h[1].startswith("/jobs/")]
check("polled /jobs/a%2Fb%3Fc%23d", polled == ["/jobs/a%2Fb%3Fc%23d"], repr(polled))
check("the PNG is saved", rc == 0 and os.path.isfile(out), text)
check("the token went to the configured host", all(h[2] == TOKEN for h in A.hits), repr(A.hits))

print("YuE client: a result URL on another host in the reply text")
A.reset(); B.reset()
A.json("POST", "/v1/chat/completions",
       {"choices": [{"message": {"content": "Done: %s/song.mp3." % B.base}}]})
B.routes[("GET", "/song.mp3")] = (200, {}, b"mp3")
rc, text = run_client("music_remote_client.py",
                      ["--base", A.base, "--token-file", TOKEN_FILE, "--model", "m", "--style", "s",
                       "--lyrics", "l", "--seed", "1", "--output", os.path.join(TMP, "s.mp3")])
check("the other host is not contacted", B.hits == [], repr(B.hits))
check("the run fails with an ERROR: line",
      rc != 0 and any(l.startswith("ERROR: ") for l in text.splitlines()), text)

print("Film client: a token file path with a typo")
A.reset()
rc, text = run_client("film_remote_client.py",
                      ["--base", A.base, "--token-file", os.path.join(TMP, "tokn"), "--model", "m",
                       "--prompt", "p", "--seed", "1", "--duration", "10",
                       "--output", os.path.join(TMP, "f3.mp4")])
lines = [l for l in text.splitlines() if l.strip()]
check("one line, a sentence naming the file", rc != 0 and len(lines) == 1
      and lines[0].startswith("ERROR: The token file") and "tokn" in lines[0], text)
check("nothing was sent", A.hits == [], repr(A.hits))

# ---------------------------------------------------------------------------
print()
print("the helper itself")
try:
    from engines import _remote_http as rh
except ImportError as e:
    rh = None
    check("engines/_remote_http.py exists", False, str(e))

if rh:
    svc = rh.Service(A.base, "s3cret", "The test service")

    def refuses(fn, needle):
        try:
            fn()
        except rh.RemoteError as e:
            return needle in str(e), str(e)
        return False, "no error"

    A.reset(); B.reset()
    B.routes[("GET", "/f")] = (200, {}, b"data")
    ok, why = refuses(lambda: svc.download(B.base + "/f", os.path.join(TMP, "x")), "another address")
    check("download from another host refused", ok, why)
    ok, why = refuses(lambda: svc.json(B.base + "/f"), "another address")
    check("JSON from another host refused", ok, why)
    ok, why = refuses(lambda: svc.resolve("//127.0.0.1:%d/f" % B.port), "another address")
    check("a scheme-relative URL to another host refused", ok, why)
    check("...and the other host saw nothing", B.hits == [], repr(B.hits))
    check("a relative URL joins the base", svc.resolve("/out/x.mp4") == A.base + "/out/x.mp4")
    check("an absolute URL on the base is kept",
          svc.resolve(A.base + "/out/x.mp4") == A.base + "/out/x.mp4")

    A.routes[("GET", "/r")] = (302, {"Location": "/f2"}, b"")
    A.json("GET", "/f2", {"ok": True})
    ok, why = refuses(lambda: svc.json(svc.url("r")), "redirect")
    check("a same-host 302 is not followed either", ok, why)
    check("...the target was never asked", not any(h[1] == "/f2" for h in A.hits), repr(A.hits))

    A.reset()
    A.routes[("GET", "/big")] = (200, {}, b"x" * 5000)
    target = os.path.join(TMP, "big.bin")
    ok, why = refuses(lambda: svc.download("/big", target, limit=1000), "larger than")
    check("oversize download (Content-Length) refused", ok, why)
    check("...no file and no .part left", not os.path.exists(target) and not os.path.exists(target + ".part"))
    check("download cap is 4 GiB", rh.MAX_DOWNLOAD == 4 * 1024 ** 3)
    saved = rh.MAX_JSON
    rh.MAX_JSON = 100
    try:
        A.json("GET", "/fat", {"pad": "y" * 500})
        ok, why = refuses(lambda: svc.json(svc.url("fat")), "larger than")
        check("oversize JSON refused", ok, why)
    finally:
        rh.MAX_JSON = saved
    check("JSON cap is 8 MiB", rh.MAX_JSON == 8 * 1024 ** 2)

    check("job id quoted as one segment", svc.url("jobs", "../a?b#c") == A.base + "/jobs/..%2Fa%3Fb%23c",
          svc.url("jobs", "../a?b#c"))

    A.json("GET", "/e1", {"error": "model not loaded", "trace": "z" * 3000}, status=400)
    ok, why = refuses(lambda: svc.json(svc.url("e1")), "model not loaded")
    check("an error body is cut to its error field", ok and "zzz" not in why, why)
    A.routes[("GET", "/e2")] = (400, {}, b"q" * 5000)
    try:
        svc.json(svc.url("e2"))
        why = ""
    except rh.RemoteError as e:
        why = str(e)
    check("a plain error body is cut to 300 characters", 0 < why.count("q") <= 300, "%d chars" % len(why))

    saved = os.environ.pop(rh.ALLOW_HTTP_ENV, None)
    try:
        ok, why = refuses(lambda: rh.Service("http://192.0.2.10:8094", "t", "S", "FILM5080_BASE"),
                          rh.ALLOW_HTTP_ENV)
        check("plain http:// to another machine refused without the opt-in", ok, why)
        check("...naming the setting", "FILM5080_BASE" in why, why)
        rh.Service("https://192.0.2.10:8094", "t", "S")
        rh.Service("http://localhost:8094", "t", "S")
        rh.Service("http://[::1]:8094", "t", "S")
        check("https and loopback http need no opt-in", True)
        os.environ[rh.ALLOW_HTTP_ENV] = "1"
        rh.Service("http://192.0.2.10:8094", "t", "S")
        check("BWF_REMOTE_ALLOW_HTTP=1 allows plain http", True)
        ok, why = refuses(lambda: rh.Service("ftp://x/", "t", "S"), "not an http")
        check("a non-http address refused", ok, why)
    finally:
        os.environ.pop(rh.ALLOW_HTTP_ENV, None)
        if saved is not None:
            os.environ[rh.ALLOW_HTTP_ENV] = saved

    ok, why = refuses(lambda: rh.read_token(os.path.join(TMP, "nope"), "FILM5080_TOKEN_FILE"),
                      "FILM5080_TOKEN_FILE")
    check("missing token file: one sentence naming the setting", ok and why.count(".") >= 1
          and "\n" not in why and "nope" in why, why)
    empty = os.path.join(TMP, "empty")
    open(empty, "w").close()
    ok, why = refuses(lambda: rh.read_token(empty, "X_TOKEN_FILE"), "empty")
    check("empty token file refused", ok, why)
    check("'none' means no token where allowed", rh.read_token("none", "X", allow_none=True) is None)
    ok, why = refuses(lambda: rh.read_token("none", "X_TOKEN_FILE"), "cannot be read")
    check("'none' is just a missing file where a token is required", ok, why)
    nosvc = rh.Service(A.base, None, "S")
    A.reset(); A.json("GET", "/t", {})
    nosvc.json(nosvc.url("t"))
    check("no token: no Authorization header", A.hits and A.hits[0][2] is None, repr(A.hits))

    import time as _t
    ok, why = refuses(lambda: svc.poll(svc.url("t"), lambda s: None, _t.monotonic() - 1), "in time")
    check("poll ends at its deadline", ok, why)
    dead = rh.Service("http://127.0.0.1:9", "t", "S")
    t0 = _t.monotonic()
    try:
        dead.poll(dead.url("jobs", "x"), lambda s: s, _t.monotonic() + 60, interval=0, max_failures=2)
        why = "no error"
    except rh.RemoteError as e:
        why = str(e)
    check("poll stops after a bounded run of failures", "could not be reached" in why
          and _t.monotonic() - t0 < 30, why)

    print()
    print("the helper and the clients are not packs")
    import engines
    files = list(engines._PACK_FILES.values()) if engines.packs() is not None else []
    names = {os.path.basename(f) for f in files}
    check("no pack comes from _remote_http.py or a *_client.py",
          not any(n == "_remote_http.py" or n.endswith("_client.py") for n in names), repr(sorted(names)))

A.server.shutdown(); B.server.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
