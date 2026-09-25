"""Acceptance gate for C3.2a -- carry(), references, and pack limits.
(the internal sequence/storyboard design spec, §8 row C3.2; contract in
internal implementation notes.)

ISOLATION: every byte this test writes goes under a fresh tempfile.mkdtemp()
directory, and each server module loaded gets its own scratch data dir (JOBS_FILE,
SEQ_DIR, SEQ_MEDIA_DIR, CHAIN_DIR, LOCAL_OUTPUTS_DIR all overridden after import,
the same way tests/test_local_outputs.py and tests/test_sequences.py do it). The
real data/ tree is never touched.

Run: python3 tests/test_refs.py
"""
import importlib.util, json, os, shutil, socket, subprocess, sys, tempfile, time
import urllib.request
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import engines

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

# Finding #21: Pillow is an optional dep of the app itself -- its absence on
# a clean clone is a SKIP, not a failure. Every section below builds on one
# Pillow-made source PNG, so the whole suite skips rather than a section.
try:
    from PIL import Image
except ImportError:
    print("SKIP: Pillow is not installed -- this suite needs it")
    sys.exit(0)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port

SCRATCH = tempfile.mkdtemp(prefix="bwf_refs_")
STORE = os.path.join(SCRATCH, "fake_lane_store")
os.makedirs(os.path.join(STORE, "outputs"))
print("scratch dir: %s (real data/ is never written)" % SCRATCH)

FAKE_PORT = free_port()
fake = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
     "--port", str(FAKE_PORT), "--store", STORE],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % FAKE_PORT, timeout=0.5)
        break
    except Exception:
        time.sleep(0.05)
else:
    raise SystemExit("fake lane did not come up")

CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "c32a test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": FAKE_PORT,
                      "caps": ["image", "video"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG


def load_server(path, data_dir, name):
    os.environ["GENCENTER_DATA"] = data_dir
    spec = importlib.util.spec_from_file_location("srv_refs_%s" % name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.JOBS_FILE = os.path.join(data_dir, "jobs.json")
    mod.SEQ_DIR = os.path.join(data_dir, "sequences")
    mod.SEQ_MEDIA_DIR = os.path.join(data_dir, "seq")
    mod.CHAIN_DIR = os.path.join(data_dir, "chain")
    mod.LOCAL_OUTPUTS_DIR = os.path.join(data_dir, "outputs")
    for d in (mod.SEQ_DIR, mod.SEQ_MEDIA_DIR, mod.CHAIN_DIR, mod.LOCAL_OUTPUTS_DIR):
        os.makedirs(d, exist_ok=True)
    with mod.STATE_LOCK:
        mod.LANE_STATE["t"] = {"up": True, "checked": time.time(), "err": ""}
    return mod


def call_chain(mod, body):
    h = mod.Handler.__new__(mod.Handler)
    h.read_json = lambda: body
    out = []
    h.send_json = lambda payload, code=200: out.append((payload, code))
    mod.Handler.api_chain(h)
    return out[-1]


def op(mod, sid, rev, name, **kw):
    return mod.seq_op(dict(kw, id=sid, rev=rev, op=name))


def log_lines():
    path = os.path.join(STORE, "requests.log")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


# ---------------------------------------------------------------------------
print("api_chain response is byte-identical before/after the carry() split")

from PIL import Image
SRC_PATH = os.path.join(STORE, "outputs", "src.png")
Image.new("RGB", (64, 64), (10, 20, 30)).save(SRC_PATH)

# The old-vs-new comparison reads the committed server.py with `git show`;
# a tree with no git history (a ZIP or archive download) skips just that part.
_old = subprocess.run(["git", "show", "HEAD:server.py"], cwd=ROOT, capture_output=True, text=True)
if _old.returncode != 0:
    print("  SKIP  old-vs-new comparison: `git show HEAD:server.py` failed (%s) -- this tree has no git "
          "history, e.g. a ZIP download" % ((_old.stderr or "").strip().splitlines() or ["no output"])[-1])
    old_srv = None
else:
    OLD_SERVER_PATH = os.path.join(SCRATCH, "old_server.py")
    with open(OLD_SERVER_PATH, "w") as f:
        f.write(_old.stdout)
    old_srv = load_server(OLD_SERVER_PATH, os.path.join(SCRATCH, "data_old"), "old")
new_srv = load_server(os.path.join(ROOT, "server.py"), os.path.join(SCRATCH, "data_new"), "new")

CHAIN_JOB = {"id": "chaina1", "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
             "prompt": "a tree", "seed": 42,
             "outputs": [{"filename": "src.png", "subfolder": "", "type": "output", "media": "image"}]}
new_srv.JOBS[CHAIN_JOB["id"]] = dict(CHAIN_JOB)

CHAIN_BODY = {"job_id": CHAIN_JOB["id"], "target_lane": "t", "output_index": 0,
              "video_width": 960, "video_height": 544}
if old_srv is not None:
    old_srv.JOBS[CHAIN_JOB["id"]] = dict(CHAIN_JOB)
    before = call_chain(old_srv, CHAIN_BODY)
    after = call_chain(new_srv, CHAIN_BODY)
    check("both calls succeeded (200)", before[1] == 200 and after[1] == 200, (before, after))
    check("api_chain JSON response is byte-identical old vs new", before == after, (before, after))


# ---------------------------------------------------------------------------
print("carry(fit=None) uploads the source pixels unchanged")

SRC_B_BYTES = b"uncropped-ref-bytes-0001"
with open(os.path.join(STORE, "outputs", "srcB.png"), "wb") as f:
    f.write(SRC_B_BYTES)
JOB_B = {"id": "chainb1", "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
         "outputs": [{"filename": "srcB.png", "subfolder": "", "type": "output", "media": "image"}]}
new_srv.JOBS[JOB_B["id"]] = dict(JOB_B)

name, note = new_srv.carry(new_srv.JOBS[JOB_B["id"]], 0, new_srv.LANE_BY_ID["t"], fit=None)
with open(os.path.join(STORE, "inputs", os.path.basename(name)), "rb") as f:
    uploaded = f.read()
check("carry(fit=None) note says uncropped", note == "sent uncropped", note)
check("carry(fit=None) uploads the exact source bytes", uploaded == SRC_B_BYTES, uploaded)


# ---------------------------------------------------------------------------
print("carry() of a type \"local\" output reads the local store, never the lane's /view")

LOCAL_BYTES = b"local-post-step-output-bytes"
local_dir = os.path.join(new_srv.LOCAL_OUTPUTS_DIR, "localjob1")
os.makedirs(local_dir, exist_ok=True)
with open(os.path.join(local_dir, "local.png"), "wb") as f:
    f.write(LOCAL_BYTES)
JOB_LOCAL = {"id": "localjob1", "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
             "outputs": [{"filename": "local.png", "subfolder": "localjob1", "type": "local", "media": "image"}]}
new_srv.JOBS[JOB_LOCAL["id"]] = dict(JOB_LOCAL)

before_n = len(log_lines())
name, note = new_srv.carry(new_srv.JOBS[JOB_LOCAL["id"]], 0, new_srv.LANE_BY_ID["t"], fit=None)
after_lines = log_lines()[before_n:]
with open(os.path.join(STORE, "inputs", os.path.basename(name)), "rb") as f:
    uploaded = f.read()
check("a local-type output's carry() never hits the lane's /view",
      not any(e["path"] == "/view" for e in after_lines), after_lines)
check("a local-type output's carry() uploads the local store's bytes", uploaded == LOCAL_BYTES, uploaded)


# ---------------------------------------------------------------------------
print("add_ref, move_ref, remove_ref, and a stale rev")

body, code = new_srv.seq_create({"title": "Till Delete", "mode": "sequence"})
sid, rev = body["id"], body["rev"]

b, c = op(new_srv, sid, rev, "add_ref", job_id=CHAIN_JOB["id"], output=0, role="set")
check("add_ref (set) ok, r1 first", c == 200 and b["refs"][0]["id"] == "r1" and b["refs"][0]["role"] == "set", b)
rev = b["rev"]
r1_path = new_srv.seq_ref_path(sid, "r1")
with open(SRC_PATH, "rb") as f:
    src_bytes = f.read()
with open(r1_path, "rb") as f:
    r1_bytes = f.read()
check("add_ref copied the bytes to data/seq/<id>/refs/r1.png", r1_bytes == src_bytes)

b, c = op(new_srv, sid, rev, "add_ref", job_id=JOB_B["id"], output=0, role="character")
check("add_ref (character) ok, r2 appended after the set", c == 200 and
      [r["id"] for r in b["refs"]] == ["r1", "r2"], b)
rev = b["rev"]

b, c = op(new_srv, sid, rev, "add_ref", job_id=CHAIN_JOB["id"], output=0, role="set")
check("a second set is refused",
      c == 400 and b["error"] == "The room already has a set. Remove it first.", b)

b, c = op(new_srv, sid, rev, "move_ref", ref_id="r2", to=0)
check("move_ref cannot put a ref ahead of the set",
      c == 400 and b["error"] == "Nothing can go ahead of the set.", b)

b, c = op(new_srv, sid, rev, "move_ref", ref_id="r1", to=1)
check("move_ref refuses to move the set itself",
      c == 400 and b["error"] == "The set stays first. It cannot be moved.", b)

b, c = op(new_srv, sid, rev, "remove_ref", ref_id="r2")
check("remove_ref works", c == 200 and [r["id"] for r in b["refs"]] == ["r1"], b)
rev = b["rev"]

b, c = op(new_srv, sid, rev - 1, "remove_ref", ref_id="r1")
check("a stale rev -> 409", c == 409 and b["sequence"]["rev"] == rev, b)


# ---------------------------------------------------------------------------
print("a ref's cache_path still works once its source lane is down")
fake.terminate()
fake.wait(timeout=10)

data = new_srv._carry_source_bytes(new_srv.JOBS[CHAIN_JOB["id"]],
                                    CHAIN_JOB["outputs"][0], cache_path=r1_path)
check("the cached ref's bytes still resolve with the source lane down", data == src_bytes)


# ---------------------------------------------------------------------------
print("add_ref pins a job outside the newest 200 immediately (save_jobs after SEQ_LOCK release)")

OLD_JOB_ID = "pin00000"
with new_srv.JOBS_LOCK:
    new_srv.JOBS.clear()
    new_srv.JOB_ORDER.clear()
    for i in range(205):
        jid = "pin%05d" % i
        new_srv.JOBS[jid] = {"id": jid, "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
                              "outputs": [{"filename": "p.png", "subfolder": jid, "type": "local", "media": "image"}]}
        new_srv.JOB_ORDER.append(jid)
old_dir = os.path.join(new_srv.LOCAL_OUTPUTS_DIR, OLD_JOB_ID)
os.makedirs(old_dir, exist_ok=True)
with open(os.path.join(old_dir, "p.png"), "wb") as f:
    f.write(b"pinned-job-bytes")
check("sanity: the old job is outside the newest 200",
      OLD_JOB_ID not in list(new_srv.JOB_ORDER)[-200:])

body2, code2 = new_srv.seq_create({"title": "Pin Test", "mode": "sequence"})
sid2, rev2 = body2["id"], body2["rev"]
b, c = op(new_srv, sid2, rev2, "add_ref", job_id=OLD_JOB_ID, output=0, role="other")
check("add_ref of a job outside the newest 200 succeeds", c == 200, b)

with open(new_srv.JOBS_FILE) as f:
    on_disk = {j["id"] for j in json.load(f)}
check("the referenced job is pinned on disk WITHOUT any other save_jobs() call",
      OLD_JOB_ID in on_disk, sorted(on_disk)[:5])


# ---------------------------------------------------------------------------
print("add_ref refuses a job that is not done, or an output that is audio")

JOB_RUNNING = {"id": "running1", "lane": "t", "kind": "image", "mode": "t2i", "status": "running",
               "outputs": []}
new_srv.JOBS[JOB_RUNNING["id"]] = dict(JOB_RUNNING)
b, c = op(new_srv, sid, rev, "add_ref", job_id=JOB_RUNNING["id"], output=0, role="other")
check("a job that has not finished is refused with a sentence",
      c == 400 and isinstance(b.get("error"), str) and b["error"], b)

JOB_AUDIO = {"id": "audio1", "lane": "t", "kind": "audio", "mode": "song", "status": "done",
             "outputs": [{"filename": "a.wav", "subfolder": "", "type": "output", "media": "audio"}]}
new_srv.JOBS[JOB_AUDIO["id"]] = dict(JOB_AUDIO)
b, c = op(new_srv, sid, rev, "add_ref", job_id=JOB_AUDIO["id"], output=0, role="other")
check("an audio output is refused with a sentence",
      c == 400 and isinstance(b.get("error"), str) and b["error"], b)


# ---------------------------------------------------------------------------
print("pack limits and ref_role (finding 4)")

ref_images_v = next(f for f in engines.fields("video", "ref2v") if f["id"] == "ref_images")
check("H3 ref2v's ref_images declares max 9", ref_images_v.get("max") == 9, ref_images_v)

ref_images_i = next(f for f in engines.fields("image", "edit") if f["id"] == "ref_images")
check("Qwen edit's ref_images declares max 10", ref_images_i.get("max") == 10, ref_images_i)

t2i_presets = {p["id"]: p for p in engines.presets("image", "t2i")}
check("set-plate preset carries ref_role: set",
      t2i_presets.get("set-plate", {}).get("ref_role") == "set", t2i_presets.get("set-plate"))
check("character-anchor preset carries ref_role: character",
      t2i_presets.get("character-anchor", {}).get("ref_role") == "character",
      t2i_presets.get("character-anchor"))


shutil.rmtree(SCRATCH, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
