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
               "caps": ["image", "video", "audio"]},
              {"id": "v", "name": "Video machine", "host": "127.0.0.1", "port": PORT_LANE, "caps": ["video"]},
              {"id": "p", "name": "Picture machine", "host": "127.0.0.1", "port": PORT_LANE, "caps": ["image"]}]
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

# Give the lane models so generate() considers it able. "t" does everything (pictures and LTX video);
# "v" only LTX video, "p" only pictures (the issue #2 shape: one machine rarely does both).
LTX_MODELS = json.load(open(os.path.join(HERE, "golden", "ltx_models.json")))
srv.LANE_BY_ID["t"]["models"] = dict(MODELS, **LTX_MODELS)
srv.LANE_BY_ID["v"]["models"] = dict(LTX_MODELS)
srv.LANE_BY_ID["p"]["models"] = {k: v for k, v in MODELS.items() if k.startswith("qwen")}
with srv.STATE_LOCK:
    srv.LANE_STATE["t"] = {"up": True, "checked": time.time(), "err": ""}
    srv.LANE_STATE["v"] = {"up": True, "checked": time.time(), "err": ""}
    srv.LANE_STATE["p"] = {"up": True, "checked": time.time(), "err": ""}

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


print("\n--- the made-up person is drawn with the Picture room's Default recipe, not the field defaults ---")
_sent = []
_real_generate = srv.generate
srv.generate = lambda p: (_sent.append(p), ({"ok": True, "job": {"id": "jp"}}, 200))[1]
try:
    srv._ForgeBackend("r", srv.LANE_BY_ID["t"], 576, 768).make_portrait("a person", 576, 768)
finally:
    srv.generate = _real_generate
_default = next(x for x in srv.engines.presets("image", "t2i") if x.get("id") == "default")["values"]
_p = _sent[0] if _sent else {}
check("make_portrait sends the Default recipe's sampler, scheduler, steps, guidance and cfg",
      all(_p.get(k) == _default[k] for k in ("sampler", "scheduler", "steps", "guidance_style", "cfg")),
      {k: _p.get(k) for k in ("sampler", "scheduler", "steps", "guidance_style", "cfg")})
check("...at the video's own size, with the prompt", _p.get("width") == 576 and _p.get("height") == 768
      and _p.get("prompt") == "a person" and _p.get("recipe") == "default", _p)

print("\n--- validation sentences (exact) ---")
def err(body):
    payload, code = post(body)
    return code, payload.get("error", ""), payload.get("ok")
c, e, ok = err(dict(GOOD, lane="nonexistent"))
check("unknown lane", c == 400 and e == "unknown lane 'nonexistent'" and ok is False, (c, e))
c, e, ok = err({"lane": "t", "photo": "me.png"})
check("no song", c == 400 and e == "Pick a finished song first, or add a song file.", (c, e))
v, errs = srv._forge_run_validation({"lane": "t", "song_upload": "abc_mysong.mp3", "photo": "me.png", "lyrics": "[Verse]\nla"})
check("a song file of your own is accepted in place of a History song, with its lyrics",
      not errs and v["song_job"] is None and v["song_upload"] == "abc_mysong.mp3" and v["lyrics"] == "[Verse]\nla", (errs, v))
for bad in ("../x.mp3", "song.txt", "a\x00b.mp3"):
    c, e, ok = err({"lane": "t", "song_upload": bad, "photo": "me.png"})
    check("song file %r is refused with one sentence" % bad, c == 400 and "song file" in e, (c, e))
_got = []
_real_upload = srv._resolve_upload_bytes
srv._resolve_upload_bytes = lambda lane_id, name: (_got.append((lane_id, name)), (b"RIFF", name))[1]
_real_master = srv.seq_master_import
srv.seq_master_import = lambda sid, rev, fname, data: ({"master": {"seconds": 12.0}}, 200)
try:
    be = srv._ForgeBackend("r", srv.LANE_BY_ID["t"], 576, 768, song_upload="abc_mysong.mp3")
    be._rev = lambda sid: 1
    secs = be.import_master("sid", None)
finally:
    srv._resolve_upload_bytes = _real_upload
    srv.seq_master_import = _real_master
check("the run reads the song file from the lane it was uploaded to", _got == [("t", "abc_mysong.mp3")] and secs == 12.0, (_got, secs))
c, e, ok = err(dict(GOOD, song_job="song_running"))
check("unfinished song", c == 400 and e == "Pick a finished song first.", (c, e))
c, e, ok = err(dict(GOOD, song_job="pic1"))
check("a picture job is not a song", c == 400 and e == "Pick a finished song first.", (c, e))
c, e, ok = err(dict(GOOD, song_job="nope"))
check("unknown song job", c == 400 and e == "Pick a finished song first.", (c, e))
c, e, ok = err({"lane": "t", "song_job": "song1"})
check("no photo and no ask to make one", c == 400 and e == "Add a photo of who is in the video, or ask for one to be made.", (c, e))
v, errs = srv._forge_run_validation(dict(GOOD, photo="", make_photo=True, person="a woman with red hair"))
check("make_photo with no photo is accepted: no photo name, the description kept",
      not errs and v["photo"] is None and v["person"] == "a woman with red hair", (errs, v))
