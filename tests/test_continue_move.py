"""Regression gate: moving a shot must not leave a "continue" cable pointing
the wrong way (review finding, orchestrator pass on C3.4b, 2026-09-23).

_op_move_slot (server.py) reordered `seq["slots"]` without re-checking any
video-typed ("continue") cable against the new order, and resolve_slot_cables
never checks order either -- so a move could leave a shot "continuing" from a
LATER shot, or even a mutual A->B / B->A pair, silently. This file was run
against the pre-fix tree first (server.py before _drop_stale_video_cables
existed) to confirm every check below actually failed there -- RED -- before
being run against the fix.

Fix (server.py): a shared `_drop_stale_video_cables(seq)`, called from
_op_move_slot after it reorders `seq["slots"]`, drops any video-typed cable
whose `from` is no longer earlier than its `to` in the video lane's own
order (K2's own invariant, re-checked). It returns the dropped cables as
[{from, to}]; seq_op merges that into the op's response body as
`removed_cables` when non-empty, and index.html's seqOp() surfaces it via
msgSeq (plain text, the same line every other op result uses). Image cables
never depend on slot order and are untouched.

Owner ruling (same review pass): no cascade. C's inputs are B's picked
take, which a move does not change, so C does not go stale just because B
moved -- only re-rendering/re-picking B does that (existing K4 machinery).
Not tested here; nothing to prove.

ISOLATION: same model as tests/test_continue.py -- one fake ComfyUI lane
("t") on a closed local port, server.py imported in-process against a fresh
tempfile.mkdtemp() data dir. The real data/ tree and the live app are never
touched.

Run: python3 tests/test_continue_move.py
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
# One fake lane ("t"), same fixture as test_continue.py.
# ---------------------------------------------------------------------------

SCRATCH = tempfile.mkdtemp(prefix="bwf_continue_move_")
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
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "c34b move test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": PORT_T,
                      "caps": ["image", "video"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG

MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
for _role in engines.model_keys():
    if not MODELS.get(_role):
        MODELS[_role] = "stub_%s.safetensors" % _role

DATA_DIR = os.path.join(SCRATCH, "data_main")


def load_server(data_dir, name):
    os.environ["GENCENTER_DATA"] = data_dir
    spec = importlib.util.spec_from_file_location("srv_continue_move_%s" % name, os.path.join(ROOT, "server.py"))
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
    req = urllib.request.Request(
        "http://127.0.0.1:%d/_control/accept" % PORT_T,
        data=json.dumps({"outputs": outputs}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def new_seq(title):
    seq, _ = mod.seq_create({"title": title, "mode": "sequence"})
    return seq["id"], seq["rev"]


# ---------------------------------------------------------------------------
# Discovery (never hardcoded), same as test_continue.py.
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

check("sanity: some installed video-lane pack declares a field of type \"video\" "
      "(the continue mode) -- without this NOTHING below can be exercised",
      VIDEO_FIELD_MODE is not None, VIDEO_FIELD_MODE)

VIDEO_FIELD_ID = VIDEO_FIELD["id"] if VIDEO_FIELD else None

IMAGE_ONLY_MODE = None
for m in engines.modes_for("video"):
    if m == VIDEO_FIELD_MODE:
        continue
    if image_fields(m) and not video_fields(m):
        IMAGE_ONLY_MODE = m
        break
check("sanity: some video-lane mode declares an image field and NO video field "
      "(the image-cable target for the 'unaffected' check)", IMAGE_ONLY_MODE is not None, IMAGE_ONLY_MODE)

PICTURE_MODE = "t2i"


def default_video_values(mode):
    field_ids = {f["id"] for f in engines.fields("video", mode)}
    values = {f["id"]: f["default"] for f in engines.fields("video", mode) if f.get("default") is not None}
    for text_field in ("prompt", "line"):
        if text_field in field_ids:
            values[text_field] = "a scene"
            break
    return values


def add_video_slot(sid, rev, mode):
    kw = dict(lane="video", cap="video", mode=mode, values=default_video_values(mode))
    return op(sid, rev, "add_slot", **kw)


def add_picture_slot(sid, rev):
    return op(sid, rev, "add_slot", lane="picture", cap="image", mode=PICTURE_MODE, values={"prompt": "a person"})


if VIDEO_FIELD_MODE is not None and IMAGE_ONLY_MODE is not None:
    # =======================================================================
    print("\nsetup: pA --(image cable)--> vC ; vA --(continue cable)--> vB, "
          "slots in order [pA, vC, vA, vB]")

    sid, rev = new_seq("move")
    b, c = add_picture_slot(sid, rev); rev = b["rev"]; pA = b["slots"][-1]["id"]
    b, c = add_video_slot(sid, rev, IMAGE_ONLY_MODE); rev = b["rev"]; vC = b["slots"][-1]["id"]
    b, c = add_video_slot(sid, rev, VIDEO_FIELD_MODE); rev = b["rev"]; vA = b["slots"][-1]["id"]
    b, c = add_video_slot(sid, rev, VIDEO_FIELD_MODE); rev = b["rev"]; vB = b["slots"][-1]["id"]

    img_field_id = image_fields(IMAGE_ONLY_MODE)[0]["id"]
    b, c = op(sid, rev, "patch", **{"from": pA, "to": vC, "field": img_field_id})
    check("setup: image cable pA->vC patched", c == 200, b)
    rev = b.get("rev", rev)

    b, c = op(sid, rev, "patch", **{"from": vA, "to": vB, "field": VIDEO_FIELD_ID})
    check("setup: continue cable vA->vB patched (vA earlier than vB)", c == 200, b)
    rev = b.get("rev", rev)

    order_before = [s["id"] for s in mod.seq_get(sid)[0]["slots"]]
    check("setup: slot order is [pA, vC, vA, vB]", order_before == [pA, vC, vA, vB], order_before)

    # =======================================================================
    print("\nmove vA to AFTER vB -> the continue cable is dropped, named in "
          "the response, generate on vB no longer resolves it, the image "
          "cable is untouched")

    b, c = op(sid, rev, "move_slot", slot_id=vA, to=3)
    check("move vA after vB -> op accepted (200)", c == 200, b)
    rev = b.get("rev", rev)

    order_after = [s["id"] for s in b.get("slots") or []]
    check("slot order is now [pA, vC, vB, vA]", order_after == [pA, vC, vB, vA], order_after)

    cables_after = b.get("cables") or []
    cable_va_vb = next((cb for cb in cables_after if cb.get("from") == vA and cb.get("to") == vB), None)
    check("the continue cable vA->vB no longer exists after the move", cable_va_vb is None, cables_after)

    cable_pa_vc = next((cb for cb in cables_after
                         if cb.get("from") == pA and cb.get("to") == vC and cb.get("field") == img_field_id), None)
    check("the image cable pA->vC is UNAFFECTED by the move", cable_pa_vc is not None, cables_after)

    removed = b.get("removed_cables")
    check("the op's own response names the removed cable: removed_cables == [{from: vA, to: vB}]",
          removed == [{"from": vA, "to": vB}], removed)

    control([{"filename": "vb_after_move.mp4", "subfolder": "", "type": "output"}])
    result_b, gcode_b = mod.seq_generate({"id": sid, "slot_id": vB})
    check("generate on vB after the move succeeds without ever resolving the removed cable "
          "(vA, the old source, was never made -- this would have refused if the cable still lived)",
          gcode_b == 200 and result_b.get("ok") is True, result_b)

    fetched = mod.seq_get(sid)[0]
    vb_slot = next(s for s in fetched["slots"] if s["id"] == vB)
    last_take = (vb_slot.get("takes") or [])[-1] if vb_slot.get("takes") else {}
    check("vB's new take carries no cable use for the continue field (nothing was resolved)",
          VIDEO_FIELD_ID not in (last_take.get("inputs", {}).get("cables") or {}), last_take)

    # =======================================================================
    print("\nmove vA back to before vB -> no cable is recreated automatically")

    fetched2 = mod.seq_get(sid)[0]
    rev = fetched2["rev"]
    order_pre_back = [s["id"] for s in fetched2["slots"]]
    back_to = order_pre_back.index(vB)   # vA's own original slot, right before vB
    b, c = op(sid, rev, "move_slot", slot_id=vA, to=back_to)
    check("move vA back before vB -> op accepted (200)", c == 200, b)
    rev = b.get("rev", rev)

    order_back = [s["id"] for s in b.get("slots") or []]
    check("slot order is [pA, vC, vA, vB] again", order_back == [pA, vC, vA, vB], order_back)

    cables_back = b.get("cables") or []
    cable_recreated = next((cb for cb in cables_back if cb.get("from") == vA and cb.get("to") == vB), None)
    check("moving vA back does NOT recreate the continue cable automatically", cable_recreated is None, cables_back)

    cable_pa_vc_still = next((cb for cb in cables_back
                               if cb.get("from") == pA and cb.get("to") == vC and cb.get("field") == img_field_id), None)
    check("the image cable pA->vC is still there after both moves", cable_pa_vc_still is not None, cables_back)

    check("moving vA back reports no removed_cables (there was nothing left to drop)",
          not b.get("removed_cables"), b.get("removed_cables"))

else:
    for rule_id in ("setup: image cable pA->vC patched",
                     "setup: continue cable vA->vB patched",
                     "move vA after vB -> the continue cable is dropped",
                     "the image cable pA->vC is UNAFFECTED by the move",
                     "the op's own response names the removed cable",
                     "generate on vB after the move succeeds",
                     "move vA back -> no cable is recreated automatically"):
        check("%s (cannot exercise -- sanity check above already failed this run)" % rule_id, False)


# ---------------------------------------------------------------------------
FAKE_T.terminate()
FAKE_T.wait(timeout=10)
shutil.rmtree(SCRATCH, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
