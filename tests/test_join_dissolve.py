"""Acceptance gate for E2 "join: dissolve", gates 1-2 of the build spec:

  1. the continue graph WITH a previous clip and the pack's join field on has
     no MotionContextTrim, and CreateVideo reads the untrimmed decoded images
     and audio; with the field off, or with no previous clip, the graph equals
     the pre-E2 builder's own output (tests/golden/h3_join_base.json, captured
     from the 57506b1 builder);
  2. the take records inputs.join = {dissolve, overlap} only when the render
     kept the overlap (field on AND cabled). The field is off by default, so a
     Video room / API continue with it unset renders today's trimmed graph and
     its job has no `join`; a SEQUENCE shot that never set it takes the pack's
     `sequence_default` (on), and the sequence says so (slot.seq_defaults).

The mode, its join field and its overlap come from the pack's own `joins`
declaration, never a hardcoded name. server.py in-process against a fake
ComfyUI lane and a fresh scratch data dir.

Run: python3 tests/test_join_dissolve.py
"""
import copy, importlib.util, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request
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


JOINS = {}
for pack in engines.packs():
    if pack["cap"] == "video":
        JOINS.update(pack.get("joins") or {})
MODE = next(iter(JOINS), None)
SPEC = JOINS.get(MODE) or {}
FIELD = SPEC.get("field")
check("sanity: a video pack declares joins {mode: {field, overlap_frames}}",
      bool(MODE and FIELD and SPEC.get("overlap_frames")), JOINS)
if not (MODE and FIELD):
    print("\nFAILED: %d -- %s" % (len(FAILED), FAILED))
    sys.exit(1)
FDECL = next((f for f in engines.fields("video", MODE) if f["id"] == FIELD), {})
check("the join field is a checkbox, OFF by default (a Video room clip stays trimmed)",
      FDECL.get("type") == "checkbox" and FDECL.get("default") is False, FDECL)
check("...and the pack declares the Cutting Room's own default: on (sequence_default)",
      SPEC.get("sequence_default") is True, SPEC)

# ---------------------------------------------------------------------------
print("\ngate 1: the graph keeps the overlap only with the field on AND a previous clip")
GOLDEN = json.load(open(os.path.join(HERE, "golden", "h3_join_base.json")))
M = GOLDEN["models"]


def build(args):
    return engines.graph_for("video", MODE, copy.deepcopy(args), M)


def of_class(g, cls):
    return [k for k, n in g.items() if n["class_type"] == cls]


for case, rec in sorted(GOLDEN["cases"].items()):
    args, base = rec["args"], rec["graph"]
    check("%s: field absent == base builder output" % case, build(args) == base)
    check("%s: field off == base builder output" % case, build(dict(args, **{FIELD: False})) == base)
    on = build(dict(args, **{FIELD: True}))
    if not args.get("prev_video"):
        check("%s: field on but no previous clip == base builder output" % case, on == base)
        continue
    check("%s: field on + previous clip -> no MotionContextTrim" % case,
          not of_class(on, "MiniMaxH3MotionContextTrim") and of_class(base, "MiniMaxH3MotionContextTrim"))
    cv = on[of_class(on, "CreateVideo")[0]]["inputs"]
    dec_v, dec_a = of_class(on, "VAEDecode"), of_class(on, "VAEDecodeAudio")
    check("%s: CreateVideo reads the untrimmed decoded images/audio (%r/%r)" % (case, cv.get("images"), cv.get("audio")),
          cv.get("images") == [dec_v[0], 0] and cv.get("audio") == [dec_a[0], 0] and cv["images"] == ["10", 0]
          and cv["audio"] == ["23", 0])
    exp = copy.deepcopy(base)
    trim = of_class(exp, "MiniMaxH3MotionContextTrim")[0]
    del exp[trim]
    exp[of_class(exp, "CreateVideo")[0]]["inputs"].update(images=["10", 0], audio=["23", 0])
    check("%s: ...and nothing else changes (base minus the Trim, CreateVideo rewired)" % case, on == exp)
    ctx = on[of_class(on, "MiniMaxH3MotionContext")[0]]["inputs"]
    check("%s: the Motion-Context still carries %d frames" % (case, SPEC["overlap_frames"]),
          int(ctx["context_length"]) == SPEC["overlap_frames"])

