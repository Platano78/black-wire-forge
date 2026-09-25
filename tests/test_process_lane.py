"""Process-lane gate (slice B1b) -- the internal Blender process-lane design doc.

A process lane runs LOCAL programs via runner.py instead of talking to
ComfyUI over HTTP. Everything here runs against:

  - a synthetic engine pack (the test_rooms.py pattern: it is appended to
    engines._PACKS and never touches a real pack),
  - a scratch GENCENTER_CONFIG / GENCENTER_DATA (mkdtemp), set BEFORE
    server.py is imported, so the module under test never sees the real
    config or the real data/ directory,
  - the real HTTP API, served in-process on a free 127.0.0.1 port.

Run:  python3 tests/test_process_lane.py
Exit 0 = all good. No third-party imports.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


def sha256_of(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


REAL_JOBS = os.path.join(ROOT, "data", "jobs.json")
REAL_JOBS_SHA = sha256_of(REAL_JOBS)

# ---------------------------------------------------------------------------
# Scratch env BEFORE importing server.py (the test_server_behaviour.py pattern)
# ---------------------------------------------------------------------------
SCRATCH = tempfile.mkdtemp(prefix="bwf_b1b_")
DATA = os.path.join(SCRATCH, "data")
os.makedirs(DATA)
CONFIG = os.path.join(SCRATCH, "config.json")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# One process lane (no host/port at all) and one comfy lane (no kind key).
CONFIG_LANES = [
    {"id": "proc", "name": "Proc box", "kind": "process",
     "caps": ["3d"], "box": "second-box"},
    {"id": "c1", "name": "Comfy box", "host": "127.0.0.1",
     "port": free_port(), "caps": ["3d"]},
]
API_PORT = free_port()   # the HTTP server below binds THIS port: the B1 guard
                         # checks the Host header against the config port, the
                         # same port the real app listens on.
with open(CONFIG, "w") as f:
    json.dump({"port": API_PORT, "bind": "127.0.0.1", "title": "b1b",
               "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
               "lanes": CONFIG_LANES}, f)

os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = DATA

import engines  # noqa: E402

_spec = importlib.util.spec_from_file_location("srv_b1b", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)

# ---------------------------------------------------------------------------
# Synthetic process pack. The program it "runs" is tests/fixtures/stub_prog.py.
# ---------------------------------------------------------------------------
STUB = os.path.join(ROOT, "tests", "fixtures", "stub_prog.py")
PROG = "PROGRESS (\\d+)/(\\d+)"
SYNTH = {
    "id": "zz-proctest", "cap": "3d", "lane_kind": "process",
    "roles": {},
    "provides": {"zzstub": ["py"]},      # the mode is able when its bin resolved
    "words": {"py": "the stub program"},
    "bins": {"py": "python3"},
    "fields": {"zzstub": [{"id": "src", "label": "Input", "type": "image"}],
               "churn": [], "fail": []},
    "graphs": {
        # 3 progress lines, then a short pause so progress is observable,
        # writes the declared output, echoes every argv item into the tail.
        "zzstub": lambda a, m: {
            "steps": [{"argv": ["{bin:py}", STUB, "--steps", "3",
                                "--write", "{job}/result.mp4",
                                "--sleep", "1.5", "--echo-args"], "timeout_s": 60}],
            "outputs": ["result.mp4"], "progress": PROG},
        # spawns a grandchild (sleep 300) and itself sleeps: the cancel
        # test checks the whole group is dead afterwards.
        "churn": lambda a, m: {
            "steps": [{"argv": ["{bin:py}", STUB, "--spawn-child"], "timeout_s": 600}],
            "outputs": ["result.mp4"], "progress": PROG},
        # exits 3 after writing its output: the failure test.
        "fail": lambda a, m: {
            "steps": [{"argv": ["{bin:py}", STUB, "--exit", "3",
                                "--write", "{job}/result.mp4"], "timeout_s": 60}],
            "outputs": ["result.mp4"], "progress": PROG},
    },
}
# The real turntable pack is shelved for this test: it is a 3d process pack
# whose bins (blender, ffmpeg) are not installed in the gate environment, and
# discovery would take this lane offline for them. It is the lane MECHANISM
# under test here; the real pack's own contract is test_turntable_pack.py's.
engines._PACKS = [p for p in engines._PACKS if p["id"] != "turntable"] + [SYNTH]

# ---------------------------------------------------------------------------
# In-process API server + lane worker
# ---------------------------------------------------------------------------
HTTPD = ThreadingHTTPServer(("127.0.0.1", API_PORT), srv.Handler)
PORT = HTTPD.server_address[1]
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()
threading.Thread(target=srv.process_worker, args=(srv.LANE_BY_ID["proc"],), daemon=True).start()


def http(path, body=None, ctype="application/json"):
    """(status, parsed-json-or-bytes) for a request to the in-process API."""
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                               data=data,
                               headers={"Content-Type": ctype} if data else {})
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read()
            is_view = path.split("?")[0] == "/api/view"
            return resp.status, (raw if is_view else json.loads(raw))
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def multipart(fields, files):
    b = "b1btestboundary"
    body = b""
    for k, v in fields:
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (b, k, v)).encode()
    for name, fn, data in files:
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n" % (b, name, fn)).encode()
        body += data + b"\r\n"
    body += ("--%s--\r\n" % b).encode()
    return body, "multipart/form-data; boundary=" + b


def lanes_snapshot():
    _, p = http("/api/lanes")
    return {l["id"]: l for l in p["lanes"]}


def job(jid):
    with srv.JOBS_LOCK:
        return json.loads(json.dumps(srv.JOBS.get(jid) or {}))


def wait_status(jid, want, timeout=20.0, sample=None):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = job(jid)
        if sample is not None:
            sample(j)
        if j.get("status") in want:
            return j
        time.sleep(0.1)
    return job(jid)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------------------
# 1. config: the process lane is accepted, no ComfyUI endpoint, up + able
# ---------------------------------------------------------------------------
proc = srv.LANE_BY_ID["proc"]
check("process lane loaded from config", proc["kind"] == "process")
check("process lane gets no ComfyUI endpoint",
      proc.get("host") == "" and int(proc.get("port", 0)) == 0,
      repr((proc.get("host"), proc.get("port"))))
check("comfy lane in the same config is still comfy",
      srv.LANE_BY_ID["c1"]["kind"] == "comfy")
t0 = time.time()
while time.time() - t0 < 10 and not (srv.LANE_STATE.get("proc") or {}).get("up"):
    time.sleep(0.1)
check("process lane is up after the worker's first poll",
      bool((srv.LANE_STATE.get("proc") or {}).get("up")))

snap = lanes_snapshot()
pl = snap.get("proc", {})
check("lanes endpoint reports kind=process", pl.get("kind") == "process")
check("lanes endpoint: process lane has no endpoint string", pl.get("endpoint") == "")
check("process lane is able for its cap", bool((pl.get("able") or {}).get("3d")))
load = pl.get("load")
check("process lane reports a numeric load >= 0",
      isinstance(load, (int, float)) and not isinstance(load, bool) and load >= 0,
      repr(load))
cores = pl.get("cores")
check("process lane reports an integer cores >= 1",
      isinstance(cores, int) and not isinstance(cores, bool) and cores >= 1,
      repr(cores))
check("comfy lane still shows its endpoint",
      snap.get("c1", {}).get("endpoint", "").endswith(":%d" % CONFIG_LANES[1]["port"]))

# ---------------------------------------------------------------------------
# 2. missing bin -> lane down with a plain, actionable note
# ---------------------------------------------------------------------------
SYNTH["bins"] = {"py": "definitely-not-installed-b1b"}
srv.poll_process_lane(srv.LANE_BY_ID["proc"])
st = dict(srv.LANE_STATE.get("proc") or {})
check("missing bin takes the lane down", st.get("up") is False, repr(st.get("err")))
check("missing-bin note is plain and actionable",
      "the stub program" in (st.get("err") or "") and "installed" in (st.get("err") or ""),
      repr(st.get("err")))
SYNTH["bins"] = {"py": "python3"}
srv.poll_process_lane(srv.LANE_BY_ID["proc"])
check("lane is up again once the bin resolves",
      bool((srv.LANE_STATE.get("proc") or {}).get("up")))

# ---------------------------------------------------------------------------
# 8. upload to a process lane: stored under uploads/<lane>/; junk names rejected
# ---------------------------------------------------------------------------
body, ctype = multipart([("lane", "proc"), ("kind", "3d"), ("mode", "zzstub")],
                        [("file", "input.png", b"fake picture bytes")])
code, res = http("/api/upload", body, ctype)
check("upload to a process lane is ok", code == 200 and res.get("ok"), repr(res))
up_file = ((res.get("files") or [{}])[0])
up_name = up_file.get("name")
check("stored name is unique-prefixed, safe suffix kept",
      bool(re.fullmatch(r"[0-9a-f]{8}_[A-Za-z0-9._-]+\.png", up_name or "")), repr(up_name))
check("the user's filename is preserved as 'original'",
      up_file.get("original") == "input.png", repr(up_file))
check("uploaded file is under the lane's uploads dir",
      os.path.isfile(os.path.join(srv.UPLOADS_DIR, "proc", up_name)))

# the SAME basename uploaded twice must not clobber the first file
body1, ctype = multipart([("lane", "proc")], [("file", "model.glb", b"first bytes")])
code1, res1 = http("/api/upload", body1, ctype)
body2, ctype = multipart([("lane", "proc")], [("file", "model.glb", b"second-bytes")])
code2, res2 = http("/api/upload", body2, ctype)
n1 = ((res1.get("files") or [{}])[0]).get("name")
n2 = ((res2.get("files") or [{}])[0]).get("name")
check("two uploads of the same name get two different stored names",
      code1 == 200 and code2 == 200 and n1 and n2 and n1 != n2, repr((n1, n2)))
check("both uploaded files exist with their own bytes",
      n1 and n2
      and open(os.path.join(srv.UPLOADS_DIR, "proc", n1), "rb").read() == b"first bytes"
      and open(os.path.join(srv.UPLOADS_DIR, "proc", n2), "rb").read() == b"second-bytes")

# a hostile basename is sanitized down to [A-Za-z0-9._-]
body, ctype = multipart([("lane", "proc")], [("file", "we ird$(id).glb", b"g")])
code, res = http("/api/upload", body, ctype)
up_name2 = ((res.get("files") or [{}])[0]).get("name")
check("hostile filename is stored with only safe characters",
      code == 200 and bool(re.fullmatch(r"[0-9a-f]{8}_[A-Za-z0-9._-]+", up_name2 or "")), repr(up_name2))
check("hostile filename sanitises to a we_ird__id_.glb suffix",
      (up_name2 or "").endswith("we_ird__id_.glb"), repr(up_name2))
check("sanitised upload exists on disk",
      os.path.isfile(os.path.join(srv.UPLOADS_DIR, "proc", up_name2)), repr(up_name2))

body, ctype = multipart([("lane", "proc")],
                        [("file", "evil/../../etc/passwd", b"x")])
code, res = http("/api/upload", body, ctype)
up_name3 = ((res.get("files") or [{}])[0]).get("name")
check("path-separator upload keeps only the (sanitized) basename",
      code == 200 and bool(re.fullmatch(r"[0-9a-f]{8}_passwd", up_name3 or "")), repr(res))
check("no file escaped the uploads dir",
      not os.path.exists(os.path.join(SCRATCH, "evil"))
      and not os.path.exists(os.path.join(SCRATCH, "passwd"))
      and os.path.isfile(os.path.join(srv.UPLOADS_DIR, "proc", up_name3)))

# a bare ".." has no basename at all and must be refused outright
body, ctype = multipart([("lane", "proc")], [("file", "..", b"x")])
code, res = http("/api/upload", body, ctype)
check("bare .. filename is refused", code == 400, repr((code, res)))

# ---------------------------------------------------------------------------
# 3. dispatch via POST /api/generate: queued -> done, output served at /view
# ---------------------------------------------------------------------------
code, res = http("/api/generate",
                 {"lane": "proc", "kind": "3d", "mode": "zzstub",
                  "src": up_name, "seed": 7})
check("generate on a process lane is accepted", code == 200 and res.get("ok"), repr(res))
jid = (res.get("job") or {}).get("id") or ""
check("process job carries no prompt_id", jid and job(jid).get("prompt_id") in (None, ""),
      repr(job(jid).get("prompt_id")))

progress_seen = []


def sample(j):
    if j.get("step"):
        progress_seen.append((j.get("step"), j.get("total")))


j = wait_status(jid, {"done", "error", "interrupted"}, sample=sample)
check("process job finished done", j.get("status") == "done",
      repr((j.get("status"), j.get("notes"))))
outs = j.get("outputs") or []
check("the declared output is listed", bool(outs) and outs[0].get("filename") == "result.mp4",
      repr(outs))
check("output is typed local+video", bool(outs)
      and outs[0].get("type") == "local" and outs[0].get("media") == "video", repr(outs))
# The page's viewURL() sends the job id as `subfolder`, not `job` -- the
# output must carry it, or the finished file can never be shown or downloaded.
check("output's subfolder is the job id (the page's viewURL shape)",
      bool(outs) and outs[0].get("subfolder") == jid, repr(outs))
check("output file exists on disk in the job dir",
      os.path.isfile(os.path.join(srv.LOCAL_OUTPUTS_DIR, jid, "result.mp4")),
      repr(srv.LOCAL_OUTPUTS_DIR))
found_dir = jid
check("the uploaded input was staged into the job dir for the program",
      os.path.isfile(os.path.join(srv.LOCAL_OUTPUTS_DIR, jid, up_name)))

# 3b. a job with no text prompt is titled by its uploaded file's name
body, ctype = multipart([("lane", "proc"), ("kind", "3d"), ("mode", "zzstub")],
                        [("file", "apple.glb", b"fake model bytes")])
code, res = http("/api/upload", body, ctype)
apple = ((res.get("files") or [{}])[0]).get("name")
code, res = http("/api/generate", {"lane": "proc", "kind": "3d", "mode": "zzstub",
                                   "src": apple, "seed": 7})
check("no-prompt dispatch with an uploaded file is accepted", code == 200 and res.get("ok"), repr(res))
tjid = (res.get("job") or {}).get("id") or ""
jt = wait_status(tjid, {"done", "error", "interrupted"})
check("a no-prompt job is titled by its uploaded file's name (prefix stripped)",
      jt.get("title") == "apple.glb", repr((jt.get("title"), jt.get("prompt"))))
check("the title stays out of the prompt (Use-again must not see it)",
      jt.get("prompt") in (None, ""), repr(jt.get("prompt")))

# 3c. an upload name that does not exist is refused at generate time, not later in the job
before = set(srv.JOBS)
code, res = http("/api/generate", {"lane": "proc", "kind": "3d", "mode": "zzstub",
                                   "src": "c0fb74b6_nosuch.glb", "seed": 7})
check("a missing upload -> 400 naming the field, before any job exists",
      code == 400 and res.get("ok") is False and "Input" in (res.get("error") or "")
      and "upload" in (res.get("error") or "").lower() and set(srv.JOBS) == before, repr((code, res)))

# 4. progress: the PROGRESS i/N lines landed on the job while it ran
check("progress step/total was updated from the program's output",
      any(t == 3 and s == 3 for s, t in progress_seen), repr(progress_seen))

# the output is served at /api/view like any other local output
code, raw = http("/api/view?job=%s&filename=result.mp4&type=local" % found_dir)
check("the output is served at /api/view", code == 200 and raw == b"ok",
      repr((code, raw if isinstance(raw, (bytes,)) else raw)[:80]))
# ...and in the page's own shape: subfolder=<job id>, no job param
code2, raw2 = http("/api/view?lane=proc&filename=result.mp4&subfolder=%s&type=local" % found_dir)
check("the output is served at /api/view in the page's shape (no job param)",
      code2 == 200 and raw2 == b"ok",
      repr((code2, raw2 if isinstance(raw2, (bytes,)) else raw2)[:80]))

# ---------------------------------------------------------------------------
# 5. a non-zero exit is a clean error with the tail, not a crash
# ---------------------------------------------------------------------------
code, res = http("/api/generate", {"lane": "proc", "kind": "3d", "mode": "fail"})
fjid = (res.get("job") or {}).get("id") or ""
j = wait_status(fjid, {"done", "error", "interrupted"})
check("failing program -> job status error", j.get("status") == "error", repr(j.get("status")))
check("error message names the exit code",
      "exit code 3" in (j.get("error") or ""), repr((j.get("error"), j.get("notes"))))

# ---------------------------------------------------------------------------
# 6. cancel: the stop signal kills the whole process group (children too)
# ---------------------------------------------------------------------------
code, res = http("/api/generate", {"lane": "proc", "kind": "3d", "mode": "churn"})
cjid = (res.get("job") or {}).get("id") or ""
j = wait_status(cjid, {"running"}, timeout=15)
check("churn job reached running", j.get("status") == "running", repr(j.get("status")))
code, res = http("/api/cancel", {"lane": "proc", "job_id": cjid})
check("cancel is accepted", code == 200 and res.get("ok"), repr(res))
j = wait_status(cjid, {"interrupted", "error", "done"}, timeout=20)
check("canceled job is marked error, not lost", j.get("status") == "error", repr(j.get("status")))
check("canceled job keeps the user-facing stop message",
      j.get("error") == "you stopped this one", repr(j.get("error")))
m = re.search(r"CHILD (\d+)", " ".join(j.get("notes") or []))
check("child pid was recorded in the tail", bool(m), repr(j.get("notes")))
if m:
    child = int(m.group(1))
    t0 = time.time()
    while pid_alive(child) and time.time() - t0 < 5:
        time.sleep(0.1)
    check("the spawned grandchild was killed with the group", not pid_alive(child))

# ---------------------------------------------------------------------------
# 6b. C2: a process-lane-only mode ("zzstub", lane_kind "process"), queried
#     against the COMFY lane in this same config (c1, cap "3d") -- neither
#     "py" role is ever discoverable there, so the OLD code reported "missing"
#     straight from SYNTH's `words` ("the stub program"), indistinguishable
#     from a genuinely-missing model file. No model download fixes a lane
#     kind mismatch; the fix is a config addition instead.
# ---------------------------------------------------------------------------
_, engines_c1 = http("/api/engines?lane=c1")
zzstub_on_comfy = next(m for m in engines_c1["3d"]["modes"] if m["id"] == "zzstub")
check("mismatched-kind mode is not available",
      zzstub_on_comfy["available"] is False, repr(zzstub_on_comfy))
check("mismatched-kind mode's own lane_kind is reported (process)",
      zzstub_on_comfy["lane_kind"] == "process", repr(zzstub_on_comfy["lane_kind"]))
check("mismatched-kind mode does NOT list the stub program as a missing file",
      "the stub program" not in zzstub_on_comfy["missing"], repr(zzstub_on_comfy["missing"]))
check("mismatched-kind mode names the mismatch in one plain sentence",
      len(zzstub_on_comfy["missing"]) == 1
      and "process lane" in zzstub_on_comfy["missing"][0]
      and "config.json" in zzstub_on_comfy["missing"][0],
      repr(zzstub_on_comfy["missing"]))
# Same mode, queried against its OWN (process) lane: ordinary reasoning applies.
_, engines_proc = http("/api/engines?lane=proc")
zzstub_on_proc = next(m for m in engines_proc["3d"]["modes"] if m["id"] == "zzstub")
check("same mode on its OWN lane kind is available (unaffected by the C2 fix)",
      zzstub_on_proc["available"] is True, repr(zzstub_on_proc))

# ---------------------------------------------------------------------------
# 7. restart: a process-lane job that was running becomes interrupted;
#    a comfy-lane job is left mid-flight for the comfy side to resolve
# ---------------------------------------------------------------------------
with open(os.path.join(DATA, "jobs.json"), "w") as f:
    json.dump([
        {"id": "old_proc_1", "lane": "proc", "lane_name": "Proc box", "prompt_id": None,
         "kind": "3d", "mode": "zzstub", "status": "running", "step": 1, "total": 2,
         "created": time.time(), "started": time.time(), "updated": time.time(),
         "outputs": [], "notes": []},
        {"id": "old_comfy_1", "lane": "c1", "lane_name": "Comfy box",
         "prompt_id": "qq-123", "kind": "3d", "mode": "mesh", "status": "running",
         "step": 1, "total": 2, "created": time.time(), "started": time.time(),
         "updated": time.time(), "outputs": [], "notes": []},
    ], f)
srv.load_jobs()
jp = job("old_proc_1")
jc = job("old_comfy_1")
check("restart: running process-lane job is interrupted at once",
      jp.get("status") == "interrupted", repr(jp.get("status")))
check("restart: comfy-lane running job stays mid-flight",
      jc.get("status") == "running", repr(jc.get("status")))

# ---------------------------------------------------------------------------
# Real data untouched
# ---------------------------------------------------------------------------
check("the real data/jobs.json was not touched",
      sha256_of(REAL_JOBS) == REAL_JOBS_SHA)

HTTPD.shutdown()
shutil.rmtree(SCRATCH, ignore_errors=True)

print()
if FAILED:
    print("FAILED: %d of %d checks: %s" % (len(FAILED), len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All process-lane (B1b) checks passed.")
