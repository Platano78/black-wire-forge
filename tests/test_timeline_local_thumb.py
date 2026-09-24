"""Browser acceptance gate: a Cutting Room timeline tile for a PICKED take
still shows real media when its lane is unreachable, as long as the harvest
already copied that take locally (data/seq/<id>/takes/) -- orchestrator
review 2026-09-24: the whole point of the local harvest is working with the
lane off, and the tile was going blank anyway because it always sourced from
the (unreachable) lane's own /api/view instead of the local copy it already
has.

ISOLATION: every byte this writes goes under a fresh tempfile.mkdtemp()
directory. The one lane in this test's config points at a port nothing is
listening on -- genuinely unreachable, not just flagged down -- and the
video slot's state ("ready", with a take and a pick) is seeded straight
onto disk (data/jobs.json + data/sequences/<id>.json + the take's own file
under data/seq/<id>/takes/) before server.py ever starts, the same
pre-seeding load_jobs() already reads at startup. No render, no fake
ComfyUI lane -- nothing needs to actually be reachable for this check.

Run: python3 tests/test_timeline_local_thumb.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(REPO)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p

def wait_true(desc, fn, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(0.15)
    check(desc, False, "still false after %.0fs" % timeout)
    return False

def http_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())

def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

SID = "s_00000001"
JOB_ID = "harvokjob1"
TAKE_FILE = "takes/%s.mp4" % JOB_ID

# Finding #21: check playwright/chromium BEFORE any fixture setup (server,
# tempfile) -- a clean clone with no dev deps installed must SKIP cleanly
# and cheaply, not fail after standing up a whole fixture first.
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed. pip install -r requirements-dev.txt "
          "&& python3 -m playwright install --with-deps chromium")
    sys.exit(0)
try:
    with sync_playwright() as _pw_probe:
        _pw_probe.chromium.launch().close()
except Exception as _pw_err:
    print("SKIP: playwright's chromium browser is not installed (%s). "
          "Run: python3 -m playwright install --with-deps chromium" % _pw_err)
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="bwf-local-thumb-")
data_dir = os.path.join(tmp, "data")
os.makedirs(os.path.join(data_dir, "sequences"))
os.makedirs(os.path.join(data_dir, "seq", SID, "takes"))

# A lane pointing at a port nothing is listening on: genuinely unreachable,
# not merely flagged down by test scaffolding.
dead_port = free_port()

now = time.time()
with open(os.path.join(data_dir, "jobs.json"), "w") as f:
    json.dump([{
        "id": JOB_ID, "lane": "deadlane", "kind": "video", "mode": "ltx", "status": "done",
        "created": now, "started": now, "finished": now, "elapsed": 1.0,
        "outputs": [{"filename": "%s.mp4" % JOB_ID, "subfolder": "", "type": "output", "media": "video"}],
        "sequence_id": SID, "slot_id": "v1",
    }], f)

with open(os.path.join(data_dir, "sequences", SID + ".json"), "w") as f:
    json.dump({
        "id": SID, "schema": 1, "rev": 1, "title": "Local Thumb Test", "mode": "sequence",
        "created": now, "updated": now, "canvas": {"width": 1024, "height": 576},
        "refs": [], "beats": [],
        "slots": [{
            "id": "v1", "lane": "video", "beat_id": None, "cap": "video", "mode": "ltx",
            "recipe": None, "quality": None, "values": {"prompt": "a scene"}, "refs": "auto",
            "takes": [{"job_id": JOB_ID, "made": now, "beat_rev": None,
                       "inputs": {"refs": [], "cables": {}}, "file": TAKE_FILE}],
            "pick": JOB_ID, "trim": None, "title": None,
        }],
        "cables": [], "cuts": [],
    }, f)

with open(os.path.join(data_dir, "seq", SID, "takes", "%s.mp4" % JOB_ID), "wb") as f:
    f.write(b"pretend-mp4-bytes-local-thumb-0001")

cfg_path = os.path.join(tmp, "config.json")
server_port = free_port()
with open(cfg_path, "w") as f:
    json.dump({
        "title": "Local Thumb Test", "port": server_port, "bind": "127.0.0.1",
        "lanes": [{"id": "deadlane", "name": "Dead lane", "host": "127.0.0.1", "port": dead_port,
                   "caps": ["video"]}],
        "timing": {"poll_seconds": 0.4, "job_poll_seconds": 1.0, "http_timeout": 1.0,
                   "free_settle_seconds": 1.0, "discover_seconds": 300.0},
    }, f)

URL = "http://127.0.0.1:%d/" % server_port
logf = open(os.path.join(tmp, "server.log"), "w")
server = subprocess.Popen(
    [sys.executable, os.path.join(REPO, "server.py")],
    cwd=REPO, env=dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data_dir),
    stdout=logf, stderr=subprocess.STDOUT)

if not wait_true("server is up", lambda: http_json(URL + "api/lanes"), 20):
    stop(server); logf.close()
    with open(os.path.join(tmp, "server.log")) as f:
        print("  -- server.py said: %s" % f.read()[-800:])
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright
    print("seeded slot 'ready' with a picked, locally-harvested take; its lane is unreachable")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(URL + "#room=cutting&seq=" + SID, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1200)

        slot_sel = '#tlTrackVideo [data-slot-id="v1"]'
        page.wait_for_selector(slot_sel, timeout=10000)
        cls = page.eval_on_selector(slot_sel, "el => el.className")
        check("the seeded slot renders 'ready' (a take exists and is picked)", "tl-slot-ready" in cls, cls)

        video_src = page.eval_on_selector(slot_sel + " video", "el => el ? el.getAttribute('src') : null")
        check("the tile's <video> element exists", video_src is not None, cls)
        check("the tile's <video> src is the LOCAL harvested copy (/api/sequence/file), not the dead lane's /api/view",
              video_src is not None and video_src.startswith("/api/sequence/file"), video_src)
        check("the src names this sequence's id and the take's own local path",
              video_src is not None and ("id=" + SID) in video_src and "path=takes" in video_src, video_src)

        browser.close()
finally:
    stop(server)
    logf.close()
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("FAILED: %d" % len(FAILED))
if FAILED:
    for n in FAILED:
        print("  - " + n)
sys.exit(1 if FAILED else 0)
