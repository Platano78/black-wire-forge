"""API tests for /api/forge/music-video, /api/forge/run and /api/forge/stop.

Starts the server in-process against a fake ComfyUI lane (like test_compare.py)
and tests validation paths (400 errors), 409 for concurrent runs, and the
GET/stop endpoints.

Run: python3 tests/test_forge_run_api.py
"""
import json
import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import http.server
import urllib.error

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import _scratch_config  # noqa: E402  # sets GENCENTER_CONFIG / GENCENTER_DATA

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (
        ("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# Fake ComfyUI lane
# ---------------------------------------------------------------------------

SCRATCH = tempfile.mkdtemp(prefix="bwf_forge_api_")
print("scratch dir: %s" % SCRATCH)


def start_fake(name):
    store = os.path.join(SCRATCH, "store_%s" % name)
    os.makedirs(os.path.join(store, "outputs"))
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
         "--port", str(port), "--store", store],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % port, timeout=0.5)
            break
        except Exception:
            time.sleep(0.05)
    else:
        raise SystemExit("fake lane %s did not come up" % name)
    return proc, port, store


FAKE_LANE, PORT_LANE, STORE_LANE = start_fake("lane")

CFG_PATH = os.path.join(SCRATCH, "config.json")
app_port = free_port()
json.dump({
    "port": app_port, "bind": "127.0.0.1", "title": "forge run api test",
    "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
    "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": PORT_LANE,
               "caps": ["image", "video", "audio"]}]
}, open(CFG_PATH, "w"))
os.environ["GENCENTER_CONFIG"] = CFG_PATH
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")
for d in ("data", "data/outputs", "data/seq"):
    os.makedirs(os.path.join(SCRATCH, d), exist_ok=True)

import importlib.util
MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
MODELS.update({"ace_unet": "ace.safetensors", "ace_clip1": "a.safetensors",
               "ace_clip2": "b.safetensors", "ace_vae": "v.safetensors"})

