"""Shared by the W1 Setup suites (tests/test_setup_mode.py, tests/test_setup_ui.py): a fake
ComfyUI lane (tests/fixtures/fake_comfy.py), a FakeHelper that also answers GET /v1/models,
and server.py as a subprocess pointed at a config file that does NOT exist yet -- Setup mode.

Setup mode listens on the app's default port, 3998 (there is no port setting without a
config), so these suites need 3998 free and boot one Setup server at a time."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import _picture_server as ps  # noqa: E402
from _picture_server import FAILED, check, free_port  # noqa: E402,F401

SETUP_PORT = 3998
BASE = "http://127.0.0.1:%d" % SETUP_PORT
SCRATCH = tempfile.mkdtemp(prefix="bwf_setup_")
PROCS = []
HELPER_STATE = ps.HELPER_STATE


class ModelsHelper(ps.FakeHelper):
    """FakeHelper plus the OpenAI-compatible model list Setup's guide probe reads."""
    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            return self._send(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
        return ps.FakeHelper.do_GET(self)


def port_free(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # as the server does: TIME_WAIT is not "in use"
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def start_fakes():
    """-> (fake ComfyUI port, fake helper port)."""
    helper = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHelper)
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    store = os.path.join(SCRATCH, "lane")
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    lane_port = free_port()
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port",
                                   str(lane_port), "--store", store], cwd=REPO,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for _ in range(100):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % lane_port, timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    return lane_port, helper.server_address[1]


def boot(name, config_text=None):
    """Start server.py with GENCENTER_CONFIG at <scratch>/<name>/config.json, which does
    not exist unless config_text is given. -> (process, config path, log path)."""
    d = os.path.join(SCRATCH, name)
    os.makedirs(d, exist_ok=True)
    cfg = os.path.join(d, "config.json")
    if config_text is not None:
        with open(cfg, "w") as f:
            f.write(config_text)
    logp = os.path.join(d, "server.log")
    env = dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=os.path.join(d, "data"))
    p = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                         stdout=open(logp, "w"), stderr=subprocess.STDOUT)
    PROCS.append(p)
    return p, cfg, logp


def wait_health(proc, want_setup=None, timeout=20):
    """-> /api/health's JSON once it answers (and, if want_setup is given, once
    its "setup" equals it), or None if the server exited or never did."""
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return None
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=1) as r:
                h = json.loads(r.read())
            if want_setup is None or h.get("setup") == want_setup:
                return h
        except Exception:
            pass
        time.sleep(0.1)
    return None


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
    for _ in range(50):                       # the next boot needs the port back
        if port_free(SETUP_PORT):
            return
        time.sleep(0.1)


def http(method, path, body=None, headers=None, timeout=30):
    """-> (status, parsed JSON or text)."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        code, raw = e.code, e.read()
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw.decode("utf-8", "replace")


def finish():
    """Report and exit; called from a `finally` (and may be called early to bail out)."""
    if PROCS and PROCS[0] is None:
        return
    exc = sys.exc_info()[0]
    if exc is not None and not issubclass(exc, SystemExit):
        import traceback
        traceback.print_exc()
        FAILED.append("the suite crashed")
    for p in PROCS:
        if p.poll() is None:
            p.terminate()
    PROCS[:] = [None]
    print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
    sys.exit(1 if FAILED else 0)
