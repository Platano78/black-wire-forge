"""Acceptance gate for the rewire (atomic task 4).

server.py must keep behaving EXACTLY as it did before the engine packs existed,
while no longer containing any model name itself. Golden captured from the
pre-rewire code; nothing here is a judgement call.

Run: python3 tests/test_server_behaviour.py
"""
import importlib.util, json, os, sys
sys.dont_write_bytecode = True   # same-length edits keep file SIZE identical, so a
# stale .pyc can answer for source you just changed (observed 2026-09-22).
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, got, want):
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + name)
    if not ok:
        print("          want: %r" % (want,)); print("          got : %r" % (got,))
        FAILED.append(name)

G = json.load(open(os.path.join(HERE, "golden", "server_behaviour.json")))
models = json.load(open(os.path.join(HERE, "golden", "models.json")))

import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv", os.path.join(ROOT, "server.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

lane = {"id": "t", "name": "T", "box": "b", "note": "", "host": "127.0.0.1", "port": 1,
        "caps": ["image", "video"], "models": models}
empty = dict(lane); empty["id"] = "t2"; empty["models"] = {}
for l in (lane, empty):
    m.LANE_BY_ID[l["id"]] = l
    if l not in m.LANES: m.LANES.append(l)

def pinned(got, golden):
    """Only the keys the golden knows about, so a new pack's new ability keys
    (e.g. the audio pack's song/music/sfx/yue2/cover/audio) don't fail this
    check -- everything the golden DOES pin must still match exactly."""
    return {k: v for k, v in got.items() if k in golden}


print("server behaviour is unchanged by the extraction")
check("abilities with all models",   pinned(m.abilities(lane), G["abilities"]),  G["abilities"])
check("abilities with none",         pinned(m.abilities(empty), G["abilities_empty"]), G["abilities_empty"])
check("describe_image",              m.describe_image(lane), G["describe_image"])
check("describe_video",              m.describe_video(lane), G["describe_video"])
check("missing_for image (have)",    m.missing_for(lane, "image"),  G["missing_image"])
check("missing_for video (have)",    m.missing_for(lane, "video"),  G["missing_video"])
check("missing_for ref2v (have)",    m.missing_for(lane, "ref2v"),  G["missing_ref2v"])
check("missing_for image (none)",    m.missing_for(empty, "image"), G["missing_image_empty"])
check("missing_for video (none)",    m.missing_for(empty, "video"), G["missing_video_empty"])

# The either-clip case. H3 accepts the nvfp4 OR the int8 encoder; a fixture with only
# nvfp4 never exercises that, and the first extraction silently regressed it.
int8 = json.load(open(os.path.join(HERE, "golden", "models_int8.json")))
lane_i8 = dict(lane); lane_i8["id"] = "i8"; lane_i8["models"] = int8
turbo = {k: "" for k in models}; turbo["h3_turbo_lora"] = models["h3_turbo_lora"]
lane_tb = dict(lane); lane_tb["id"] = "tb"; lane_tb["models"] = turbo
for l in (lane_i8, lane_tb):
    m.LANE_BY_ID[l["id"]] = l
    if l not in m.LANES: m.LANES.append(l)
check("int8 clip alone still does video", pinned(m.abilities(lane_i8), G["abilities_int8_clip"]), G["abilities_int8_clip"])
check("int8 clip alone misses nothing",   m.missing_for(lane_i8, "video"), G["missing_video_int8"])
check("a turbo LoRA alone is not video",  pinned(m.abilities(lane_tb), G["abilities_turbo_only"]), G["abilities_turbo_only"])

print("the audio pack's abilities are present and correct with no audio models")
audio_want = {"song": False, "music": False, "sfx": False, "yue2": False, "cover": False, "audio": False}
check("audio abilities false with no audio models", pinned(m.abilities(lane), audio_want), audio_want)

print("the core no longer names any model")
import re
src = open(os.path.join(ROOT, "server.py")).read()
hits = sorted({w.lower() for w in re.findall(
    r"qwen|minimax|h3_[a-z_]*|ace.step|\byue\b|trellis|sdxl", src, re.I)})
print("     vendor tokens still in server.py: %d %s" % (len(hits), hits[:8]))
check("server.py names no model", hits, [])