spec = importlib.util.spec_from_file_location("srv", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# Give the lane models so generate() considers it able.
srv.LANE_BY_ID["t"]["models"] = dict(MODELS)
with srv.STATE_LOCK:
    srv.LANE_STATE["t"] = {"up": True, "checked": time.time(), "err": ""}

# ---------------------------------------------------------------------------
# Start HTTP server in a thread
# ---------------------------------------------------------------------------

httpd = http.server.HTTPServer(("127.0.0.1", app_port), srv.Handler)
httpd.timeout = 0.5
srv_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
srv_thread.start()

# Wait for the server to be ready
for _ in range(50):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/health" % app_port, timeout=0.5)
        break
    except Exception:
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def call_api(body, path="/api/forge/music-video"):
    """Send a POST request, return (payload, code)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (app_port, path),
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        code = resp.getcode()
        payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        code = e.code
        payload = json.loads(e.read().decode())
    return payload, code


def call_get(path="/api/forge/run"):
    """Send a GET request, return (payload, code)."""
    try:
        url = "http://127.0.0.1:%d%s" % (app_port, path)
        resp = urllib.request.urlopen(url, timeout=5)
        code = resp.getcode()
        payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        code = e.code
        payload = json.loads(e.read().decode())
    return payload, code


import forge_run  # noqa: E402

# A finished song in History, and a picture job, planted the way a real finished job looks.
with srv.JOBS_LOCK:
    srv.JOBS["song1"] = {"id": "song1", "lane": "t", "kind": "audio", "mode": "song", "status": "done",
                         "outputs": [{"filename": "s.mp3", "subfolder": "", "type": "output", "media": "audio"}],
                         "args": {"lyrics": "[Verse]\nla la la\nla la"}, "created": time.time()}
    srv.JOBS["pic1"] = {"id": "pic1", "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
                        "outputs": [{"filename": "p.png", "subfolder": "", "type": "output", "media": "image"}], "created": time.time()}
    srv.JOBS["song_running"] = {"id": "song_running", "lane": "t", "kind": "audio", "mode": "song", "status": "running", "outputs": []}
GOOD = {"lane": "t", "song_job": "song1", "photo": "me.png"}


def post(body, path="/api/forge/music-video"):
    return call_api(body, path)


print("\n--- validation sentences (exact) ---")
def err(body):
    payload, code = post(body)
    return code, payload.get("error", ""), payload.get("ok")
c, e, ok = err(dict(GOOD, lane="nonexistent"))
check("unknown lane", c == 400 and e == "unknown lane 'nonexistent'" and ok is False, (c, e))
c, e, ok = err({"lane": "t", "photo": "me.png"})
check("no song", c == 400 and e == "Pick a finished song first.", (c, e))
c, e, ok = err(dict(GOOD, song_job="song_running"))
check("unfinished song", c == 400 and e == "Pick a finished song first.", (c, e))
c, e, ok = err(dict(GOOD, song_job="pic1"))
check("a picture job is not a song", c == 400 and e == "Pick a finished song first.", (c, e))
c, e, ok = err(dict(GOOD, song_job="nope"))
check("unknown song job", c == 400 and e == "Pick a finished song first.", (c, e))
c, e, ok = err({"lane": "t", "song_job": "song1"})
check("no photo", c == 400 and e == "Add a photo of who is in the video.", (c, e))
c, e, ok = err(dict(GOOD, style="x" * 201))
check("style too long", c == 400 and "too long" in e and "200" in e, (c, e))
c, e, ok = err(dict(GOOD, lyrics="x" * 6001))
check("lyrics too long", c == 400 and "too long" in e and "6000" in e, (c, e))
for bad in (dict(width=100), dict(width=577), dict(height=2000), dict(height="tall")):
    c, e, ok = err(dict(GOOD, **bad))
    check("bad size %s" % bad, c == 400 and ("multiple of 16" in e or "numbers" in e), (c, e))
with srv.STATE_LOCK:
    srv.LANE_STATE["t"]["up"] = False
c, e, ok = err(GOOD)
check("a lane that is down says so", c == 400 and "offline" in e, (c, e))
with srv.STATE_LOCK:
    srv.LANE_STATE["t"]["up"] = True

print("\n--- validated body: the song's own lyrics are used, defaults applied ---")
v, errors = srv._forge_run_validation(dict(GOOD))
check("no errors for a good body", errors == [], errors)
check("default canvas 576x768", (v["width"], v["height"]) == (576, 768), v)
check("lyrics come from the song job's args", "la la la" in (v["lyrics"] or ""), v["lyrics"])
v2, _ = srv._forge_run_validation(dict(GOOD, lyrics="my words"))
check("lyrics in the request win", v2["lyrics"] == "my words")

print("\n--- one run at a time, GET, stop (the run thread is stubbed) ---")
release = threading.Event()
def stub_thread(run_id, v):
    release.wait(20)
    forge_run.update_run(run_id, status="done", stage="done", message="Your video is ready.")
srv._forge_thread = stub_thread
payload, code = post(GOOD)
check("a good request starts a run", code == 200 and payload.get("ok") is True and payload.get("run_id"), (code, payload))
rid = payload.get("run_id")
payload2, code2 = post(GOOD)
check("a second request while one runs -> 409 with the sentence",
      code2 == 409 and payload2.get("error") == "A music video is already being made. Wait for it, or stop it first.", (code2, payload2))
rec, code3 = call_get("/api/forge/run?id=" + rid)
check("GET by id returns the running record", code3 == 200 and rec.get("status") == "running" and rec.get("kind") == "music-video", rec)
newest, code4 = call_get("/api/forge/run")
check("GET with no id returns the newest record", code4 == 200 and newest.get("id") == rid, newest)
_, code5 = call_get("/api/forge/run?id=unknown999")
check("GET unknown id -> 404", code5 == 404, code5)
sp, sc = post({"id": rid}, "/api/forge/stop")
check("stop -> ok", sc == 200 and sp.get("ok") is True, (sc, sp))
check("stop sets the flag the run checks", forge_run.FORGE_RUN_STOP.get(rid) is True)
sp2, sc2 = post({"id": "no_such_run"}, "/api/forge/stop")
check("stop unknown -> 404", sc2 == 404 and sp2.get("ok") is False, (sc2, sp2))
release.set(); time.sleep(0.5)
rec_done, _ = call_get("/api/forge/run?id=" + rid)
check("the record finishes", rec_done.get("status") == "done", rec_done)
payload3, code6 = post(GOOD)
check("after it finishes a new run may start", code6 == 200, (code6, payload3))
release.set(); time.sleep(0.5)

print("\n--- a restart: a record still 'running' becomes an error with a plain sentence ---")
d = tempfile.mkdtemp(prefix="bwf_forge_cfg_")
json.dump({"abc": {"id": "abc", "kind": "music-video", "status": "running", "created": 1.0, "done": 3, "total": 10},
           "def": {"id": "def", "kind": "music-video", "status": "done", "created": 2.0}}, open(os.path.join(d, "forge_runs.json"), "w"))
with forge_run.FORGE_RUN_LOCK:
    forge_run.FORGE_RUNS.clear()
forge_run.configure(d)
a = forge_run.get_run("abc")
check("a running record from a dead process is an error", a["status"] == "error" and "restarted" in a["error"], a)
check("a finished record is kept as it was", forge_run.get_run("def")["status"] == "done")
forge_run.configure(DATA_DIR_FOR_TESTS) if "DATA_DIR_FOR_TESTS" in dir() else None

print()
if FAILED:
    print("FAILED (%d): %s" % (len(FAILED), ", ".join(FAILED)))
else:
    print("All tests passed.")
sys.exit(1 if FAILED else 0)
