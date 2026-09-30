"""W1 first-run Setup, over HTTP against a real server.py subprocess (no browser):

(a) no config file -> Setup mode: /api/health says setup: true, on 127.0.0.1 only;
(b) every other API is 503 "Finish Setup first."; /api/setup/state answers;
(c) the ComfyUI probe finds tests/fixtures/fake_comfy.py and returns only the fields shown;
(d) the guide probe finds the FakeHelper's /models and "Test it" returns its reply;
(e) write -> a valid config.json, the server re-execs (same pid), setup false, the lane listed;
(f) a config that appears meanwhile -> 409 and is left untouched; /api/setup/* 404 once configured;
(g) an INVALID config file still exits with code 2 (never Setup mode, never rewritten).

Run: python3 tests/test_setup_mode.py
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _setup_fixture as fx  # noqa: E402
from _setup_fixture import check  # noqa: E402


def refused(host):
    """True when nothing accepts a connection at host:3998."""
    try:
        with socket.create_connection((host, fx.SETUP_PORT), timeout=2):
            return False
    except OSError:
        return True


def lan_address():
    """This machine's own non-loopback address (no packet is sent), or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("203.0.113.1", 9))     # TEST-NET-3: only picks the outgoing interface
        ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        s.close()


LISTENER_HITS = []   # every request the "internal" listener ever receives
TRICKLE_STOP = threading.Event()


class Listener(BaseHTTPRequestHandler):
    """Stands for something on this machine a redirect must never reach."""
    def do_GET(self):
        LISTENER_HITS.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")
    do_POST = do_GET

    def log_message(self, *a):
        pass


class Hostile(BaseHTTPRequestHandler):
    """A guide / ComfyUI that misbehaves. REDIRECT: every path answers 302 to the
    listener. Otherwise: 200 with a huge Content-Length, then one byte every 0.2 s."""
    REDIRECT = False
    FLOOD = False   # instead: a 2 MiB chat reply at once, past Setup's 1 MiB read cap
    def _answer(self):
        if self.command == "POST":
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.REDIRECT:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:%d/internal-secret" % LISTEN_PORT)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        flood = (b'{"choices": [{"message": {"content": "' + b"a" * (2 * 1024 * 1024)
                 + b'"}, "finish_reason": "stop"}]}') if self.FLOOD else b""
        self.send_header("Content-Length", str(len(flood) if self.FLOOD else 100 * 1024 * 1024))
        self.end_headers()
        if self.FLOOD:              # a valid chat reply, just too big: only the cap can refuse it
            try:
                self.wfile.write(flood)
            except OSError:
                pass
            return
        try:
            self.wfile.write(b"{")
            while not TRICKLE_STOP.wait(0.2):
                self.wfile.write(b" ")
                self.wfile.flush()
        except OSError:
            pass
    do_GET = do_POST = _answer

    def log_message(self, *a):
        pass


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


def timed(method, path, body, limit):
    """-> (seconds, status, body); the client itself gives up after `limit` s."""
    t0 = time.monotonic()
    try:
        code, out = fx.http(method, path, body, timeout=limit)
    except Exception as e:           # the client gave up: the server held the request too long
        code, out = None, repr(e)
    return time.monotonic() - t0, code, out


