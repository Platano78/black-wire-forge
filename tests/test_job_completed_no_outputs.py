"""Acceptance gate for F2 (small-fixes recipe): a job whose prompt COMPLETED
but produced nothing collect_outputs() recognizes as real output (e.g. only
a LoadVideo `type: "input"` echo, no SaveX result) must be marked "error",
not silently "done" with an empty outputs list -- a job with no output is a
recipe missing its save step, not a finished one.

RED on the pre-fix tree: job_poller falls through to the "else" branch,
marking the job "done" with outputs == [].

Run: python3 tests/test_job_completed_no_outputs.py
"""
import importlib.util, json, os, socket, subprocess, sys, threading, time, urllib.request
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv_no_outputs", os.path.join(ROOT, "server.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


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

try:
    lane = {"id": "f2", "name": "F2", "box": "b", "note": "", "host": "127.0.0.1",
            "port": fake_port, "caps": ["video"], "models": {}}
    m.LANE_BY_ID[lane["id"]] = lane
    if lane not in m.LANES: m.LANES.append(lane)

    # This lane only ever echoes a LoadVideo input, tagged type: "input" --
    # never a real SaveX "output". collect_outputs() must see nothing.
    _fake_post("/_control/accept", {"outputs": [
        {"filename": "CONT_00002_.mp4", "subfolder": "", "type": "input", "node": "220"}]})
    resp = _fake_post("/prompt", {"prompt": {}})
    pid = resp["prompt_id"]

    jid = "f2-job"
    job = {"id": jid, "lane": lane["id"], "kind": "video", "status": "running",
           "prompt_id": pid, "started": time.time() - 5.0, "step": 8, "total": 8}
    with m.JOBS_LOCK:
        m.JOBS[jid] = job; m.JOB_ORDER.append(jid)

    m.JOB_POLL_SECONDS = 0.3
    threading.Thread(target=m.job_poller, daemon=True).start()
    time.sleep(2.0)

    print("a job that completed with only an input echo (no real output) is an error")
    got_status = m.JOBS[jid]["status"]
    got_error = m.JOBS[jid].get("error")
    check("status is error, not done", got_status == "error", got_status)
    check("error names the missing save step",
          got_error == "The machine finished but saved nothing — the recipe may be missing its save step.",
          repr(got_error))
    check("outputs never got set", "outputs" not in m.JOBS[jid] or m.JOBS[jid]["outputs"] in (None, []), repr(m.JOBS[jid].get("outputs")))
finally:
    fake.kill()

print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All F2 checks passed.")