v, errs = srv._forge_run_validation(dict(GOOD, make_photo=True, person="ignored"))
check("a photo given wins over make_photo (and drops the description)", not errs and v["photo"] == "me.png" and v["person"] == "", (errs, v))
c, e, ok = err({"lane": "t", "song_job": "song1", "make_photo": True, "person": "x" * 201})
check("person too long", c == 400 and "too long" in e and "200" in e, (c, e))
c, e, ok = err({"lane": "t", "song_job": "song1", "make_photo": "yes"})
check("make_photo must be exactly true", c == 400, (c, e))
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


print("\n--- issue #2: the picture machine and the video machine are resolved apart ---")
def lanes_up(**up):
    with srv.STATE_LOCK:
        for k, val in up.items():
            srv.LANE_STATE[k]["up"] = val
lanes_up(t=False, v=True, p=True)
v, errs = srv._forge_run_validation(dict(GOOD, lane="v"))
check("(a) a video-only lane as the request's lane + an up picture lane validates", not errs and v, errs)
check("(a) picture_lane is the picture lane, video_lane is the request's lane",
      v and v["picture_lane"]["id"] == "p" and v["video_lane"]["id"] == "v" and v["lane"]["id"] == "v",
      v and (v["picture_lane"]["id"], v["video_lane"]["id"]))
v, errs = srv._forge_run_validation(dict(GOOD, lane="v", photo="", make_photo=True))
check("(a) make_photo needs t2i as well: the picture lane still resolves", not errs and v["picture_lane"]["id"] == "p", errs)
lanes_up(t=True, v=True, p=True)
v, errs = srv._forge_run_validation(dict(GOOD, lane="t"))
check("one lane that does both keeps doing both", not errs and v["picture_lane"]["id"] == "t" and v["video_lane"]["id"] == "t", errs)

_sent = []
srv.generate = lambda p: (_sent.append(p), ({"ok": True, "job": {"id": "jp%d" % len(_sent)}}, 200))[1]
try:
    be = srv._ForgeBackend("r", srv.LANE_BY_ID["v"], 576, 768, picture_lane=srv.LANE_BY_ID["p"], video_lane=srv.LANE_BY_ID["v"])
    be._photo_for_picture_lane = lambda name: name
    still_job = be.make_still("a scene", "me.png", 576, 768)
    portrait_job = be.make_portrait("a person", 576, 768)
finally:
    srv.generate = _real_generate
check("(b) make_still posts to the picture lane", _sent[0]["lane"] == "p" and _sent[0]["mode"] == "edit", _sent[0])
check("(b) make_portrait posts to the picture lane", _sent[1]["lane"] == "p" and _sent[1]["mode"] == "t2i", _sent[1])

_carried = []
_real_carry = srv.carry
srv.carry = lambda job, idx, lane, fit=None, cache_path=None: (_carried.append(lane["id"]), ("on_" + lane["id"] + ".png", ""))[1]
with srv.JOBS_LOCK:
    srv.JOBS[still_job] = {"id": still_job, "lane": "p", "status": "done", "outputs": [{"filename": "x.png", "media": "image"}]}
    srv.JOBS[portrait_job] = {"id": portrait_job, "lane": "p", "status": "done", "outputs": [{"filename": "y.png", "media": "image"}]}
try:
    still_name = be.carry_still(still_job)
    portrait_name = be.carry_still(portrait_job)
finally:
    srv.carry = _real_carry
check("(c) carry_still carries a still onto the video lane", _carried[0] == "v" and still_name == "on_v.png", _carried)
check("(c) a made-up person is carried back onto the picture lane (it is the stills' reference)",
      _carried[1] == "p" and portrait_name == "on_p.png", _carried)

_seq = []
_real_seq_generate = srv.seq_generate
srv.seq_generate = lambda p: (_seq.append(p), ({"ok": True, "job": {"id": "jshot"}}, 200))[1]
try:
    be.generate_shot("sid", "slot")
finally:
    srv.seq_generate = _real_seq_generate
check("(c) the shots are pinned to the video lane", _seq and _seq[0].get("lane") == "v", _seq)

lanes_up(t=False, v=True, p=False)
with forge_run.FORGE_RUN_LOCK:
    _runs_before = len(forge_run.FORGE_RUNS)
payload, code = post(dict(GOOD, lane="v"))
check("(d) no picture-capable lane up -> one plain sentence, before any run starts",
      code == 400 and payload.get("ok") is False and payload.get("error") ==
      "No machine that is up can make the pictures (it needs the picture edit mode). Start one, then try again.", (code, payload))
with forge_run.FORGE_RUN_LOCK:
    _runs_after = len(forge_run.FORGE_RUNS)
check("(d) ...and no run was created", _runs_after == _runs_before, (_runs_before, _runs_after))
lanes_up(t=False, v=False, p=True)
payload, code = post(dict(GOOD, lane="p"))
check("no video-capable lane up -> one plain sentence naming the video",
      code == 400 and "make the video" in payload.get("error", "") and payload["error"].count(".") == 2, (code, payload))
lanes_up(t=True, v=True, p=True)

for bad in ("song.mp3", "voice.WAV", "x.flac"):
    try:
        be.make_still("a scene", bad, 576, 768)
        refused = None
    except forge_run.RunError as e:
        refused = str(e)
    check("(e) make_still refuses a sound file %r as a picture" % bad, refused and "sound file" in refused, refused)
    try:
        be.add_shot("sid", {"frames": 97}, "p", bad, 576, 768)
        refused = None
    except forge_run.RunError as e:
        refused = str(e)
    check("(e) add_shot refuses a sound file %r as a start image" % bad, refused and "sound file" in refused, refused)

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