# ---------------------------------------------------------------------------
SCRATCH = tempfile.mkdtemp(prefix="bwf_join_")
print("\nscratch dir: %s (real data/ is never written)" % SCRATCH)
STORE = os.path.join(SCRATCH, "store_t")
os.makedirs(os.path.join(STORE, "outputs"))
PORT_T = free_port()
FAKE_T = subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
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
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "e2 test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": PORT_T,
                      "caps": ["image", "video", "audio"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
DATA = os.path.join(SCRATCH, "data")
os.environ["GENCENTER_DATA"] = DATA
MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
for _role in engines.model_keys():
    if not MODELS.get(_role):
        MODELS[_role] = "stub_%s.safetensors" % _role
spec = importlib.util.spec_from_file_location("srv_join", os.path.join(ROOT, "server.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.JOBS_FILE = os.path.join(DATA, "jobs.json")
mod.SEQ_DIR = os.path.join(DATA, "sequences")
mod.SEQ_MEDIA_DIR = os.path.join(DATA, "seq")
mod.CHAIN_DIR = os.path.join(DATA, "chain")
mod.LOCAL_OUTPUTS_DIR = os.path.join(DATA, "outputs")
for d in (mod.SEQ_DIR, mod.SEQ_MEDIA_DIR, mod.CHAIN_DIR, mod.LOCAL_OUTPUTS_DIR):
    os.makedirs(d, exist_ok=True)
mod.LANE_BY_ID["t"]["models"] = dict(MODELS)
with mod.STATE_LOCK:
    mod.LANE_STATE["t"] = {"up": True, "checked": time.time(), "err": ""}
JACK = next((f["id"] for f in engines.fields("video", MODE) if f.get("type") == "video"), None)


def op(sid, name, **kw):
    return mod.seq_op(dict(kw, id=sid, rev=mod.seq_get(sid)[0]["rev"], op=name))


def slot_of(sid, slot_id):
    return next(s for s in mod.seq_get(sid)[0]["slots"] if s["id"] == slot_id)


def control(outputs):
    req = urllib.request.Request("http://127.0.0.1:%d/_control/accept" % PORT_T,
                                 data=json.dumps({"outputs": outputs}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def last_prompt():
    with open(os.path.join(STORE, "last_prompt.json")) as f:
        return json.load(f)


def add(sid, join=None, cable_from=None):
    values = {f["id"]: f["default"] for f in engines.fields("video", MODE) if f.get("default") is not None}
    values.update(prompt="the car keeps driving", length=124)
    if join is None:
        values.pop(FIELD, None)
    else:
        values[FIELD] = join
    b, c = op(sid, "add_slot", lane="video", cap="video", mode=MODE, values=values)
    assert c == 200, b
    v = b["added_slot_id"]
    if cable_from:
        b, c = op(sid, "patch", **{"from": cable_from, "to": v, "field": JACK})
        assert c == 200, b
    return v


def make(sid, v, fname):
    with open(os.path.join(STORE, "outputs", fname), "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(512))
    control([{"filename": fname, "subfolder": "", "type": "output"}])
    body, code = mod.seq_generate({"id": sid, "slot_id": v})
    assert code == 200, body
    jid = body["job"]["id"]
    with mod.JOBS_LOCK:
        mod.JOBS[jid].update(status="done", outputs=[{"filename": fname, "subfolder": "", "type": "output",
                                                      "media": "video"}])
    b, c = op(sid, "pick_take", slot_id=v, job_id=jid)
    assert c == 200, b
    take = next(t for t in slot_of(sid, v)["takes"] if t["job_id"] == jid)
    return take, last_prompt()


def trims(g):
    return [n for n in g.values() if n["class_type"] == "MiniMaxH3MotionContextTrim"]


print("\ngate 2: the take records inputs.join only when the overlap was kept")
seq, _ = mod.seq_create({"title": "joins", "mode": "sequence"})
sid = seq["id"]
head = add(sid, join=True)
t_head, g_head = make(sid, head, "head.mp4")
check("a chain head with the field on (nothing cabled): no inputs.join", "join" not in t_head["inputs"], t_head)
check("...and its graph renders plain (no Motion-Context)",
      not any(n["class_type"] == "MiniMaxH3MotionContext" for n in g_head.values()))
on = add(sid, join=None, cable_from=head)
t_on, g_on = make(sid, on, "on.mp4")
check("a cabled shot with the field unset (default on): inputs.join = {dissolve: true, overlap: %d}"
      % SPEC["overlap_frames"], t_on["inputs"].get("join") == {"dissolve": True, "overlap": SPEC["overlap_frames"]},
      t_on["inputs"])
check("...and the lane got the untrimmed graph", not trims(g_on)
      and any(n["class_type"] == "MiniMaxH3MotionContext" for n in g_on.values()))
on2 = add(sid, join=True, cable_from=on)
t_on2, g_on2 = make(sid, on2, "on2.mp4")
check("a cabled shot with the field ticked: inputs.join recorded",
      t_on2["inputs"].get("join") == {"dissolve": True, "overlap": SPEC["overlap_frames"]}, t_on2["inputs"])
off = add(sid, join=False, cable_from=on2)
t_off, g_off = make(sid, off, "off.mp4")
check("a cabled shot with the field off: no inputs.join", "join" not in t_off["inputs"], t_off["inputs"])
check("...and the lane got today's trimmed graph", len(trims(g_off)) == 1)
check("the other records are unchanged (refs + cables, the cable naming the source take)",
      set(t_off["inputs"]) == {"refs", "cables"} and t_off["inputs"]["cables"].get(JACK) == slot_of(sid, on2)["pick"],
      t_off["inputs"])

check("the sequence tells the page what an unset field means on these shots: seq_defaults {%s: true}" % FIELD,
      slot_of(sid, on).get("seq_defaults") == {FIELD: True} and "seq_defaults" not in slot_of(sid, on).get("values", {}),
      slot_of(sid, on).get("seq_defaults"))

print("\nVideo room / API: a continue with the field unset (or off) renders today's trimmed graph, no join")
for label, extra in (("unset", {}), ("off, as the Video room form sends it", {FIELD: False})):
    with open(os.path.join(STORE, "outputs", "room.mp4"), "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(64))
    control([{"filename": "room.mp4", "subfolder": "", "type": "output"}])
    body, code = mod.generate(dict({"lane": "t", "kind": "video", "mode": MODE, "prompt": "the car keeps driving",
                                    "length": 243, "steps": 8, "prev_video": "prev.mp4"}, **extra))
    g = last_prompt()
    with mod.JOBS_LOCK:
        job = dict(mod.JOBS.get((body.get("job") or {}).get("id"), {}))
    check("%s: the render keeps today's MotionContextTrim, CreateVideo reads it" % label,
          code == 200 and len(trims(g)) == 1
          and next(n for n in g.values() if n["class_type"] == "CreateVideo")["inputs"]["images"][0]
          == next(k for k, n in g.items() if n["class_type"] == "MiniMaxH3MotionContextTrim"), (code, body))
    check("%s: the job records no join" % label, job and "join" not in job, job)

print("\nthe job's own record, so a take adopted from History keeps it")
jid_on = slot_of(sid, on)["pick"]
with mod.JOBS_LOCK:
    job_join = mod.JOBS[jid_on].get("join")
check("the job made with the overlap records join too", job_join == {"dissolve": True, "overlap": SPEC["overlap_frames"]},
      job_join)
jid_off = slot_of(sid, off)["pick"]
with mod.JOBS_LOCK:
    off_job = dict(mod.JOBS[jid_off])
check("a job made without it does not", "join" not in off_job)
seq2, _ = mod.seq_create({"title": "adopt", "mode": "sequence"})
a = add(seq2["id"], join=True)
b, c = op(seq2["id"], "adopt_take", slot_id=a, job_id=jid_on)
tk = next(t for t in slot_of(seq2["id"], a)["takes"] if t["job_id"] == jid_on)
check("adopting it carries the job's join record into the take", c == 200 and tk["inputs"].get("join") == job_join,
      (b, tk))

FAKE_T.terminate()
shutil.rmtree(SCRATCH, ignore_errors=True)
print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
