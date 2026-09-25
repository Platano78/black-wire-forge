"""Acceptance gate for C3.2b -- the generate() refactor, the reference
resolver, and harvest (the internal sequence/storyboard design spec, §8 row
C3.2).

ISOLATION: every byte this test writes goes under a fresh tempfile.mkdtemp()
directory; GENCENTER_DATA/GENCENTER_CONFIG point there before server.py is
ever imported, the same way tests/test_refs.py and tests/test_sequences.py
do it. Two fake ComfyUI lanes run as subprocesses on closed local ports --
one stays up throughout ("t", the target), one is deliberately shut down
mid-test to prove a down SOURCE lane never blocks a generate that only needs
its cache. The real data/ tree is never touched.

Run: python3 tests/test_slot_generate.py
"""
import importlib.util, json, os, shutil, socket, subprocess, sys
import tempfile, threading, time, urllib.request
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

# Finding #21: Pillow is an optional dep of the app itself -- its absence on
# a clean clone is a SKIP, not a failure. Most of this suite (from the
# ref2v section on) builds its own Pillow source images, so the whole
# suite skips rather than a section.
try:
    from PIL import Image as PILImage  # noqa: F401
except ImportError:
    print("SKIP: Pillow is not installed -- this suite needs it")
    sys.exit(0)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


# ---------------------------------------------------------------------------
# Two fake lanes: "t" (target, stays up) and "src" (a ref's own source, taken
# down mid-test). Both are tests/fixtures/fake_comfy.py.
# ---------------------------------------------------------------------------

SCRATCH = tempfile.mkdtemp(prefix="bwf_slotgen_")
print("scratch dir: %s (real data/ is never written)" % SCRATCH)

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

FAKE_T, PORT_T, STORE_T = start_fake("t")
FAKE_SRC, PORT_SRC, STORE_SRC = start_fake("src")

CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "c32b test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
           "lanes": [{"id": "t", "name": "Target lane", "host": "127.0.0.1", "port": PORT_T,
                      "caps": ["image", "video", "audio"]},
                     {"id": "src", "name": "Source lane", "host": "127.0.0.1", "port": PORT_SRC,
                      "caps": ["image"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG

MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
MODELS.update({"ace_unet": "ace.safetensors", "ace_clip1": "a.safetensors",
               "ace_clip2": "b.safetensors", "ace_vae": "v.safetensors"})


def load_server(path, data_dir, name):
    os.environ["GENCENTER_DATA"] = data_dir
    spec = importlib.util.spec_from_file_location("srv_slotgen_%s" % name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.JOBS_FILE = os.path.join(data_dir, "jobs.json")
    mod.SEQ_DIR = os.path.join(data_dir, "sequences")
    mod.SEQ_MEDIA_DIR = os.path.join(data_dir, "seq")
    mod.CHAIN_DIR = os.path.join(data_dir, "chain")
    mod.LOCAL_OUTPUTS_DIR = os.path.join(data_dir, "outputs")
    for d in (mod.SEQ_DIR, mod.SEQ_MEDIA_DIR, mod.CHAIN_DIR, mod.LOCAL_OUTPUTS_DIR):
        os.makedirs(d, exist_ok=True)
    for lid in ("t", "src"):
        mod.LANE_BY_ID[lid]["models"] = dict(MODELS)
        with mod.STATE_LOCK:
            mod.LANE_STATE[lid] = {"up": True, "checked": time.time(), "err": ""}
    return mod


def op(mod, sid, rev, name, **kw):
    return mod.seq_op(dict(kw, id=sid, rev=rev, op=name))


def control(port, outputs):
    """Flip a fake lane's /prompt between reject (default) and accept."""
    req = urllib.request.Request(
        "http://127.0.0.1:%d/_control/accept" % port,
        data=json.dumps({"outputs": outputs}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def last_prompt(store):
    with open(os.path.join(store, "last_prompt.json")) as f:
        return json.load(f)


def log_lines(store):
    path = os.path.join(store, "requests.log")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def call_old_api_generate(mod, body):
    h = mod.Handler.__new__(mod.Handler)
    h.read_json = lambda: body
    out = []
    h.send_json = lambda payload, code=200: out.append((payload, code))
    mod.Handler.api_generate(h)
    return out[-1]


def strip_volatile(obj):
    """A fresh job id/prompt_id/timestamp is inherent to every successful
    dispatch -- "byte-identical" can only mean identical MODULO that
    bookkeeping (test_refs.py's api_chain gate never creates a job, so it
    never hit this)."""
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items()
                if k not in ("id", "prompt_id", "created", "started", "updated", "finished")}
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj


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


# ---------------------------------------------------------------------------
print("generate() byte-identical to the old api_generate, modulo a fresh job's own bookkeeping")

if old_srv is not None:
    REFUSED_BODY = {"lane": "t", "kind": "image", "mode": "t2i"}   # no prompt
    before = call_old_api_generate(old_srv, REFUSED_BODY)
    after = new_srv.generate(REFUSED_BODY)
    check("a refused body (no prompt) is truly byte-identical, old vs new",
          before == after, (before, after))

    IMAGE_BODY = {"lane": "t", "kind": "image", "mode": "t2i", "prompt": "a tree", "seed": 42,
                  "width": 512, "height": 512, "steps": 4, "cfg": 2.5}
    AUDIO_BODY = {"lane": "t", "kind": "audio", "mode": "song", "tags": "upbeat pop", "seed": 7}
    for label, body in (("image t2i", IMAGE_BODY), ("audio song", AUDIO_BODY)):
        control(PORT_T, [{"filename": "out_%s.png" % label.split()[0], "subfolder": "", "type": "output"}])
        before = call_old_api_generate(old_srv, body)
        control(PORT_T, [{"filename": "out_%s.png" % label.split()[0], "subfolder": "", "type": "output"}])
        after = new_srv.generate(body)
        check("%s: same http code" % label, before[1] == after[1] == 200, (before[1], after[1]))
        check("%s: identical modulo id/prompt_id/timestamps" % label,
              strip_volatile(before[0]) == strip_volatile(after[0]), (strip_volatile(before[0]), strip_volatile(after[0])))
    control(PORT_T, None)


# ---------------------------------------------------------------------------
print("a ref2v slot with a set + a character ref: graph order, uncropped upload bytes")

from PIL import Image as PILImage
SET_PATH = os.path.join(STORE_SRC, "outputs", "set_plate.png")
CHAR_PATH = os.path.join(STORE_SRC, "outputs", "character.png")
PILImage.new("RGB", (1328, 1328), (10, 20, 30)).save(SET_PATH)
PILImage.new("RGB", (900, 1200), (200, 100, 50)).save(CHAR_PATH)

SET_JOB = {"id": "setjob01", "lane": "src", "kind": "image", "mode": "t2i", "status": "done",
           "recipe": "set-plate",
           "outputs": [{"filename": "set_plate.png", "subfolder": "", "type": "output", "media": "image"}]}
CHAR_JOB = {"id": "charjob1", "lane": "src", "kind": "image", "mode": "t2i", "status": "done",
            "recipe": "character-anchor",
            "outputs": [{"filename": "character.png", "subfolder": "", "type": "output", "media": "image"}]}
with new_srv.JOBS_LOCK:
    new_srv.JOBS[SET_JOB["id"]] = dict(SET_JOB)
    new_srv.JOBS[CHAR_JOB["id"]] = dict(CHAR_JOB)

seq, code = new_srv.seq_create({"title": "Ref2v shot", "mode": "sequence"})
sid, rev = seq["id"], seq["rev"]
b, c = op(new_srv, sid, rev, "add_ref", job_id=SET_JOB["id"], output=0, role="set")
rev = b["rev"]
b, c = op(new_srv, sid, rev, "add_ref", job_id=CHAR_JOB["id"], output=0, role="character")
rev = b["rev"]
check("both refs present, set first", c == 200 and [r["role"] for r in b["refs"]] == ["set", "character"], b)

b, c = op(new_srv, sid, rev, "add_slot", lane="video", cap="video", mode="ref2v",
          values={"prompt": "a scene", "length": 124})
rev = b["rev"]
slot_id = b["slots"][0]["id"]
check("add_slot ok", c == 200, b)

control(PORT_T, [{"filename": "clip1.mp4", "subfolder": "", "type": "output"}])
before_reqs = len(log_lines(STORE_T))
result, gcode = new_srv.seq_generate({"id": sid, "slot_id": slot_id})
check("seq_generate ok", gcode == 200 and result.get("ok"), result)

graph = last_prompt(STORE_T)
check("graph node 400 (first ref) is the SET plate's upload",
      graph.get("400", {}).get("inputs", {}).get("image") == "set_plate.png", graph.get("400"))
check("graph node 401 (second ref) is the CHARACTER anchor's upload",
      graph.get("401", {}).get("inputs", {}).get("image") == "character.png", graph.get("401"))

with open(os.path.join(STORE_T, "inputs", "set_plate.png"), "rb") as f:
    uploaded_set = f.read()
with open(new_srv.seq_ref_path(sid, "r1"), "rb") as f:
    cached_set = f.read()
check("the uploaded set-plate bytes equal the cached ref file (uncropped)",
      uploaded_set == cached_set, (len(uploaded_set), len(cached_set)))
with open(SET_PATH, "rb") as f:
    original_set = f.read()
check("the cached/uploaded bytes are the ORIGINAL image, not centre-cropped",
      uploaded_set == original_set, (len(uploaded_set), len(original_set)))

seq_after = new_srv.seq_get(sid)[0]
take = seq_after["slots"][0]["takes"][-1]
check("the take records both refs, set first, as '<ref id>:<job id>'",
      take["inputs"]["refs"] == ["r1:%s" % SET_JOB["id"], "r2:%s" % CHAR_JOB["id"]], take)
check("job meta carries sequence_id/slot_id/recipe",
      result["job"]["sequence_id"] == sid and result["job"]["slot_id"] == slot_id, result["job"])
control(PORT_T, None)


# ---------------------------------------------------------------------------
print("11 refs into a max-10 field is refused with the exact sentence; nothing sent to the lane")

seq3, code = new_srv.seq_create({"title": "Over max", "mode": "sequence"})
sid3, rev3 = seq3["id"], seq3["rev"]
# one "set" + ten "other" refs -> 11 total, into image/edit's ref_images (max 10).
jobs11 = []
for i in range(11):
    fn = "extra%02d.png" % i
    PILImage.new("RGB", (64, 64), (i, i, i)).save(os.path.join(STORE_SRC, "outputs", fn))
    jid = "over%03d" % i
    job = {"id": jid, "lane": "src", "kind": "image", "mode": "t2i", "status": "done",
           "outputs": [{"filename": fn, "subfolder": "", "type": "output", "media": "image"}]}
    with new_srv.JOBS_LOCK:
        new_srv.JOBS[jid] = dict(job)
    role = "set" if i == 0 else "other"
    b, c = op(new_srv, sid3, rev3, "add_ref", job_id=jid, output=0, role=role)
    check("add_ref #%d ok" % i, c == 200, b)
    rev3 = b["rev"]
    jobs11.append(jid)
check("sanity: 11 refs in the room", len(b["refs"]) == 11, len(b["refs"]))

b, c = op(new_srv, sid3, rev3, "add_slot", lane="picture", cap="image", mode="edit", values={})
rev3 = b["rev"]
slot3 = b["slots"][0]["id"]

before_t_reqs = len(log_lines(STORE_T))
result3, gcode3 = new_srv.seq_generate({"id": sid3, "slot_id": slot3})
check("over-max refused with the exact sentence, 400",
      gcode3 == 400 and result3.get("error") ==
      "The room holds 11 references; this recipe takes 10. Turn one off for this shot.", result3)
check("nothing was sent to the target lane", len(log_lines(STORE_T)) == before_t_reqs,
      log_lines(STORE_T)[before_t_reqs:])


# ---------------------------------------------------------------------------
print("source lane of a ref down -> the slot still generates from the cache")

seq4, code = new_srv.seq_create({"title": "Down source", "mode": "sequence"})
sid4, rev4 = seq4["id"], seq4["rev"]
DOWN_JOB = {"id": "downjob1", "lane": "src", "kind": "image", "mode": "t2i", "status": "done",
            "outputs": [{"filename": "set_plate.png", "subfolder": "", "type": "output", "media": "image"}]}
with new_srv.JOBS_LOCK:
    new_srv.JOBS[DOWN_JOB["id"]] = dict(DOWN_JOB)
b, c = op(new_srv, sid4, rev4, "add_ref", job_id=DOWN_JOB["id"], output=0, role="set")
check("add_ref (cache written while the source lane is still up)", c == 200, b)
rev4 = b["rev"]
b, c = op(new_srv, sid4, rev4, "add_slot", lane="video", cap="video", mode="ref2v",
          values={"prompt": "a scene", "length": 124})
rev4 = b["rev"]
slot4 = b["slots"][0]["id"]

# Take the SOURCE lane down -- the fake process is killed outright (not just
# LANE_STATE flipped), so any code path that still tried to reach it would
# get a connection refused, not a mocked "down" flag.
FAKE_SRC.terminate()
FAKE_SRC.wait(timeout=10)
with new_srv.STATE_LOCK:
    new_srv.LANE_STATE["src"]["up"] = False

control(PORT_T, [{"filename": "clip2.mp4", "subfolder": "", "type": "output"}])
result4, gcode4 = new_srv.seq_generate({"id": sid4, "slot_id": slot4})
check("the slot still generates with its source lane down (cache-backed)",
      gcode4 == 200 and result4.get("ok"), result4)
control(PORT_T, None)


# ---------------------------------------------------------------------------
print("harvest: a finished job with sequence_id is copied to data/seq/<id>/takes/")

seq5, code = new_srv.seq_create({"title": "Harvest", "mode": "sequence"})
sid5, rev5 = seq5["id"], seq5["rev"]
b, c = op(new_srv, sid5, rev5, "add_slot", lane="video", cap="video", mode="ref2v",
          values={"prompt": "a scene", "length": 124})
rev5 = b["rev"]
slot5 = b["slots"][0]["id"]

CLIP_BYTES = b"pretend-mp4-bytes-0001"
with open(os.path.join(STORE_T, "outputs", "harvest_ok.mp4"), "wb") as f:
    f.write(CLIP_BYTES)
OK_JOB = {"id": "harvok001", "lane": "t", "kind": "video", "status": "done",
          "sequence_id": sid5, "slot_id": slot5,
          "outputs": [{"filename": "harvest_ok.mp4", "subfolder": "", "type": "output", "media": "video"}]}
with new_srv.JOBS_LOCK:
    new_srv.JOBS[OK_JOB["id"]] = dict(OK_JOB)
new_srv.seq_add_take(sid5, slot5, OK_JOB["id"])
new_srv.seq_harvest(new_srv.LANE_BY_ID["t"], new_srv.JOBS[OK_JOB["id"]])
after5 = new_srv.seq_get(sid5)[0]
take_ok = next(t for t in after5["slots"][0]["takes"] if t["job_id"] == OK_JOB["id"])
check("the take's file is set", take_ok["file"] == "takes/%s.mp4" % OK_JOB["id"], take_ok)
harvested_path = os.path.join(new_srv.SEQ_MEDIA_DIR, sid5, "takes", "%s.mp4" % OK_JOB["id"])
with open(harvested_path, "rb") as f:
    check("the harvested bytes match the source", f.read() == CLIP_BYTES)

FAIL_JOB = {"id": "harvfail1", "lane": "t", "kind": "video", "status": "done",
            "sequence_id": sid5, "slot_id": slot5,
            "outputs": [{"filename": "does_not_exist.mp4", "subfolder": "", "type": "output", "media": "video"}]}
with new_srv.JOBS_LOCK:
    new_srv.JOBS[FAIL_JOB["id"]] = dict(FAIL_JOB)
new_srv.seq_add_take(sid5, slot5, FAIL_JOB["id"])
new_srv.seq_harvest(new_srv.LANE_BY_ID["t"], new_srv.JOBS[FAIL_JOB["id"]])
after5b = new_srv.seq_get(sid5)[0]
take_fail = next(t for t in after5b["slots"][0]["takes"] if t["job_id"] == FAIL_JOB["id"])
check("a failing harvest leaves the take's file null", take_fail["file"] is None, take_fail)
check("a failing harvest never touches the job's status", new_srv.JOBS[FAIL_JOB["id"]]["status"] == "done")


# ---------------------------------------------------------------------------
print("sees_refs, warnings, and stale 'room plate changed' after re-picking the set")

seq6, code = new_srv.seq_create({"title": "Warnings", "mode": "sequence"})
sid6, rev6 = seq6["id"], seq6["rev"]
b, c = op(new_srv, sid6, rev6, "add_slot", lane="video", cap="video", mode="ref2v",
          values={"prompt": "a scene", "length": 124})
rev6 = b["rev"]
slot6 = b["slots"][0]["id"]
check("sees_refs true: refs=='auto' and ref2v has an image_list field",
      b["slots"][0]["sees_refs"] is True, b["slots"][0])
check("no set yet -> 'the room has no set plate yet'",
      "the room has no set plate yet" in b["slots"][0]["warnings"], b["slots"][0]["warnings"])

b, c = op(new_srv, sid6, rev6, "update_slot", slot_id=slot6, refs="off")
rev6 = b["rev"]
check("sees_refs false once refs=='off'", b["slots"][0]["sees_refs"] is False, b["slots"][0])

PILImage.new("RGB", (64, 64), (5, 5, 5)).save(os.path.join(STORE_T, "outputs", "warn_set.png"))
NOPRESET_JOB = {"id": "nopreset1", "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
                "recipe": None,
                "outputs": [{"filename": "warn_set.png", "subfolder": "", "type": "output", "media": "image"}]}
with new_srv.JOBS_LOCK:
    new_srv.JOBS[NOPRESET_JOB["id"]] = dict(NOPRESET_JOB)
b, c = op(new_srv, sid6, rev6, "add_ref", job_id=NOPRESET_JOB["id"], output=0, role="set")
rev6 = b["rev"]
check("refs=='off': a video slot with a set in the room but no view of it",
      "this shot will invent its own room" in b["slots"][0]["warnings"], b["slots"][0]["warnings"])

b, c = op(new_srv, sid6, rev6, "update_slot", slot_id=slot6, refs="auto")
rev6 = b["rev"]
check("refs=='auto' again: the set's own role mismatch warning shows (not made with a set-role preset)",
      "not made with the no-people recipe" in b["slots"][0]["warnings"], b["slots"][0]["warnings"])

# Stale "room plate changed": pick a take whose recorded inputs.refs no
# longer matches what the resolver would produce now (the set gets swapped).
with new_srv.JOBS_LOCK:
    new_srv.JOBS["staletk01"] = {"id": "staletk01", "lane": "t", "kind": "video", "status": "done", "outputs": []}
new_srv.seq_add_take(sid6, slot6, "staletk01", inputs={"refs": ["r1:%s" % NOPRESET_JOB["id"]], "cables": {}})
cur = new_srv.seq_get(sid6)[0]
b, c = op(new_srv, sid6, cur["rev"], "pick_take", slot_id=slot6, job_id="staletk01")
rev6 = b["rev"]
check("freshly picked, not stale", b["slots"][0]["stale"] == [], b["slots"][0])

b, c = op(new_srv, sid6, rev6, "remove_ref", ref_id="r1")
rev6 = b["rev"]
PILImage.new("RGB", (64, 64), (6, 6, 6)).save(os.path.join(STORE_T, "outputs", "warn_newset.png"))
NEW_SET_JOB = {"id": "newsetjob", "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
               "outputs": [{"filename": "warn_newset.png", "subfolder": "", "type": "output", "media": "image"}]}
with new_srv.JOBS_LOCK:
    new_srv.JOBS[NEW_SET_JOB["id"]] = dict(NEW_SET_JOB)
b, c = op(new_srv, sid6, rev6, "add_ref", job_id=NEW_SET_JOB["id"], output=0, role="set")
rev6 = b["rev"]
check("re-picking the set makes the earlier take's pick STALE 'room plate changed'",
      "room plate changed" in b["slots"][0]["stale"], b["slots"][0]["stale"])


# ---------------------------------------------------------------------------
print("add_ref's source fetch never holds SEQ_LOCK: a slow ref does not stall another sequence's ops")

seqA, code = new_srv.seq_create({"title": "Slow add_ref", "mode": "sequence"})
sidA, revA = seqA["id"], seqA["rev"]
seqB, code = new_srv.seq_create({"title": "Concurrent update", "mode": "sequence"})
sidB, revB = seqB["id"], seqB["rev"]
b, c = op(new_srv, sidB, revB, "add_slot", lane="picture", cap="image", mode="t2i", values={"prompt": "x"})
revB, slotB = b["rev"], b["slots"][0]["id"]

SLOW_JOB_ID = "slowjob01"
PILImage.new("RGB", (32, 32), (1, 2, 3)).save(os.path.join(STORE_T, "outputs", "slow.png"))
with new_srv.JOBS_LOCK:
    new_srv.JOBS[SLOW_JOB_ID] = {"id": SLOW_JOB_ID, "lane": "t", "kind": "image", "mode": "t2i",
                                  "status": "done",
                                  "outputs": [{"filename": "slow.png", "subfolder": "", "type": "output",
                                               "media": "image"}]}

real_fetch = new_srv._carry_source_bytes
def slow_fetch(job, out, cache_path=None):
    if job.get("id") == SLOW_JOB_ID:
        time.sleep(3.0)
    return real_fetch(job, out, cache_path)
new_srv._carry_source_bytes = slow_fetch

add_ref_result = {}
def do_slow_add_ref():
    add_ref_result["result"] = op(new_srv, sidA, revA, "add_ref", job_id=SLOW_JOB_ID, output=0, role="set")

t_add_ref = threading.Thread(target=do_slow_add_ref)
t_add_ref.start()
time.sleep(0.5)   # let it get well into the mocked 3s sleep, still outside SEQ_LOCK

t0 = time.time()
b, c = op(new_srv, sidB, revB, "update_slot", slot_id=slotB, values={"prompt": "concurrent"})
elapsed = time.time() - t0
check("a concurrent update_slot on a DIFFERENT sequence completes in well under 1s",
      c == 200 and elapsed < 1.0, (c, elapsed))

t_add_ref.join(timeout=10)
new_srv._carry_source_bytes = real_fetch
check("the slow add_ref itself still completed and succeeded",
      add_ref_result["result"][1] == 200, add_ref_result["result"])


# ---------------------------------------------------------------------------
print("resolve_slot_cables never hands carry() a cache_path outside data/seq/<id>/")

import engines
video_mode_jack = next((m for m in engines.modes_for("video")
                        if any(f.get("type") == "image" for f in engines.fields("video", m))), None)
if video_mode_jack is None:
    check("(skipped -- no installed video mode declares an image-type field)", False)
else:
    jack_field = next(f["id"] for f in engines.fields("video", video_mode_jack) if f.get("type") == "image")
    seqC, _ = new_srv.seq_create({"title": "Cable escape", "mode": "sequence"})
    sidC, revC = seqC["id"], seqC["rev"]
    b, c = op(new_srv, sidC, revC, "add_slot", lane="picture", cap="image", mode="t2i", values={"prompt": "x"})
    revC, pC = b["rev"], b["slots"][-1]["id"]
    video_values = {f["id"]: f["default"] for f in engines.fields("video", video_mode_jack) if f.get("default") is not None}
    video_values["prompt"] = "y"
    b, c = op(new_srv, sidC, revC, "add_slot", lane="video", cap="video", mode=video_mode_jack, values=video_values)
    revC, vC = b["rev"], b["slots"][-1]["id"]
    b, c = op(new_srv, sidC, revC, "patch", **{"from": pC, "to": vC, "field": jack_field})
    revC = b["rev"]

    ESCAPE_JOB_ID = "escapejob01"
    PILImage.new("RGB", (32, 32), (9, 9, 9)).save(os.path.join(STORE_T, "outputs", "escape.png"))
    with new_srv.JOBS_LOCK:
        new_srv.JOBS[ESCAPE_JOB_ID] = {"id": ESCAPE_JOB_ID, "lane": "t", "kind": "image", "mode": "t2i",
                                        "status": "done",
                                        "outputs": [{"filename": "escape.png", "subfolder": "", "type": "output",
                                                     "media": "image"}]}
    new_srv.seq_add_take(sidC, pC, ESCAPE_JOB_ID)
    curC = new_srv.seq_get(sidC)[0]["rev"]
    b, c = op(new_srv, sidC, curC, "pick_take", slot_id=pC, job_id=ESCAPE_JOB_ID)

    # Implant a take whose stored `file` tries to escape data/seq/<id>/ -- the
    # sequence JSON on disk is untrusted input, even though server.py itself
    # is the only thing that ever writes take["file"] on the normal path
    # (seq_harvest). Bypasses the ops layer on purpose, same as
    # tests/test_cables.py's own raw-file implants.
    raw_path = os.path.join(new_srv.SEQ_DIR, sidC + ".json")
    rawC = json.load(open(raw_path))
    for slot in rawC["slots"]:
        if slot["id"] == pC:
            for t in slot["takes"]:
                if t["job_id"] == ESCAPE_JOB_ID:
                    t["file"] = "takes/../../../../../../etc/passwd"
    json.dump(rawC, open(raw_path, "w"))

    seen = {}
    real_carry = new_srv.carry
    def spy_carry(job, output_index, target_lane, fit=None, cache_path=None):
        seen["cache_path"] = cache_path
        return real_carry(job, output_index, target_lane, fit=fit, cache_path=cache_path)
    new_srv.carry = spy_carry
    control(PORT_T, [{"filename": "cable_escape.png", "subfolder": "", "type": "output"}])
    fetchedC = new_srv.seq_get(sidC)[0]
    slotC = next(s for s in fetchedC["slots"] if s["id"] == vC)
    try:
        field_values, cable_uses = new_srv.resolve_slot_cables(fetchedC, slotC, new_srv.LANE_BY_ID["t"])
    finally:
        new_srv.carry = real_carry
    check("a take file escaping data/seq/<id>/ is never handed to carry() as cache_path",
          seen.get("cache_path") is None, seen.get("cache_path"))
    check("resolve_slot_cables still resolves the field (falls back to fetching from the source lane)",
          jack_field in field_values, field_values)


# ---------------------------------------------------------------------------
FAKE_T.terminate()
FAKE_T.wait(timeout=10)
shutil.rmtree(SCRATCH, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