try:
    if not fx.port_free(fx.SETUP_PORT):
        check("port %d is free for the Setup server" % fx.SETUP_PORT, False,
              "something else is listening on it; stop it and run again")
        fx.finish()
    lane_port, helper_port = fx.start_fakes()

    print("(a) no config file -> Setup mode, this computer only")
    proc, cfg_path, log_path = fx.boot("setup")
    health = fx.wait_health(proc)
    check("the server starts without a config file", health is not None,
          "exit=%s log=%s" % (proc.poll(), open(log_path).read()[-600:]))
    if health is None:
        fx.finish()
    check("/api/health says setup: true", health.get("setup") is True, health)
    check("it listens on the default port", health.get("port") == fx.SETUP_PORT, health)
    check("bound to 127.0.0.1: nothing answers on 127.0.0.2 (Linux loopback)",
          sys.platform != "linux" or refused("127.0.0.2"))
    lan = lan_address()
    check("bound to 127.0.0.1: nothing answers on this machine's LAN address",
          lan is None or refused(lan), lan)
    log = open(log_path).read()
    check("the log names the settings file by basename only",
          "config.json" in log and os.path.dirname(cfg_path) not in log, log[-400:])

    print("(b) only Setup answers")
    code, body = fx.http("GET", "/api/engines")
    check("/api/engines -> 503 Finish Setup first.",
          code == 503 and body == {"ok": False, "setup": True, "error": "Finish Setup first."}, (code, body))
    code, body = fx.http("POST", "/api/generate", {"mode": "x"})
    check("POST /api/generate -> 503", code == 503 and body.get("setup") is True, (code, body))
    code, body = fx.http("GET", "/api/setup/state")
    check("/api/setup/state ok", code == 200 and body.get("ok") and body.get("setup") is True
          and body.get("name") == "config.json" and body.get("exists") is False
          and set(body.get("tools", {})) == {"blender", "ffmpeg"}, (code, body))
    code, page = fx.http("GET", "/")
    check("/ serves the Setup page", code == 200 and "Set up Black Wire Forge" in page
          and "Step 1 of 5" in page, str(page)[:200])
    code, _ = fx.http("GET", "/help")
    check("/help still answers", code == 200, code)
    code, body = fx.http("POST", "/api/setup/probe-comfy", None, {"Content-Type": "text/plain"})
    check("a Setup POST without JSON content type -> 415 (the request guard)", code == 415, (code, body))
    code, body = fx.http("POST", "/api/setup/probe-comfy", {}, {"Origin": "http://evil.example"})
    check("a Setup POST from another site -> 403", code == 403, (code, body))

    print("(c) find ComfyUI")
    code, body = fx.http("POST", "/api/setup/probe-comfy", {"host": "127.0.0.1", "port": lane_port})
    found = (body.get("found") or {}) if isinstance(body, dict) else {}
    check("the fake ComfyUI is found at its port", code == 200 and body.get("ok"), (code, body))
    check("GPU name, VRAM GB and version come back",
          found.get("gpu") == "fake gpu" and found.get("vram_gb") == 16.0 and found.get("version") == "fake",
          found)
    check("only the shown fields come back (no raw body)",
          set(found) == {"host", "port", "gpu", "vram_gb", "version", "name", "this_computer"} and "ram_free" not in json.dumps(body),
          body)
    code, body = fx.http("POST", "/api/setup/probe-comfy", {"host": "127.0.0.1", "port": fx.free_port()})
    check("a closed port -> one plain sentence", code == 200 and body.get("ok") is False
          and body.get("error", "").startswith("No ComfyUI answered at 127.0.0.1:"), body)
    code, body = fx.http("POST", "/api/setup/probe-comfy", {"host": "a b/c", "port": 1})
    check("a bad address is refused before any request", code == 400 and body.get("ok") is False, (code, body))
    code, body = fx.http("POST", "/api/setup/probe-helper", {"url": "file:///etc/passwd"})
    check("a non-http(s) guide address is refused", code == 400 and body.get("ok") is False, (code, body))

    code, body = fx.http("POST", "/api/setup/preview", {"process": True, "who": "network"})
    check("no graphics card: the process lane, and the network bind when chosen",
          code == 200 and json.loads(body.get("text", "{}")) == {
              "bind": "0.0.0.0", "lanes": [{"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}]},
          (code, body))
    code, body = fx.http("POST", "/api/setup/preview", {"who": "local"})
    check("nothing to make things with -> a plain 400", code == 400 and body.get("ok") is False, (code, body))

    print("(d) switch on a guide")
    helper_base = "http://127.0.0.1:%d" % helper_port
    code, body = fx.http("POST", "/api/setup/probe-helper", {"url": helper_base})
    g = (body.get("found") or [{}])[0] if isinstance(body, dict) else {}
    check("the guide probe finds the FakeHelper's /models",
          code == 200 and body.get("ok") and g.get("url") == helper_base + "/v1"
          and g.get("models") == ["fake-model"], body)
    answers = {"comfy": {"host": "127.0.0.1", "port": lane_port, "name": "My ComfyUI"},
               "helper": {"url": g.get("url"), "model": "fake-model"}, "who": "local"}
    code, body = fx.http("POST", "/api/setup/preview", answers)
    check("an untested guide is not written", code == 400 and "Test the guide" in body.get("error", ""),
          (code, body))
    fx.HELPER_STATE["replies"].append("ready")
    code, body = fx.http("POST", "/api/setup/test-helper", {"url": g.get("url"), "model": "fake-model"})
    check("Test it returns the guide's reply", code == 200 and body.get("ok") and body.get("reply") == "ready",
          (code, body))
    last = fx.HELPER_STATE["requests"][-1] if fx.HELPER_STATE["requests"] else {}
    check("the test sent one short chat to the chosen model",
          last.get("model") == "fake-model"
          and last.get("messages") == [{"role": "user", "content": "Reply with the word ready."}], last)
    code, body = fx.http("POST", "/api/setup/test-helper", {"url": "http://127.0.0.1:%d/v1" % fx.free_port(),
                                                            "model": "x"})
    check("a guide that is not there -> one plain sentence",
          body.get("ok") is False and body.get("error", "").startswith("Nothing is answering at"), body)

    print("(s) security: Setup never follows a redirect, caps and time-limits what it reads")
    LISTEN_PORT = serve(Listener)
    hostile = "http://127.0.0.1:%d" % serve(Hostile)
    redirector = "http://127.0.0.1:%d" % serve(type("Redirector", (Hostile,), {"REDIRECT": True}))
    code, body = fx.http("POST", "/api/setup/test-helper", {"url": redirector + "/v1", "model": "m"})
    check("Test it against a guide answering 302: a plain error",
          code == 200 and body.get("ok") is False and body.get("error", "").startswith("The guide at"), (code, body))
    code, body = fx.http("POST", "/api/setup/probe-helper", {"url": redirector + "/v1"})
    check("the guide probe against a 302: nothing found", code == 200 and body.get("ok") is False, (code, body))
    code, body = fx.http("POST", "/api/setup/probe-comfy", {"host": redirector})
    check("the ComfyUI probe against a 302: nothing found", code == 200 and body.get("ok") is False, (code, body))
    check("the local listener got NO request from any redirect", LISTENER_HITS == [], LISTENER_HITS)
    secs, code, body = timed("POST", "/api/setup/probe-comfy",
                             {"host": "127.0.0.1", "port": int(hostile.rsplit(":", 1)[1])}, 20)
    check("a trickling ComfyUI probe gives up within its 4 s (+1 s margin), plainly",
          secs < 5 and code == 200 and body.get("ok") is False, (round(secs, 1), code, body))
    secs, code, body = timed("POST", "/api/setup/probe-helper", {"url": hostile + "/trickle/v1"}, 20)
    check("a trickling guide probe gives up within its 3 s (+1 s margin), plainly",
          secs < 4 and code == 200 and body.get("ok") is False, (round(secs, 1), code, body))
    flood = "http://127.0.0.1:%d" % serve(type("Flood", (Hostile,), {"FLOOD": True}))
    secs, code, body = timed("POST", "/api/setup/test-helper", {"url": flood + "/v1", "model": "m"}, 20)
    check("Test it against a guide answering 2 MiB: refused past the 1 MiB cap, plainly, at once",
          secs < 5 and code == 200 and body.get("ok") is False and body.get("error", "").startswith("The guide at"),
          (round(secs, 1), code, body))
    secs, code, body = timed("POST", "/api/setup/test-helper", {"url": hostile + "/trickle/v1", "model": "m"}, 80)
    check("Test it against a trickling guide gives up within its 60 s (+3 s margin), plainly",
          secs < 63 and code == 200 and body.get("ok") is False and body.get("error", "").startswith("The guide at"),
          (round(secs, 1), code, body))
    TRICKLE_STOP.set()

    print("(e) write, re-exec, configured")
    code, body = fx.http("POST", "/api/setup/preview", answers)
    expected = {"bind": "127.0.0.1",
                "lanes": [{"id": "comfy", "name": "My ComfyUI", "host": "127.0.0.1", "port": lane_port}],
                "helper": {"url": helper_base + "/v1", "model": "fake-model"}}
    check("the preview is the exact file", code == 200 and json.loads(body.get("text", "{}")) == expected,
          (code, body))
    preview_text = body.get("text")
    pid = proc.pid
    code, body = fx.http("POST", "/api/setup/write", answers)
    check("write -> ok", code == 200 and body.get("ok"), (code, body))
    health = fx.wait_health(proc, want_setup=False)
    check("the server re-execs itself: health says setup false", health is not None and health.get("lanes") == 1,
          "health=%s exit=%s log=%s" % (health, proc.poll(), open(log_path).read()[-600:]))
    check("same process (re-exec, not a new one)", proc.poll() is None and proc.pid == pid)
    written = open(cfg_path).read() if os.path.exists(cfg_path) else ""
    check("config.json exists and is what the preview showed", written == preview_text, written)
    check("no temp file left beside it",
          sorted(os.listdir(os.path.dirname(cfg_path))) == ["config.json", "data", "server.log"],
          os.listdir(os.path.dirname(cfg_path)))
    code, lanes = fx.http("GET", "/api/lanes")
    lanes = lanes.get("lanes") if isinstance(lanes, dict) else None
    lane = next((x for x in lanes if isinstance(x, dict) and x.get("id") == "comfy"), None) \
        if isinstance(lanes, list) else None
    check("/api/lanes shows the lane", code == 200 and lane is not None and lane.get("name") == "My ComfyUI",
          (code, str(lanes)[:300]))
    code, eng = fx.http("GET", "/api/engines")
    check("/api/engines answers now, with the guide on", code == 200 and eng.get("helper") is True,
          (code, str(eng)[:200]))

    print("(f) never overwrite; Setup is gone once configured")
    for method, path, b in (("GET", "/api/setup/state", None), ("POST", "/api/setup/write", answers),
                            ("POST", "/api/setup/probe-comfy", {"host": "127.0.0.1", "port": lane_port})):
        code, _ = fx.http(method, path, b)
        check("%s %s -> 404 in normal mode" % (method, path), code == 404, code)
    code, page = fx.http("GET", "/")
    check("/ is the app again, not Setup", code == 200 and "Set up Black Wire Forge" not in page)
    fx.stop(proc)

    proc2, cfg2, log2 = fx.boot("race")
    check("second Setup server up", fx.wait_health(proc2, want_setup=True) is not None, open(log2).read()[-400:])
    theirs = '{"lanes": [{"id": "mine", "name": "Mine", "host": "127.0.0.1", "port": 1}]}\n'
    with open(cfg2, "w") as f:           # e.g. an agent writes config.json while Setup is open
        f.write(theirs)
    code, body = fx.http("POST", "/api/setup/write", answers)
    check("a second write -> 409 A settings file already exists.",
          code == 409 and body.get("error") == "A settings file already exists.", (code, body))
    check("the existing file is untouched", open(cfg2).read() == theirs)
    check("no temp file left beside it", sorted(os.listdir(os.path.dirname(cfg2))) == ["config.json", "data",
                                                                                      "server.log"],
          os.listdir(os.path.dirname(cfg2)))
    code, h = fx.http("GET", "/api/health")
    check("and the server stays in Setup (no restart)", code == 200 and h.get("setup") is True, h)
    fx.stop(proc2)

    print("(g) a broken config still exits with code 2")
    for name, text, words in (("badjson", "{not json", "is not valid JSON"),
                              ("nolanes", '{"lanes": []}', "non-empty \"lanes\" list")):
        p, cfg, lp = fx.boot(name, text)
        try:
            rc = p.wait(30)
        except subprocess.TimeoutExpired:
            rc = None
            fx.stop(p)
        out = open(lp).read()
        check("%s: exit code 2 with its own message" % name, rc == 2 and words in out, (rc, out[-300:]))
        check("%s: the file is left as it was" % name, open(cfg).read() == text)
finally:
    fx.finish()
