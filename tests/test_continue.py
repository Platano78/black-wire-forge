"""Frozen acceptance gate for C3.4b -- continue from the last shot
(the internal continue-feature commission doc, rules K1-K7). Written AGAINST the
frozen commission before the feature exists.

RED on the current tree, by construction: server.py's `slot_jacks` only ever
looks at fields of type "image" (server.py around line 1376-1389), `_op_patch`
unconditionally requires `from` to be a picture-lane slot (around line
1805-1824), and no engine pack declares a field of type "video" yet -- so
every check below that depends on any of that is expected to FAIL on the
current tree, and this file records that as RED. If NO pack declares a field
of type "video" at all, this file still fails loudly (see the sanity check
below) rather than silently skipping everything.

ISOLATION: same model as tests/test_cables.py -- one fake ComfyUI lane ("t")
on a closed local port, server.py imported in-process against a fresh
tempfile.mkdtemp() data dir (GENCENTER_DATA/GENCENTER_CONFIG point there
before import). The real data/ tree and the live app are never touched.

Every check() call is independent -- one failure never aborts the rest.

Run: python3 tests/test_continue.py
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

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


# ---------------------------------------------------------------------------
# One fake lane ("t"), same fixture as test_cables.py / test_refs.py.
# ---------------------------------------------------------------------------

SCRATCH = tempfile.mkdtemp(prefix="bwf_continue_")
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
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "c34b test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": PORT_T,
                      "caps": ["image", "video"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG

MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
# Fill in a stub filename for every role any pack declares but golden/models.json
# leaves empty/absent -- so whichever mode this file discovers below (any
# pack, never assumed) reports itself fully installed on the fake lane and
# graph_for() never KeyErrors on a role the continue pack introduces.
for _role in engines.model_keys():
    if not MODELS.get(_role):
        MODELS[_role] = "stub_%s.safetensors" % _role

DATA_DIR = os.path.join(SCRATCH, "data_main")


def load_server(data_dir, name):
    os.environ["GENCENTER_DATA"] = data_dir
    spec = importlib.util.spec_from_file_location("srv_continue_%s" % name, os.path.join(ROOT, "server.py"))
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


def new_seq(title):
    seq, _ = mod.seq_create({"title": title, "mode": "sequence"})
    return seq["id"], seq["rev"]


# ---------------------------------------------------------------------------
# Discovery (never hardcoded): the first video-lane mode that declares a
# field of type "video" -- K5's continue-mode pack. FAIL loudly, don't skip,
# if no pack declares one; every later section guards on this.
# ---------------------------------------------------------------------------

def video_fields(mode):
    return [f for f in engines.fields("video", mode) if f.get("type") == "video"]

def image_fields(mode):
    return [f for f in engines.fields("video", mode) if f.get("type") == "image"]

VIDEO_FIELD_MODE = None
VIDEO_FIELD = None
for m in engines.modes_for("video"):
    vf = video_fields(m)
    if vf:
        VIDEO_FIELD_MODE = m
        VIDEO_FIELD = vf[0]
        break

check("K5 sanity: some installed video-lane pack declares a field of type "
      "\"video\" (the continue mode) -- without this NOTHING below can be exercised",
      VIDEO_FIELD_MODE is not None, VIDEO_FIELD_MODE)

VIDEO_FIELD_ID = VIDEO_FIELD["id"] if VIDEO_FIELD else None
VIDEO_FIELD_LABEL = (VIDEO_FIELD.get("label") or VIDEO_FIELD_ID) if VIDEO_FIELD else None
print("     video-typed field: mode=%r field=%r label=%r" % (VIDEO_FIELD_MODE, VIDEO_FIELD_ID, VIDEO_FIELD_LABEL))

# A video mode that declares an IMAGE field but no video field (an
# image-only mode, e.g. C3.4's own fixture mode) -- for "an image-only mode
# still lists only image jacks" and for re-asserting C3.4's own rules
# unchanged.
IMAGE_ONLY_MODE = None
for m in engines.modes_for("video"):
    if m == VIDEO_FIELD_MODE:
        continue
    if image_fields(m) and not video_fields(m):
        IMAGE_ONLY_MODE = m
        break
check("sanity: some video-lane mode declares an image field and NO video field "
      "(a picture-lane cable target, unrelated to K1-K4)", IMAGE_ONLY_MODE is not None, IMAGE_ONLY_MODE)

PICTURE_MODE = "t2i"
check("sanity: image/t2i has no image-type field (a plain picture slot)",
      not [f for f in engines.fields("image", PICTURE_MODE) if f.get("type") == "image"])


def default_video_values(mode):
    """The mode's own declared defaults, plus a value for whichever field
    the mode actually uses for its main text (usually "prompt", but a mode
    like "talking" uses "line" instead -- never assume "prompt" exists)."""
    field_ids = {f["id"] for f in engines.fields("video", mode)}
    values = {f["id"]: f["default"] for f in engines.fields("video", mode) if f.get("default") is not None}
    for text_field in ("prompt", "line"):
        if text_field in field_ids:
            values[text_field] = "a scene"
            break
    return values


def add_video_slot(sid, rev, mode=None, at=None):
    mode = mode or VIDEO_FIELD_MODE
    kw = dict(lane="video", cap="video", mode=mode, values=default_video_values(mode))
    if at is not None:
        kw["at"] = at
    return op(sid, rev, "add_slot", **kw)


def add_picture_slot(sid, rev):
    return op(sid, rev, "add_slot", lane="picture", cap="image", mode=PICTURE_MODE, values={"prompt": "a person"})


def make_video_job(job_id, fname, data, media="video"):
    """Registers a 'done' job whose one output lives on the fake lane, and
    writes the bytes there. Returns the bytes written."""
    with open(os.path.join(STORE, "outputs", fname), "wb") as f:
        f.write(data)
    with mod.JOBS_LOCK:
        mod.JOBS[job_id] = {"id": job_id, "lane": "t", "kind": "video", "mode": VIDEO_FIELD_MODE,
                             "status": "done",
                             "outputs": [{"filename": fname, "subfolder": "", "type": "output", "media": media}]}
    return data


if VIDEO_FIELD_MODE is not None:
    # =======================================================================
    print("\nK1: slot_jacks lists the video field, typed; image-only modes "
          "list only image jacks; a picture-lane slot has none")

    seqK1, revK1 = new_seq("K1")
    b, c = add_video_slot(seqK1, revK1); revK1 = b["rev"]; vK1 = b["slots"][-1]["id"]
    pK1 = None
    if IMAGE_ONLY_MODE is not None:
        b, c = add_video_slot(seqK1, revK1, mode=IMAGE_ONLY_MODE); revK1 = b["rev"]; imgK1 = b["slots"][-1]["id"]
    else:
        imgK1 = None
    b, c = add_picture_slot(seqK1, revK1); revK1 = b["rev"]; pK1 = b["slots"][-1]["id"]

    fetchedK1 = mod.seq_get(seqK1)[0]
    slotK1 = next(s for s in fetchedK1["slots"] if s["id"] == vK1)
    jacksK1 = slotK1.get("jacks") or []
    hit = next((j for j in jacksK1 if j.get("field") == VIDEO_FIELD_ID), None)
    check("K1: slot_jacks / GET jacks includes the video field", hit is not None, jacksK1)
    check('K1: that jack is typed "video"', hit is not None and hit.get("type") == "video", hit)
    check("K1: that jack's label matches the pack's own field label",
          hit is not None and hit.get("label") == VIDEO_FIELD_LABEL, hit)

    if imgK1 is not None:
        imgSlotK1 = next(s for s in fetchedK1["slots"] if s["id"] == imgK1)
        imgJacksK1 = imgSlotK1.get("jacks") or []
        check("K1: an image-only video mode still lists only image-typed jacks",
              bool(imgJacksK1) and all(j.get("type") == "image" for j in imgJacksK1), imgJacksK1)
    else:
        check("K1: (cannot exercise -- no image-only video mode discovered)", False)

    pSlotK1 = next(s for s in fetchedK1["slots"] if s["id"] == pK1)
    check("K1: a picture-lane slot has no jacks at all", pSlotK1.get("jacks") == [], pSlotK1.get("jacks"))

    # =======================================================================
    print("\nK2: the continue cable -- earlier ok, later/self refused with "
          "'before', wrong lane refused, duplicate refused, C3.4 unchanged")

    seqK2, revK2 = new_seq("K2")
    b, c = add_video_slot(seqK2, revK2); revK2 = b["rev"]; vA = b["slots"][-1]["id"]   # earlier
    b, c = add_video_slot(seqK2, revK2); revK2 = b["rev"]; vB = b["slots"][-1]["id"]   # later
    b, c = add_picture_slot(seqK2, revK2); revK2 = b["rev"]; pK2 = b["slots"][-1]["id"]

    b, c = op(seqK2, revK2, "patch", **{"from": vA, "to": vB, "field": VIDEO_FIELD_ID})
    check("K2a: patch {from: earlier video slot, to: later video slot, field: video jack} -> ok (200)",
          c == 200, b)
    cables = b.get("cables") if isinstance(b, dict) else None
    cable_ab = next((cb for cb in (cables or [])
                      if cb.get("from") == vA and cb.get("to") == vB and cb.get("field") == VIDEO_FIELD_ID), None)
    check("K2a: the stored cable has that from/to/field and an id",
          cable_ab is not None and isinstance(cable_ab.get("id"), str) and cable_ab["id"], cables)
    revK2 = b.get("rev", revK2)

    b, c = op(seqK2, revK2, "patch", **{"from": vB, "to": vA, "field": VIDEO_FIELD_ID})
    check('K2b: patch {from: LATER video slot, to: earlier video slot} refused 400 with a '
          '"before" sentence', c == 400 and isinstance(b.get("error"), str) and "before" in b["error"].lower(), b)

    seqK2c, revK2c = new_seq("K2c")
    b, c = add_video_slot(seqK2c, revK2c); revK2c = b["rev"]; vSelf = b["slots"][-1]["id"]
    b, c = op(seqK2c, revK2c, "patch", **{"from": vSelf, "to": vSelf, "field": VIDEO_FIELD_ID})
    check('K2c: patch {from: X, to: X} (continue from itself) refused 400 with a "before" sentence',
          c == 400 and isinstance(b.get("error"), str) and "before" in b["error"].lower(), b)

    seqK2d, revK2d = new_seq("K2d")
    b, c = add_picture_slot(seqK2d, revK2d); revK2d = b["rev"]; pK2d = b["slots"][-1]["id"]
    b, c = add_video_slot(seqK2d, revK2d); revK2d = b["rev"]; vK2d = b["slots"][-1]["id"]
    b, c = op(seqK2d, revK2d, "patch", **{"from": pK2d, "to": vK2d, "field": VIDEO_FIELD_ID})
    check("K2d: patch {from: a PICTURE-lane slot, to: video slot, field: a VIDEO jack} refused 400 "
          "(a video jack's source must be an earlier video shot, not a picture)",
          c == 400 and isinstance(b.get("error"), str) and b["error"].strip(), b)

    b, c = op(seqK2, revK2, "patch", **{"from": pK2, "to": vB, "field": VIDEO_FIELD_ID})
    # (the field already has cable_ab from vA -- this attempt, from anywhere,
    # must be refused as a duplicate regardless of whether it itself would
    # otherwise be valid)
    b, c = op(seqK2, revK2, "patch", **{"from": vA, "to": vB, "field": VIDEO_FIELD_ID})
    check('K2e: a second cable into the already-cabled (to, field) refused 400 '
          '"That input already has a cable. Unplug it first."',
          c == 400 and b.get("error") == "That input already has a cable. Unplug it first.", b)

    if IMAGE_ONLY_MODE is not None:
        seqK2f, revK2f = new_seq("K2f")
        b, c = add_video_slot(seqK2f, revK2f); revK2f = b["rev"]; vSrcK2f = b["slots"][-1]["id"]
        b, c = add_video_slot(seqK2f, revK2f, mode=IMAGE_ONLY_MODE); revK2f = b["rev"]
        vDstK2f = b["slots"][-1]["id"]
        img_field_id = image_fields(IMAGE_ONLY_MODE)[0]["id"]
        b, c = op(seqK2f, revK2f, "patch", **{"from": vSrcK2f, "to": vDstK2f, "field": img_field_id})
        check('K2f: C3.4 unchanged -- patch {from: a VIDEO-lane slot, to: video slot, field: an '
              'IMAGE jack} still refused 400 "A cable starts at a picture."',
              c == 400 and b.get("error") == "A cable starts at a picture.", b)
    else:
        check("K2f: (cannot exercise -- no image-only video mode discovered)", False)

    # =======================================================================
    print("\nK3: generate resolves the continue cable -- unmade source refused, "
          "made+harvested source carries exact bytes + graph wiring + take.inputs.cables, "
          "an unharvested-but-recopyable source still succeeds, a gone source refuses naming it")

    seqK3, revK3 = new_seq("K3")
    b, c = add_video_slot(seqK3, revK3); revK3 = b["rev"]; srcK3 = b["slots"][-1]["id"]   # unpicked
    b, c = add_video_slot(seqK3, revK3); revK3 = b["rev"]; dstK3 = b["slots"][-1]["id"]
    b, c = op(seqK3, revK3, "patch", **{"from": srcK3, "to": dstK3, "field": VIDEO_FIELD_ID})
    revK3 = b.get("rev", revK3)

    control([{"filename": "k3_unmade.mp4", "subfolder": "", "type": "output"}])
    result, gcode = mod.seq_generate({"id": seqK3, "slot_id": dstK3})
    check('K3a: generate with the cabled source unpicked refused 400 '
          '"The shot this continues from is not made yet."',
          gcode == 400 and result.get("error") == "The shot this continues from is not made yet.", result)

    # -- made + genuinely harvested (a real local file already on disk) ----
    seqK3b, revK3b = new_seq("K3b")
    b, c = add_video_slot(seqK3b, revK3b); revK3b = b["rev"]; srcK3b = b["slots"][-1]["id"]
    b, c = add_video_slot(seqK3b, revK3b); revK3b = b["rev"]; dstK3b = b["slots"][-1]["id"]

    JOB_SRC = "k3b_src_job"
    SRC_BYTES = os.urandom(4096)   # opaque bytes -- server logic never inspects video content
    make_video_job(JOB_SRC, "k3b_src.mp4", SRC_BYTES)
    mod.seq_add_take(seqK3b, srcK3b, JOB_SRC)
    cur = mod.seq_get(seqK3b)[0]["rev"]
    b, c = op(seqK3b, cur, "pick_take", slot_id=srcK3b, job_id=JOB_SRC)
    revK3b = b["rev"]

    harvested_path = mod._cut_ensure_take_file(seqK3b, srcK3b, JOB_SRC)
    check("K3b setup: the source take is genuinely harvested to a local file",
          os.path.isfile(harvested_path) and open(harvested_path, "rb").read() == SRC_BYTES, harvested_path)
    # _cut_ensure_take_file writes the take and bumps rev as a side effect --
    # re-fetch the current rev before patching, or patch would 409 on a
    # stale rev and this whole section would silently no-op.
    revK3b = mod.seq_get(seqK3b)[0]["rev"]

    b, c = op(seqK3b, revK3b, "patch", **{"from": srcK3b, "to": dstK3b, "field": VIDEO_FIELD_ID})
    check("K3b setup: the patch itself succeeded (guards against a silent stale-rev no-op)",
          c == 200, b)
    revK3b = b.get("rev", revK3b)

    before_inputs = input_files()
    control([{"filename": "k3b_out.mp4", "subfolder": "", "type": "output"}])
    result_b, gcode_b = mod.seq_generate({"id": seqK3b, "slot_id": dstK3b})
    check("K3b: generate on the patched, harvested-source slot succeeds",
          gcode_b == 200 and result_b.get("ok") is True, result_b)

    after_inputs = input_files()
    new_uploads = after_inputs - before_inputs
    check("K3b: exactly one new file was uploaded to the target lane for the cable",
          len(new_uploads) == 1, sorted(new_uploads))
    uploaded_name = next(iter(new_uploads)) if len(new_uploads) == 1 else None
    uploaded_bytes = None
    if uploaded_name:
        with open(os.path.join(STORE, "inputs", uploaded_name), "rb") as f:
            uploaded_bytes = f.read()
    check("K3b: the uploaded bytes EQUAL the source's harvested mp4 bytes exactly "
          "(no crop, no re-encode)", uploaded_bytes == SRC_BYTES, (uploaded_name,))

    graphK3b = last_prompt()
    wired = False
    if uploaded_name:
        for node in graphK3b.values():
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict) \
                    and uploaded_name in node["inputs"].values():
                wired = True
                break
    check("K3b: the graph submitted to the fake lane wires the uploaded file's name "
          "into some node input (the video field)", wired, (uploaded_name, graphK3b))

    seqK3b_after = mod.seq_get(seqK3b)[0]
    dstK3b_slot = next(s for s in seqK3b_after["slots"] if s["id"] == dstK3b)
    last_take_b = (dstK3b_slot.get("takes") or [])[-1] if dstK3b_slot.get("takes") else {}
    check("K3b: the new take's inputs.cables == {field: source pick job id}",
          last_take_b.get("inputs", {}).get("cables") == {VIDEO_FIELD_ID: JOB_SRC}, last_take_b)

    # -- picked but NOT yet locally harvested, source still fetchable: the
    #    retry (_cut_ensure_take_file, reused) must still succeed ----------
    seqK3c, revK3c = new_seq("K3c")
    b, c = add_video_slot(seqK3c, revK3c); revK3c = b["rev"]; srcK3c = b["slots"][-1]["id"]
    b, c = add_video_slot(seqK3c, revK3c); revK3c = b["rev"]; dstK3c = b["slots"][-1]["id"]

    JOB_SRC_C = "k3c_src_job"
    SRC_BYTES_C = os.urandom(4096)
    make_video_job(JOB_SRC_C, "k3c_src.mp4", SRC_BYTES_C)
    mod.seq_add_take(seqK3c, srcK3c, JOB_SRC_C)   # take.file stays None -- never pre-harvested
    cur = mod.seq_get(seqK3c)[0]["rev"]
    b, c = op(seqK3c, cur, "pick_take", slot_id=srcK3c, job_id=JOB_SRC_C)
    revK3c = b["rev"]
    b, c = op(seqK3c, revK3c, "patch", **{"from": srcK3c, "to": dstK3c, "field": VIDEO_FIELD_ID})
    revK3c = b.get("rev", revK3c)

    took_before = mod.seq_get(seqK3c)[0]
    src_take_before = next(t for t in next(s for s in took_before["slots"] if s["id"] == srcK3c)["takes"]
                            if t["job_id"] == JOB_SRC_C)
    check("K3c setup: the source take's file is null going in (never pre-harvested)",
          src_take_before.get("file") is None, src_take_before)

    control([{"filename": "k3c_out.mp4", "subfolder": "", "type": "output"}])
    result_c, gcode_c = mod.seq_generate({"id": seqK3c, "slot_id": dstK3c})
    check("K3c: generate on a picked-but-unharvested (still fetchable) source still succeeds "
          "(_cut_ensure_take_file's retry is reused at generate time)",
          gcode_c == 200 and result_c.get("ok") is True, result_c)

    # -- picked, and now gone from BOTH the local cache and the source: refused, naming the slot --
    seqK3d, revK3d = new_seq("K3d")
    b, c = add_video_slot(seqK3d, revK3d); revK3d = b["rev"]; srcK3d = b["slots"][-1]["id"]
    b, c = add_video_slot(seqK3d, revK3d); revK3d = b["rev"]; dstK3d = b["slots"][-1]["id"]

    JOB_SRC_D = "k3d_src_job"
    make_video_job(JOB_SRC_D, "k3d_src.mp4", os.urandom(128))
    mod.seq_add_take(seqK3d, srcK3d, JOB_SRC_D)
    cur = mod.seq_get(seqK3d)[0]["rev"]
    b, c = op(seqK3d, cur, "pick_take", slot_id=srcK3d, job_id=JOB_SRC_D)
    revK3d = b["rev"]
    b, c = op(seqK3d, revK3d, "patch", **{"from": srcK3d, "to": dstK3d, "field": VIDEO_FIELD_ID})
    revK3d = b.get("rev", revK3d)
    os.remove(os.path.join(STORE, "outputs", "k3d_src.mp4"))   # the fake lane's /view now 404s it

    control([{"filename": "k3d_out.mp4", "subfolder": "", "type": "output"}])
    result_d, gcode_d = mod.seq_generate({"id": seqK3d, "slot_id": dstK3d})
    expected_d = "Shot %s's take is not copied yet — is its lane off?" % srcK3d
    check("K3d: generate with the source picked but its file missing everywhere and not "
          "re-copyable refused 400, naming the source's own slot (_cut_ensure_take_file's "
          "own sentence)",
          gcode_d == 400 and result_d.get("error") == expected_d, result_d)

    # =======================================================================
    print("\nK4: re-picking the cable source makes the target STALE "
          "('previous shot changed'); a new target take clears it")

    seqK4, revK4 = new_seq("K4")
    b, c = add_video_slot(seqK4, revK4); revK4 = b["rev"]; srcK4 = b["slots"][-1]["id"]
    b, c = add_video_slot(seqK4, revK4); revK4 = b["rev"]; dstK4 = b["slots"][-1]["id"]

    JOB_OLD, JOB_NEW = "k4_old_job", "k4_new_job"
    with mod.JOBS_LOCK:
        mod.JOBS[JOB_OLD] = {"id": JOB_OLD, "lane": "t", "kind": "video", "status": "done", "outputs": []}
        mod.JOBS[JOB_NEW] = {"id": JOB_NEW, "lane": "t", "kind": "video", "status": "done", "outputs": []}
    mod.seq_add_take(seqK4, srcK4, JOB_OLD)
    cur = mod.seq_get(seqK4)[0]["rev"]
    b, c = op(seqK4, cur, "pick_take", slot_id=srcK4, job_id=JOB_OLD)
    revK4 = b["rev"]

    VJOB_A = "k4_dst_a"
    with mod.JOBS_LOCK:
        mod.JOBS[VJOB_A] = {"id": VJOB_A, "lane": "t", "kind": "video", "status": "done", "outputs": []}
    mod.seq_add_take(seqK4, dstK4, VJOB_A, inputs={"refs": [], "cables": {VIDEO_FIELD_ID: JOB_OLD}})
    cur = mod.seq_get(seqK4)[0]["rev"]
    b, c = op(seqK4, cur, "pick_take", slot_id=dstK4, job_id=VJOB_A)
    revK4 = b["rev"]

    raw4 = raw_seq(seqK4)
    raw4["cables"] = [{"id": "cK4", "from": srcK4, "to": dstK4, "field": VIDEO_FIELD_ID}]
    write_raw_seq(seqK4, raw4)

    mod.seq_add_take(seqK4, srcK4, JOB_NEW)
    cur = mod.seq_get(seqK4)[0]["rev"]
    b, c = op(seqK4, cur, "pick_take", slot_id=srcK4, job_id=JOB_NEW)
    revK4 = b["rev"]

    fetchedK4 = mod.seq_get(seqK4)[0]
    dstK4_slot = next(s for s in fetchedK4["slots"] if s["id"] == dstK4)
    expected_reason = "%s changed" % VIDEO_FIELD_LABEL.lower()
    check('K4a: the field label lower-cased + " changed" == the commission\'s literal wording '
          '"previous shot changed" (i.e. the pack must label the field "Previous shot")',
          expected_reason == "previous shot changed", expected_reason)
    check('K4b: re-picking the cable source shows "%s" in the target\'s stale list' % expected_reason,
          expected_reason in (dstK4_slot.get("stale") or []), dstK4_slot.get("stale"))

    VJOB_B = "k4_dst_b"
    with mod.JOBS_LOCK:
        mod.JOBS[VJOB_B] = {"id": VJOB_B, "lane": "t", "kind": "video", "status": "done", "outputs": []}
    mod.seq_add_take(seqK4, dstK4, VJOB_B, inputs={"refs": [], "cables": {VIDEO_FIELD_ID: JOB_NEW}})
    cur = mod.seq_get(seqK4)[0]["rev"]
    b, c = op(seqK4, cur, "pick_take", slot_id=dstK4, job_id=VJOB_B)

    fetchedK4b = mod.seq_get(seqK4)[0]
    dstK4_slot2 = next(s for s in fetchedK4b["slots"] if s["id"] == dstK4)
    check('K4c: a new take on the target clears "%s"' % expected_reason,
          expected_reason not in (dstK4_slot2.get("stale") or []), dstK4_slot2.get("stale"))

    # =======================================================================
    print("\nK6: the mode picker line for the continue mode names what it does")

    note = engines.mode_note("video", VIDEO_FIELD_MODE)
    check('K6: engines.mode_note("video", %r) (the /api/engines "note" the picker renders as '
          'erow-reason) contains "continues from an earlier shot"' % VIDEO_FIELD_MODE,
          isinstance(note, str) and "continues from an earlier shot" in note, note)

else:
    # No pack declares a field of type "video" on this tree yet -- every
    # individual rule that would have been exercised above FAILs explicitly
    # by name, rather than being silently swallowed by one blanket line.
    for rule_id in ("K1: slot_jacks lists the video field, typed",
                     "K1: image-only mode lists only image jacks",
                     "K1: a picture-lane slot has no jacks",
                     "K2a: patch from an earlier video slot -> ok",
                     "K2b: patch from a later video slot -> refused (before)",
                     "K2c: patch from itself -> refused (before)",
                     "K2d: patch from a picture slot into a video jack -> refused",
                     "K2e: a second cable into the same (to, field) -> refused",
                     "K2f: C3.4 image-cable behaviour unchanged",
                     "K3a: generate with an unpicked source -> refused",
                     "K3b: generate with a harvested source -> bytes/graph/inputs.cables",
                     "K3c: generate with an unharvested-but-recopyable source -> ok",
                     "K3d: generate with a gone source -> refused, naming it",
                     "K4a/b/c: staleness on re-pick, cleared by a new take",
                     "K6: the mode picker line names what it does"):
        check("%s (cannot exercise -- K5's sanity check above already failed this run)" % rule_id, False)


# ===========================================================================
print("\nK7: the engine-independence ratchet script exits 0")

rc = subprocess.run(["bash", os.path.join(ROOT, "scripts", "check-engine-independence.sh")],
                     cwd=ROOT, capture_output=True, text=True)
check("K7: scripts/check-engine-independence.sh exits 0", rc.returncode == 0,
      (rc.returncode, rc.stdout[-800:], rc.stderr[-800:]))


# ---------------------------------------------------------------------------
FAKE_T.terminate()
FAKE_T.wait(timeout=10)
shutil.rmtree(SCRATCH, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