print("apply_quality: the caller's explicit value wins; the tier only fills what's missing")
tiers = m.engines.quality("image", "t2i")
tier = next(t for t in tiers if t.get("values"))
k = next(iter(tier["values"]))
body = dict(tier["values"]); body["quality"] = tier["id"]; body[k] = 123
p_explicit, err_explicit = m.apply_quality(dict(body), "image", "t2i", {})
p_bare, err_bare = m.apply_quality({kk: vv for kk, vv in body.items() if kk != k}, "image", "t2i", {})
check("apply_quality keeps the caller's explicit %s" % k, (err_explicit, p_explicit.get(k)), (None, 123))
check("apply_quality fills a missing %s with the tier's" % k, (err_bare, p_bare.get(k)), (None, tier["values"][k]))

# --- E4: a job the lane forgot becomes "interrupted", not rendering forever ---
# A real incident (2026-09-23): ComfyUI was restarted mid-render, the new
# instance has no history for the prompt and it is in no queue, and the job
# sat on "running" until the 6-hour cutoff. The poller must mark it lost after
# two consecutive clean misses -- and must NOT, when the lane is down, when
# /queue failed, or when the prompt is still live in the queue.
import socket, subprocess, threading, time, urllib.request

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close()
    return p

fake_port = _free_port()
fake = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
     "--port", str(fake_port)],
    cwd=os.path.dirname(HERE), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _fake_get(path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (fake_port, path), timeout=1) as r:
        return json.loads(r.read().decode("utf-8"))


def _fake_post(path, obj):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (fake_port, path),
                                 data=json.dumps(obj).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.loads(r.read().decode("utf-8"))


deadline = time.time() + 15
while True:
    try:
        _fake_get("/system_stats"); break
    except Exception:
        if time.time() > deadline:
            fake.kill()
            print("  (fake lane never came up)")
            sys.exit(1)
        time.sleep(0.2)


def _f_lane(lid, port=fake_port, up_port=None):
    l = dict(lane); l["id"] = lid; l["port"] = up_port if up_port is not None else port
    m.LANE_BY_ID[lid] = l
    if l not in m.LANES: m.LANES.append(l)
    return l


def _f_job(jid, lid, pid, started_ago=3600.0):
    j = {"id": jid, "lane": lid, "kind": "video", "status": "running",
         "prompt_id": pid, "started": time.time() - started_ago, "step": 6, "total": 8}
    with m.JOBS_LOCK:
        m.JOBS[jid] = j; m.JOB_ORDER.append(jid)
    return j


# lane 1: up, clean /queue read, empty queue, no history for the prompt -> LOST
l1 = _f_lane("e4-lost"); m.poll_lane_once(l1)
# lane 2: up, clean /queue read, the prompt IS live in queue_running -> running
_fake_post("/_control/queue", {"running": [[0, "e4-keep-pid"]]})
l2 = _f_lane("e4-keep"); m.poll_lane_once(l2)
# lane 3: down (port 1, nothing there) -> CANNOT TELL, never lost
l3 = _f_lane("e4-down", up_port=1); m.poll_lane_once(l3)
# lane 4: up, but the /queue read failed -> no queue_checked -> never lost
l4 = _f_lane("e4-qf")
m.LANE_STATE["e4-qf"] = {"up": True, "checked": time.time(), "live_ids": [], "err": ""}

_j_lost = _f_job("e4-lost-j", "e4-lost", "e4-ghost-pid")
_j_keep = _f_job("e4-keep-j", "e4-keep", "e4-keep-pid")
_j_down = _f_job("e4-down-j", "e4-down", "e4-down-pid")
_j_qf = _f_job("e4-qf-j", "e4-qf", "e4-qf-pid")

m.JOB_POLL_SECONDS = 0.3   # the loop reads this global every pass
threading.Thread(target=m.job_poller, daemon=True).start()
time.sleep(3.0)   # ten poll periods: two consecutive misses is plenty

check("a prompt the lane forgot is interrupted",
      (m.JOBS["e4-lost-j"]["status"], m.JOBS["e4-lost-j"].get("error")),
      ("interrupted", "The machine restarted and lost this one."))
check("a prompt still in the lane's queue keeps running",
      m.JOBS["e4-keep-j"]["status"], "running")
check("a job on a down lane is never marked lost",
      (m.JOBS["e4-down-j"]["status"], m.JOBS["e4-down-j"].get("error")), ("running", None))
check("a failed /queue read never marks a job lost",
      (m.JOBS["e4-qf-j"]["status"], m.JOBS["e4-qf-j"].get("error")), ("running", None))
fake.kill()

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
