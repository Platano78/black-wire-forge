"""Acceptance gate for C3.4 -- patch cables (the internal sequence/storyboard design spec
Sections 1, 4, 7; slice C3.4). Written AGAINST the frozen rules before the
feature exists: server.py still lists "patch"/"unpatch" in SEQ_LATER_OPS
(around line 1142), so every check below is expected to FAIL on the current
tree, and this file records that as RED.

ISOLATION: every byte this test writes goes under a fresh tempfile.mkdtemp()
directory; GENCENTER_DATA/GENCENTER_CONFIG point there before server.py is
ever imported, the same way tests/test_slot_generate.py does it. One fake
ComfyUI lane ("t") runs as a subprocess on a closed local port. The real
data/ tree is never touched.

Every check() call is independent -- one failure never aborts the rest.

Run: python3 tests/test_cables.py
"""
import importlib.util, json, os, shutil, socket, subprocess, sys
import tempfile, time, urllib.request
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
# a clean clone is a SKIP, not a failure. A late section of this suite
# builds its own Pillow source image, so the whole suite skips rather than
# a section.
try:
    from PIL import Image as PILImage  # noqa: F401
except ImportError:
    print("SKIP: Pillow is not installed -- this suite needs it")
    sys.exit(0)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


# ---------------------------------------------------------------------------
# One fake lane ("t"), the same fixture test_slot_generate.py / test_refs.py use.
# ---------------------------------------------------------------------------

SCRATCH = tempfile.mkdtemp(prefix="bwf_cables_")
print("scratch dir: %s (real data/ is never written)" % SCRATCH)

STORE = os.path.join(SCRATCH, "store_t")
os.makedirs(os.path.join(STORE, "outputs"))
PORT_T = free_port()
FAKE_T = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
     "--port", str(PORT_T), "--store", STORE],
    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % PORT_T, timeout=0.5)
        break
    except Exception:
        time.sleep(0.05)
else:
    raise SystemExit("fake lane did not come up")

CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "c34 test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": PORT_T,
                      "caps": ["image", "video"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG

MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
# Fill in a stub filename for every role any pack declares but golden/models.json
# leaves empty/absent, so whichever video mode gets discovered below (any pack,
# never assumed) reports itself fully installed on the fake lane.
for _role in engines.model_keys():
    if not MODELS.get(_role):
        MODELS[_role] = "stub_%s.safetensors" % _role

DATA_DIR = os.path.join(SCRATCH, "data_main")


def load_server(data_dir, name):
    os.environ["GENCENTER_DATA"] = data_dir
    spec = importlib.util.spec_from_file_location("srv_cables_%s" % name, os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.JOBS_FILE = os.path.join(data_dir, "jobs.json")
    mod.SEQ_DIR = os.path.join(data_dir, "sequences")
    mod.SEQ_MEDIA_DIR = os.path.join(data_dir, "seq")
    mod.CHAIN_DIR = os.path.join(data_dir, "chain")
    mod.LOCAL_OUTPUTS_DIR = os.path.join(data_dir, "outputs")
    for d in (mod.SEQ_DIR, mod.SEQ_MEDIA_DIR, mod.CHAIN_DIR, mod.LOCAL_OUTPUTS_DIR):
        os.makedirs(d, exist_ok=True)
    mod.LANE_BY_ID["t"]["models"] = dict(MODELS)
    with mod.STATE_LOCK:
        mod.LANE_STATE["t"] = {"up": True, "checked": time.time(), "err": ""}
    return mod


mod = load_server(DATA_DIR, "main")


def op(sid, rev, name, **kw):
    return mod.seq_op(dict(kw, id=sid, rev=rev, op=name))


def control(outputs):
    """Flip the fake lane's /prompt between reject (default) and accept."""
    req = urllib.request.Request(
        "http://127.0.0.1:%d/_control/accept" % PORT_T,
        data=json.dumps({"outputs": outputs}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def last_prompt():
    path = os.path.join(STORE, "last_prompt.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def input_files():
    d = os.path.join(STORE, "inputs")
    return set(os.listdir(d)) if os.path.isdir(d) else set()


def raw_seq(sid):
    return json.load(open(os.path.join(mod.SEQ_DIR, sid + ".json")))


def write_raw_seq(sid, obj):
    path = os.path.join(mod.SEQ_DIR, sid + ".json")
    json.dump(obj, open(path, "w"))


# ---------------------------------------------------------------------------
# Discover real modes from the packs' own field lists, never hardcoded:
#   - a video mode with >=1 field of type "image" (the jack donor)
#   - a video mode/cap usable in a video-lane slot with NO image field
# ---------------------------------------------------------------------------

def image_fields(cap, mode):
    return [f for f in engines.fields(cap, mode) if f.get("type") == "image"]

VIDEO_MODE_WITH_JACKS = None
VIDEO_MODE_NO_JACKS = None
for m in engines.modes_for("video"):
    imgs = image_fields("video", m)
    if imgs and VIDEO_MODE_WITH_JACKS is None:
        VIDEO_MODE_WITH_JACKS = m
    if not imgs and VIDEO_MODE_NO_JACKS is None:
        VIDEO_MODE_NO_JACKS = m

check("sanity: a real video mode declares >=1 image-type field (no fake pack needed)",
      VIDEO_MODE_WITH_JACKS is not None, VIDEO_MODE_WITH_JACKS)
check("sanity: a real video mode declares NO image-type field (no fake pack needed)",
      VIDEO_MODE_NO_JACKS is not None, VIDEO_MODE_NO_JACKS)

JACK_FIELDS = image_fields("video", VIDEO_MODE_WITH_JACKS) if VIDEO_MODE_WITH_JACKS else []
JACK_FIELD_ID = JACK_FIELDS[0]["id"] if JACK_FIELDS else None
JACK_FIELD_LABEL = JACK_FIELDS[0]["label"] if JACK_FIELDS else None
EXPECTED_JACKS = [{"field": f["id"], "label": f["label"], "type": "image"} for f in JACK_FIELDS]
print("     video mode with jacks: %r -> %r" % (VIDEO_MODE_WITH_JACKS, EXPECTED_JACKS))
print("     video mode with no jacks: %r" % (VIDEO_MODE_NO_JACKS,))

PICTURE_MODE = "t2i"
check("sanity: image/t2i has no image-type field (a plain picture slot)",
      not image_fields("image", PICTURE_MODE))

# A picture-lane mode that itself DOES declare an image-type field (e.g. a
# cutout/upscale/pixel-art recipe takes a picture as input) -- discovered at
# runtime, never hardcoded, so Rule 3's "non-video slots have jacks == []"
# is actually exercised against a slot that could plausibly have a jack.
PICTURE_MODE_WITH_IMAGE_FIELD = next(
    (m for m in engines.modes_for("image") if image_fields("image", m)), None)
check("sanity: an image-cap mode declares an image-type field (a picture recipe that itself takes a picture)",
      PICTURE_MODE_WITH_IMAGE_FIELD is not None, PICTURE_MODE_WITH_IMAGE_FIELD)
print("     picture mode with an image field (jack-eligible cap, but not video-lane): %r"
      % (PICTURE_MODE_WITH_IMAGE_FIELD,))


def add_picture_slot_with_image_field(sid, rev):
    return op(sid, rev, "add_slot", lane="picture", cap="image", mode=PICTURE_MODE_WITH_IMAGE_FIELD, values={})

def default_video_values(mode):
    """The mode's own declared defaults (never a hardcoded length/etc that might
    violate some OTHER pack's own constraint, e.g. LTX's length % 8 == 1)."""
    values = {f["id"]: f["default"] for f in engines.fields("video", mode) if f.get("default") is not None}
    values["prompt"] = "a scene"
    return values

VIDEO_VALUES = default_video_values(VIDEO_MODE_WITH_JACKS) if VIDEO_MODE_WITH_JACKS else {"prompt": "a scene"}


def add_video_slot(sid, rev, mode=None):
    mode = mode or VIDEO_MODE_WITH_JACKS
    b, c = op(sid, rev, "add_slot", lane="video", cap="video", mode=mode,
              values=default_video_values(mode))
    return b, c


def add_picture_slot(sid, rev):
    b, c = op(sid, rev, "add_slot", lane="picture", cap="image", mode=PICTURE_MODE, values={"prompt": "a person"})
    return b, c


# ===========================================================================
print("\nRule 1: patch ok -> rev+1, cable stored with from/to/field/id, survives a restart")

seq1, _ = mod.seq_create({"title": "Rule1", "mode": "sequence"})
sid1, rev1 = seq1["id"], seq1["rev"]
b, c = add_picture_slot(sid1, rev1); rev1 = b["rev"]; p1 = b["slots"][-1]["id"]
b, c = add_video_slot(sid1, rev1); rev1 = b["rev"]; v1 = b["slots"][-1]["id"]

before_rev = rev1
b, c = op(sid1, rev1, "patch", **{"from": p1, "to": v1, "field": JACK_FIELD_ID})
check("Rule1: patch {from: picture slot, to: video slot, field: image field} -> ok (200)", c == 200, b)
check("Rule1: rev increments by exactly 1", c == 200 and b.get("rev") == before_rev + 1, (b.get("rev"), before_rev))
cables = b.get("cables") if isinstance(b, dict) else None
new_cable = next((cb for cb in (cables or [])
                   if cb.get("from") == p1 and cb.get("to") == v1 and cb.get("field") == JACK_FIELD_ID), None)
check("Rule1: the stored sequence has a cable with that from/to/field and an id",
      new_cable is not None and isinstance(new_cable.get("id"), str) and new_cable["id"], cables)

mod2 = load_server(DATA_DIR, "restart1")
after_restart = mod2.seq_get(sid1)[0]
cable_after = next((cb for cb in (after_restart.get("cables") or [])
                     if cb.get("from") == p1 and cb.get("to") == v1 and cb.get("field") == JACK_FIELD_ID), None)
check("Rule1: the cable is still there after reloading the module against the same data dir",
      cable_after is not None, after_restart.get("cables"))


# ===========================================================================
print("\nRule 2: five exact refusal sentences")

seq2, _ = mod.seq_create({"title": "Rule2", "mode": "sequence"})
sid2, rev2 = seq2["id"], seq2["rev"]
b, c = add_picture_slot(sid2, rev2); rev2 = b["rev"]; p2a = b["slots"][-1]["id"]
b, c = add_picture_slot(sid2, rev2); rev2 = b["rev"]; p2b = b["slots"][-1]["id"]
b, c = add_video_slot(sid2, rev2, mode=VIDEO_MODE_WITH_JACKS); rev2 = b["rev"]; v2a = b["slots"][-1]["id"]
if VIDEO_MODE_NO_JACKS:
    b, c = add_video_slot(sid2, rev2, mode=VIDEO_MODE_NO_JACKS); rev2 = b["rev"]; v2b = b["slots"][-1]["id"]
else:
    v2b = None

# (a) from is not a picture-lane slot
b, c = op(sid2, rev2, "patch", **{"from": v2a, "to": v2a, "field": JACK_FIELD_ID})
check('Rule2a: from-not-picture refused 400 "A cable starts at a picture."',
      c == 400 and b.get("error") == "A cable starts at a picture.", b)

# (b) to is not a video-lane slot
b, c = op(sid2, rev2, "patch", **{"from": p2a, "to": p2b, "field": JACK_FIELD_ID})
check('Rule2b: to-not-video refused 400 "A cable ends at a video shot."',
      c == 400 and b.get("error") == "A cable ends at a video shot.", b)

# (c) to's mode has no image-type field at all
if v2b is not None:
    b, c = op(sid2, rev2, "patch", **{"from": p2a, "to": v2b, "field": "whatever"})
    check('Rule2c: to-mode-has-no-image-field refused 400 '
          '"This shot\'s recipe has no picture input to plug into."',
          c == 400 and b.get("error") == "This shot's recipe has no picture input to plug into.", b)
else:
    check("Rule2c: (skipped -- no video mode with zero image fields was discovered)", False,
          "VIDEO_MODE_NO_JACKS is None")

# (d) to's mode has image fields but not the named one
b, c = op(sid2, rev2, "patch", **{"from": p2a, "to": v2a, "field": "not-a-real-field-id"})
check('Rule2d: to-mode-lacks-that-field refused 400 '
      '"This shot\'s recipe has no input called that."',
      c == 400 and b.get("error") == "This shot's recipe has no input called that.", b)

# (e) a second cable into the same (to, field)
b, c = op(sid2, rev2, "patch", **{"from": p2a, "to": v2a, "field": JACK_FIELD_ID})
first_ok = c == 200
rev2_after_first = b.get("rev", rev2) if first_ok else rev2
b, c = op(sid2, rev2_after_first, "patch", **{"from": p2b, "to": v2a, "field": JACK_FIELD_ID})
check('Rule2e: a second cable into the same (to, field) refused 400 '
      '"That input already has a cable. Unplug it first."',
      c == 400 and b.get("error") == "That input already has a cable. Unplug it first.", b)


# ===========================================================================
print("\nRule 3: GET /api/sequence -- jacks per slot")

seq3, _ = mod.seq_create({"title": "Rule3", "mode": "sequence"})
sid3, rev3 = seq3["id"], seq3["rev"]
b, c = add_picture_slot(sid3, rev3); rev3 = b["rev"]; p3 = b["slots"][-1]["id"]
b, c = add_video_slot(sid3, rev3); rev3 = b["rev"]; v3 = b["slots"][-1]["id"]

if PICTURE_MODE_WITH_IMAGE_FIELD is not None:
    b, c = add_picture_slot_with_image_field(sid3, rev3); rev3 = b["rev"]; p3c = b["slots"][-1]["id"]
else:
    p3c = None

fetched = mod.seq_get(sid3)[0]
v3_slot = next(s for s in fetched["slots"] if s["id"] == v3)
p3_slot = next(s for s in fetched["slots"] if s["id"] == p3)
check("Rule3: the video slot's jacks == its mode's image-type fields, in pack order",
      v3_slot.get("jacks") == EXPECTED_JACKS, v3_slot.get("jacks"))
check("Rule3: a non-video slot has jacks == []", p3_slot.get("jacks") == [], p3_slot.get("jacks"))

if p3c is not None:
    p3c_slot = next(s for s in fetched["slots"] if s["id"] == p3c)
    check("Rule3: a PICTURE-lane slot whose own mode (%r) DOES declare an image-type "
          "field still has jacks == [] (jacks are a video-lane thing, not just "
          "'this mode has no image field')" % (PICTURE_MODE_WITH_IMAGE_FIELD,),
          p3c_slot.get("jacks") == [], p3c_slot.get("jacks"))
else:
    check("Rule3: (skipped -- no image-cap mode with an image-type field was discovered)", False)


# ===========================================================================
print("\nRule 4: unpatch removes a cable (rev+1); unknown id refused")

seq4, _ = mod.seq_create({"title": "Rule4", "mode": "sequence"})
sid4, rev4 = seq4["id"], seq4["rev"]
b, c = add_picture_slot(sid4, rev4); rev4 = b["rev"]; p4 = b["slots"][-1]["id"]
b, c = add_video_slot(sid4, rev4); rev4 = b["rev"]; v4 = b["slots"][-1]["id"]

# Implant a cable directly (cables[] is already part of the stored schema,
# per Section 1's object literal -- this only bypasses the "patch" OP, not
# the schema) so unpatch can be exercised without depending on patch itself.
raw4 = raw_seq(sid4)
raw4["cables"] = [{"id": "cimplant4", "from": p4, "to": v4, "field": JACK_FIELD_ID}]
write_raw_seq(sid4, raw4)
cur_rev4 = raw_seq(sid4)["rev"]

b, c = op(sid4, cur_rev4, "unpatch", cable_id="cimplant4")
check("Rule4: unpatch {cable_id} removes it (rev+1)",
      c == 200 and b.get("rev") == cur_rev4 + 1
      and not any(cb.get("id") == "cimplant4" for cb in (b.get("cables") or [])), b)
rev4_after = b.get("rev", cur_rev4)

b, c = op(sid4, rev4_after, "unpatch", cable_id="no-such-cable")
check('Rule4: unknown id refused 400 "There is no such cable."',
      c == 400 and b.get("error") == "There is no such cable.", b)


# ===========================================================================
print("\nRule 5: update_slot changing the video slot's mode away removes the patched cable")

seq5, _ = mod.seq_create({"title": "Rule5", "mode": "sequence"})
sid5, rev5 = seq5["id"], seq5["rev"]
b, c = add_picture_slot(sid5, rev5); rev5 = b["rev"]; p5 = b["slots"][-1]["id"]
b, c = add_video_slot(sid5, rev5); rev5 = b["rev"]; v5 = b["slots"][-1]["id"]

raw5 = raw_seq(sid5)
raw5["cables"] = [{"id": "cimplant5", "from": p5, "to": v5, "field": JACK_FIELD_ID}]
write_raw_seq(sid5, raw5)
cur_rev5 = raw_seq(sid5)["rev"]

new_mode = VIDEO_MODE_NO_JACKS or next(
    (m for m in engines.modes_for("video") if m != VIDEO_MODE_WITH_JACKS
     and JACK_FIELD_ID not in {f["id"] for f in engines.fields("video", m)}), None)
if new_mode is None:
    check("Rule5: (skipped -- no alternate video mode lacking the patched field was discovered)", False)
else:
    old_field_ids = {f["id"] for f in engines.fields("video", VIDEO_MODE_WITH_JACKS)}
    new_field_ids = {f["id"] for f in engines.fields("video", new_mode)}
    switch_values = {k: None for k in old_field_ids - new_field_ids}   # drop what the new mode does not know
    switch_values.update(default_video_values(new_mode))
    b, c = op(sid5, cur_rev5, "update_slot", slot_id=v5, mode=new_mode, values=switch_values)
    check("Rule5: update_slot to a mode lacking the patched field removes that cable",
          c == 200 and not any(cb.get("id") == "cimplant5" for cb in (b.get("cables") or [])), b)


# ===========================================================================
print("\nRule 6: generate on a patched video slot with an unmade source refuses with the jack sentence")

seq6, _ = mod.seq_create({"title": "Rule6", "mode": "sequence"})
sid6, rev6 = seq6["id"], seq6["rev"]
b, c = add_picture_slot(sid6, rev6); rev6 = b["rev"]; p6 = b["slots"][-1]["id"]   # no pick
b, c = add_video_slot(sid6, rev6); rev6 = b["rev"]; v6 = b["slots"][-1]["id"]

b, c = op(sid6, rev6, "patch", **{"from": p6, "to": v6, "field": JACK_FIELD_ID})
rev6 = b.get("rev", rev6)

control([{"filename": "rule6.mp4", "subfolder": "", "type": "output"}])
result6, gcode6 = mod.seq_generate({"id": sid6, "slot_id": v6})
check('Rule6: generate refuses 400 "%s is not made yet." when the cabled source has no pick'
      % JACK_FIELD_LABEL,
      gcode6 == 400 and result6.get("error") == "%s is not made yet." % JACK_FIELD_LABEL, result6)


# ===========================================================================
print("\nRule 7: generate with a finished source pick -- graph, uploaded size, take.inputs.cables")

seq7, _ = mod.seq_create({"title": "Rule7", "mode": "sequence"})
sid7, rev7 = seq7["id"], seq7["rev"]
b, c = add_picture_slot(sid7, rev7); rev7 = b["rev"]; p7 = b["slots"][-1]["id"]
b, c = add_video_slot(sid7, rev7); rev7 = b["rev"]; v7 = b["slots"][-1]["id"]

CANVAS_W, CANVAS_H = 320, 240
b, c = op(sid7, rev7, "set_canvas", width=CANVAS_W, height=CANVAS_H)
rev7 = b["rev"]

from PIL import Image as PILImage
PIC_PATH = os.path.join(STORE, "outputs", "pic_from.png")
PILImage.new("RGB", (640, 480), (40, 60, 80)).save(PIC_PATH)
PIC_JOB_ID = "picjob01"
with mod.JOBS_LOCK:
    mod.JOBS[PIC_JOB_ID] = {"id": PIC_JOB_ID, "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
                             "outputs": [{"filename": "pic_from.png", "subfolder": "", "type": "output",
                                          "media": "image"}]}
mod.seq_add_take(sid7, p7, PIC_JOB_ID)
cur7 = mod.seq_get(sid7)[0]["rev"]
b, c = op(sid7, cur7, "pick_take", slot_id=p7, job_id=PIC_JOB_ID)
rev7 = b["rev"]

b, c = op(sid7, rev7, "patch", **{"from": p7, "to": v7, "field": JACK_FIELD_ID})
rev7 = b.get("rev", rev7)

before_inputs = input_files()
control([{"filename": "rule7.mp4", "subfolder": "", "type": "output"}])
result7, gcode7 = mod.seq_generate({"id": sid7, "slot_id": v7})
check("Rule7: generate on the patched, source-ready slot succeeds",
      gcode7 == 200 and result7.get("ok") is True, result7)

after_inputs = input_files()
new_uploads = after_inputs - before_inputs
check("Rule7: exactly one new file was uploaded to the target lane for the cable",
      len(new_uploads) == 1, sorted(new_uploads))

uploaded_name = next(iter(new_uploads)) if len(new_uploads) == 1 else None
graph7 = last_prompt()
load_image_hit = None
if uploaded_name:
    for node in graph7.values():
        if isinstance(node, dict) and node.get("class_type") == "LoadImage" \
                and node.get("inputs", {}).get("image") == uploaded_name:
            load_image_hit = node
            break
check("Rule7: the graph submitted to the fake lane contains a LoadImage node "
      "whose image value is the uploaded carried file",
      load_image_hit is not None, (uploaded_name, graph7))

if uploaded_name:
    with PILImage.open(os.path.join(STORE, "inputs", uploaded_name)) as im:
        uploaded_size = im.size
else:
    uploaded_size = None
check("Rule7: the uploaded file, opened with PIL, has exactly the sequence canvas size",
      uploaded_size == (CANVAS_W, CANVAS_H), uploaded_size)

seq7_after = mod.seq_get(sid7)[0]
v7_slot = next(s for s in seq7_after["slots"] if s["id"] == v7)
last_take7 = (v7_slot.get("takes") or [])[-1] if v7_slot.get("takes") else {}
check("Rule7: the new take's inputs.cables == {field: source pick job id}",
      last_take7.get("inputs", {}).get("cables") == {JACK_FIELD_ID: PIC_JOB_ID}, last_take7)


# ===========================================================================
print("\nRule 8 & 9: staleness from a re-picked cable source, and non-staleness with no cable")

seq89, _ = mod.seq_create({"title": "Rule89", "mode": "sequence"})
sid89, rev89 = seq89["id"], seq89["rev"]
b, c = add_picture_slot(sid89, rev89); rev89 = b["rev"]; p89 = b["slots"][-1]["id"]
b, c = add_video_slot(sid89, rev89); rev89 = b["rev"]; v_stale = b["slots"][-1]["id"]
b, c = add_video_slot(sid89, rev89); rev89 = b["rev"]; v_plain = b["slots"][-1]["id"]

JOB_OLD, JOB_NEW = "oldpick01", "newpick01"
with mod.JOBS_LOCK:
    mod.JOBS[JOB_OLD] = {"id": JOB_OLD, "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
                         "outputs": []}
    mod.JOBS[JOB_NEW] = {"id": JOB_NEW, "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
                         "outputs": []}
mod.seq_add_take(sid89, p89, JOB_OLD)
cur89 = mod.seq_get(sid89)[0]["rev"]
b, c = op(sid89, cur89, "pick_take", slot_id=p89, job_id=JOB_OLD)
rev89 = b["rev"]

# A cable already resolved once (from p89, when p89's pick was JOB_OLD).
VJOB_A = "vstaletk1"
with mod.JOBS_LOCK:
    mod.JOBS[VJOB_A] = {"id": VJOB_A, "lane": "t", "kind": "video", "status": "done", "outputs": []}
mod.seq_add_take(sid89, v_stale, VJOB_A, inputs={"refs": [], "cables": {JACK_FIELD_ID: JOB_OLD}})
cur89 = mod.seq_get(sid89)[0]["rev"]
b, c = op(sid89, cur89, "pick_take", slot_id=v_stale, job_id=VJOB_A)
rev89 = b["rev"]

raw89 = raw_seq(sid89)
raw89["cables"] = [{"id": "c89", "from": p89, "to": v_stale, "field": JACK_FIELD_ID}]
write_raw_seq(sid89, raw89)

mod.seq_add_take(sid89, p89, JOB_NEW)
cur89 = mod.seq_get(sid89)[0]["rev"]
b, c = op(sid89, cur89, "pick_take", slot_id=p89, job_id=JOB_NEW)
rev89 = b["rev"]

fetched89 = mod.seq_get(sid89)[0]
v_stale_slot = next(s for s in fetched89["slots"] if s["id"] == v_stale)
expected_reason = "%s changed" % JACK_FIELD_LABEL.lower()
check('Rule8: re-picking the cable source shows "%s" in the video slot\'s stale list'
      % expected_reason,
      expected_reason in (v_stale_slot.get("stale") or []), v_stale_slot.get("stale"))

VJOB_B = "vstaletk2"
with mod.JOBS_LOCK:
    mod.JOBS[VJOB_B] = {"id": VJOB_B, "lane": "t", "kind": "video", "status": "done", "outputs": []}
mod.seq_add_take(sid89, v_stale, VJOB_B, inputs={"refs": [], "cables": {JACK_FIELD_ID: JOB_NEW}})
cur89 = mod.seq_get(sid89)[0]["rev"]
b, c = op(sid89, cur89, "pick_take", slot_id=v_stale, job_id=VJOB_B)
rev89 = b["rev"]

fetched89b = mod.seq_get(sid89)[0]
v_stale_slot2 = next(s for s in fetched89b["slots"] if s["id"] == v_stale)
check('Rule8: a new take on the video slot clears "%s"' % expected_reason,
      expected_reason not in (v_stale_slot2.get("stale") or []), v_stale_slot2.get("stale"))

# Rule 9: a take with inputs.cables == {} on a slot with no cables at all is not stale.
VJOB_PLAIN = "vplaintk1"
with mod.JOBS_LOCK:
    mod.JOBS[VJOB_PLAIN] = {"id": VJOB_PLAIN, "lane": "t", "kind": "video", "status": "done", "outputs": []}
mod.seq_add_take(sid89, v_plain, VJOB_PLAIN, inputs={"refs": [], "cables": {}})
cur89b = mod.seq_get(sid89)[0]["rev"]
b, c = op(sid89, cur89b, "pick_take", slot_id=v_plain, job_id=VJOB_PLAIN)

fetched89c = mod.seq_get(sid89)[0]
v_plain_slot = next(s for s in fetched89c["slots"] if s["id"] == v_plain)
check("Rule9: a take with inputs.cables == {} on a slot with no cables is not stale",
      c == 200 and v_plain_slot.get("stale") == [], (c, v_plain_slot.get("stale")))


# ---------------------------------------------------------------------------
FAKE_T.terminate()
FAKE_T.wait(timeout=10)
shutil.rmtree(SCRATCH, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
