"""Acceptance gate for POST /api/compare.

Tests the compare sweep: same request across a single axis value.
Run: python3 tests/test_compare.py
"""
import importlib.util
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

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import _scratch_config  # noqa: E402  # sets GENCENTER_CONFIG / GENCENTER_DATA before server.py import

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
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

SCRATCH = tempfile.mkdtemp(prefix="bwf_compare_")
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
    "port": app_port, "bind": "127.0.0.1", "title": "compare test",
    "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
    "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": PORT_LANE,
               "caps": ["image", "video", "audio"]}]
}, open(CFG_PATH, "w"))
os.environ["GENCENTER_CONFIG"] = CFG_PATH
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")
for d in ("data", "data/outputs"):
    os.makedirs(os.path.join(SCRATCH, d), exist_ok=True)

MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
MODELS.update({"ace_unet": "ace.safetensors", "ace_clip1": "a.safetensors",
               "ace_clip2": "b.safetensors", "ace_vae": "v.safetensors"})

spec = importlib.util.spec_from_file_location("srv_compare", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# Give the lane models so generate() considers it able.
srv.LANE_BY_ID["t"]["models"] = dict(MODELS)
with srv.STATE_LOCK:
    srv.LANE_STATE["t"] = {"up": True, "checked": time.time(), "err": ""}


# ---------------------------------------------------------------------------
# Control helpers
# ---------------------------------------------------------------------------

def accept_outputs(outputs):
    """Tell the fake lane to accept /prompt and record outputs."""
    req = urllib.request.Request(
        "http://127.0.0.1:%d/_control/accept" % PORT_LANE,
        data=json.dumps({"outputs": outputs}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


# ---------------------------------------------------------------------------
# Mock handler for calling api_compare
# ---------------------------------------------------------------------------

def call_api_compare(body):
    """Call api_compare with a mock handler, return (payload, code)."""
    h = srv.Handler.__new__(srv.Handler)
    h.read_json = lambda: body
    out = []
    h.send_json = lambda payload, code=200: out.append((payload, code))
    srv.Handler.api_compare(h)
    return out[-1]


# ---------------------------------------------------------------------------
# A minimal valid generate body for image/t2i
# ---------------------------------------------------------------------------

IMAGE_BODY = {"lane": "t", "kind": "image", "mode": "t2i", "prompt": "a tree",
              "seed": 42, "width": 512, "height": 512, "steps": 4, "cfg": 2.5}


# ---------------------------------------------------------------------------
# 1. steps axis with [12, 20, 30] queues 3 jobs, same seed, tagged
# ---------------------------------------------------------------------------
print("\n--- steps axis ---")

accept_outputs([{"filename": "out.png", "subfolder": "", "type": "output"}])
body = dict(IMAGE_BODY)
result, code = call_api_compare({**body, "compare": {"axis": "steps", "values": [12, 20, 30]}})
check("steps axis returns ok", result.get("ok") is True and code == 200,
      "code=%d" % code)
check("steps axis: group is 12 hex chars", len(result.get("group", "")) == 12, result.get("group"))
check("steps axis: axis matches", result.get("axis") == "steps", result.get("axis"))
check("steps axis: 3 jobs queued", len(result.get("jobs", [])) == 3, len(result.get("jobs", [])))

# Check jobs were created with correct steps and same seed
with srv.JOBS_LOCK:
    job_snap = {j["id"]: dict(j) for j in srv.JOBS.values()}
for jinfo in result["jobs"]:
    job = job_snap.get(jinfo["id"])
    if job:
        # Verify steps match the value
        steps_match = job.get("steps") == jinfo["value"]
        check("job %s has correct steps=%d" % (jinfo["id"], jinfo["value"]), steps_match,
              "got steps=%d" % job.get("steps"))
        # Verify compare tag
        cmp = job.get("compare")
        check("job %s tagged with group" % jinfo["id"],
              cmp and cmp["group"] == result["group"],
              str(cmp))
        check("job %s tagged with correct value" % jinfo["id"],
              cmp and cmp["value"] == jinfo["value"],
              str(cmp))
    else:
        check("job %s exists in JOBS" % jinfo["id"], False, "not found")

# Verify all have the same seed (pinned)
seeds = set(job.get("seed") for j in job_snap.values()
            if j.get("compare") and j["compare"]["axis"] == "steps")
check("all steps jobs share the same pinned seed", len(seeds) == 1, seeds)


# ---------------------------------------------------------------------------
# 2. quality axis with real tier ids
# ---------------------------------------------------------------------------
print("\n--- quality axis ---")

# Find real tier ids from engines.quality
tier_ids = [t["id"] for t in srv.engines.quality("image", "t2i")]
if tier_ids:
    accept_outputs([{"filename": "q_out.png", "subfolder": "", "type": "output"}])
    body2 = dict(IMAGE_BODY)
    result2, code2 = call_api_compare({**body2, "compare": {"axis": "quality", "values": tier_ids[:2]}})
    check("quality axis returns ok", result2.get("ok") is True and code2 == 200, "code=%d" % code2)
    check("quality axis: correct jobs", len(result2.get("jobs", [])) == len(tier_ids[:2]),
          len(result2.get("jobs", [])))
    # Verify quality was set on each job
    with srv.JOBS_LOCK:
        for jinfo in result2["jobs"]:
            job = srv.JOBS.get(jinfo["id"])
            if job:
                check("job %s has quality=%s" % (jinfo["id"], jinfo["value"]),
                      job.get("quality") == jinfo["value"],
                      "got quality=%s" % job.get("quality"))
else:
    print("  SKIP  no quality tiers defined for image/t2i")


# ---------------------------------------------------------------------------
# 3. seed axis
# ---------------------------------------------------------------------------
print("\n--- seed axis ---")

accept_outputs([{"filename": "s_out.png", "subfolder": "", "type": "output"}])
body3 = dict(IMAGE_BODY)
seed_vals = [100, 200, 300]
result3, code3 = call_api_compare({**body3, "compare": {"axis": "seed", "values": seed_vals}})
check("seed axis returns ok", result3.get("ok") is True and code3 == 200, "code=%d" % code3)

# Verify each job has its own seed
with srv.JOBS_LOCK:
    for jinfo in result3["jobs"]:
        job = srv.JOBS.get(jinfo["id"])
        if job:
            check("seed job %s has seed=%d" % (jinfo["id"], jinfo["value"]),
                  job.get("seed") == jinfo["value"],
                  "got seed=%s" % job.get("seed"))


# ---------------------------------------------------------------------------
# 4. too few / too many values
# ---------------------------------------------------------------------------
print("\n--- value count validation ---")

body4 = dict(IMAGE_BODY)

# 1 value -> 400
result4a, code4a = call_api_compare({**body4, "compare": {"axis": "steps", "values": [12]}})
check("1 value -> 400", code4a == 400, "code=%d" % code4a)

# 7 values -> 400
result4b, code4b = call_api_compare({**body4, "compare": {"axis": "steps", "values": [1,2,3,4,5,6,7]}})
check("7 values -> 400", code4b == 400, "code=%d" % code4b)


# ---------------------------------------------------------------------------
# 5. unknown axis -> 400
# ---------------------------------------------------------------------------
print("\n--- unknown axis ---")

result5, code5 = call_api_compare({**body4, "compare": {"axis": "unknown_field", "values": [1, 2]}})
check("unknown axis -> 400", code5 == 400, "code=%d" % code5)


# ---------------------------------------------------------------------------
# 6. text field axis (like prompt) -> 400
# ---------------------------------------------------------------------------
print("\n--- text field axis ---")

# "prompt" is not an int/number/select field, so it should be rejected
result6, code6 = call_api_compare({**body4, "compare": {"axis": "prompt", "values": ["a", "b"]}})
check("text field axis -> 400", code6 == 400, "code=%d" % code6)


# ---------------------------------------------------------------------------
# 7. out-of-range values -> 400
# ---------------------------------------------------------------------------
print("\n--- out-of-range values ---")

# Check if any video field has a range defined and test against it.
video_fields = [f for f in srv.engines.fields("video", "fl2va") if f["type"] in ("int", "number") and f.get("range")]
if video_fields:
    f_def = video_fields[0]
    lo, hi = f_def["range"][0], f_def["range"][1]
    bad_val = hi + 1 if hi is not None else 999
    accept_outputs([{"filename": "oor.png", "subfolder": "", "type": "output"}])
    video_body7 = {"lane": "t", "kind": "video", "mode": "fl2va", "prompt": "a scene",
                   "width": 640, "height": 360, "steps": 4, "length": 64}
    # Create a dummy first_frame so generate() doesn't reject
    dummy_frame = os.path.join(SCRATCH, "dummy.png")
    with open(dummy_frame, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")  # minimal PNG header
    result7, code7 = call_api_compare(
        {**video_body7, "first_frame": dummy_frame,
         "compare": {"axis": f_def["id"], "values": [bad_val, 12]}})
    check("out-of-range %s -> 400" % f_def["id"], code7 == 400, "code=%d" % code7)
else:
    print("  SKIP  no int/number fields with range to test")


# ---------------------------------------------------------------------------
# 8. failing generate mid-way returns partial queued list
# ---------------------------------------------------------------------------
print("\n--- mid-way failure ---")

# Make the lane refuse by setting its state to down temporarily
with srv.STATE_LOCK:
    srv.LANE_STATE["t"]["up"] = False

# Still have a valid body
body8 = dict(IMAGE_BODY)
result8, code8 = call_api_compare({**body8, "compare": {"axis": "steps", "values": [12, 20, 30]}})
check("mid-way failure returns error + partial queued", code8 != 200 or not result8.get("ok"),
      "ok=%s code=%d queued=%s" % (result8.get("ok"), code8, result8.get("queued", [])))
check("mid-way failure includes queued list", "queued" in result8,
      "keys=%s" % list(result8.keys()))

# Restore the lane
with srv.STATE_LOCK:
    srv.LANE_STATE["t"]["up"] = True


# ---------------------------------------------------------------------------
# 9. normal /api/generate jobs carry NO compare key
# ---------------------------------------------------------------------------
print("\n--- no compare on normal jobs ---")

accept_outputs([{"filename": "norm.png", "subfolder": "", "type": "output"}])
# Use the module-level generate directly to create a normal job
result9, code9 = srv.generate(IMAGE_BODY)
check("normal generate returns ok", result9.get("ok") is True and code9 == 200,
      "code=%d" % code9)
normal_job_id = result9.get("job", {}).get("id")
if normal_job_id:
    with srv.JOBS_LOCK:
        normal_job = srv.JOBS.get(normal_job_id)
    if normal_job:
        check("normal job has no compare key", "compare" not in normal_job,
              "compare=%s" % normal_job.get("compare"))


# ---------------------------------------------------------------------------
# 10. kind "audio" -> 400
# ---------------------------------------------------------------------------
print("\n--- kind audio -> 400 ---")

result10, code10 = call_api_compare({"lane": "t", "kind": "audio", "mode": "song",
                                      "compare": {"axis": "steps", "values": [12, 20]}})
check("kind audio -> 400", code10 == 400, "code=%d" % code10)


# ---------------------------------------------------------------------------
# 10b. a non-dict "values" in the body must not crash the route (it was a 500)
# ---------------------------------------------------------------------------
print("\n--- non-dict values in body ---")
accept_outputs([{"filename": "out.png", "subfolder": "", "type": "output"}])
try:
    result10b, code10b = call_api_compare({**IMAGE_BODY, "values": ["not", "a", "dict"],
                                           "compare": {"axis": "steps", "values": [12, 20]}})
    check("non-dict values does not crash", code10b != 500, "code=%s" % code10b)
except Exception as e:
    check("non-dict values does not crash", False, "%s: %s" % (type(e).__name__, e))


# ---------------------------------------------------------------------------
# 11. kind "video" with steps axis
# ---------------------------------------------------------------------------
print("\n--- video steps axis ---")

# Create a dummy first_frame so generate() doesn't reject fl2va.
dummy_frame = os.path.join(SCRATCH, "dummy_frame.png")
with open(dummy_frame, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n")  # minimal PNG header

video_body = {"lane": "t", "kind": "video", "mode": "fl2va", "prompt": "a scene",
              "width": 640, "height": 360, "steps": 4, "length": 64, "first_frame": dummy_frame}
accept_outputs([{"filename": "vid_out.png", "subfolder": "", "type": "output"}])
result11, code11 = call_api_compare({**video_body, "compare": {"axis": "steps", "values": [4, 8]}})
check("video steps axis works", result11.get("ok") is True and code11 == 200,
      "code=%d" % code11)
check("video steps axis: 2 jobs", len(result11.get("jobs", [])) == 2,
      len(result11.get("jobs", [])))
# Verify same seed on both
with srv.JOBS_LOCK:
    seeds_vid = set()
    for jinfo in result11["jobs"]:
        job = srv.JOBS.get(jinfo["id"])
        if job:
            seeds_vid.add(job.get("seed"))
            check("video job %s has steps=%d" % (jinfo["id"], jinfo["value"]),
                  job.get("steps") == jinfo["value"],
                  "got steps=%d" % job.get("steps"))
check("video jobs share pinned seed", len(seeds_vid) == 1, seeds_vid)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if FAILED:
    print("FAILED: %s" % ", ".join(FAILED))
    sys.exit(1)
else:
    print("All tests passed.")
    sys.exit(0)
