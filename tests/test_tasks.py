"""Gate for the front door's task model (engines.tasks()).

Pins tasks.json (core config: task words only) merged with each pack's
`mode_tasks`:
1. the declared mapping: every built-in mode lands in exactly one task, as a way
   or as someone's final;
2. an undeclared mode, and a mode naming a task tasks.json lacks, each become a
   task of their own (a stranger's pack shows up); a task no mode fills keeps
   `ways: []`;
3. `accepts` is derived from primary upload fields, never declared;
4. draft/final pairing, incl. `pictures`; a bad `final_of` is a plain way + a warning;
5. built-in packs sort before data packs inside a task;
6. a missing or broken tasks.json falls back to per-mode tasks;
7. data-pack manifest keys task/way/tier/final_of/pictures/blurb are validated
   and mapped, and the per-mode `blurb` never shows a data pack's note;
8. tasks.json names no engine and no plumbing word; /api/engines carries `tasks`.

Run: python3 tests/test_tasks.py   (scripts/run-tests.sh puts scratch on disk)
"""
import json
import logging
import os
import re
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)

WORK = tempfile.mkdtemp(prefix="bwf-tasks-")
os.environ["GENCENTER_DATA"] = WORK          # no data packs: the built-ins alone
import engines
import engines._userpacks as up


def rescan(data):
    os.environ["GENCENTER_DATA"] = data
    engines._PACKS = None
    return engines.packs()


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []
    def emit(self, record):
        self.lines.append(record.getMessage())


def with_log(fn):
    h = Capture()
    lg = logging.getLogger("engines")
    lg.addHandler(h)
    try:
        return fn(), h.lines
    finally:
        lg.removeHandler(h)


def modes_of(task):
    out = []
    for w in task["ways"]:
        out.append((w["cap"], w["mode"]))
        if w["final"]:
            out.append((w["final"]["cap"], w["final"]["mode"]))
        out += [(f["cap"], f["mode"]) for f in w["finals_by_count"].values()]
    return out


ALL_MODES = [(cap, mode) for cap in engines.caps() for mode in engines.modes_for(cap)]
TASKS = engines.tasks()
BY = {t["id"]: t for t in TASKS}

print("tasks.json: the 21 tasks, task words only")
RAW = json.load(open(os.path.join(ROOT, "tasks.json"), encoding="utf-8"))
ROOMS_RAW = json.load(open(os.path.join(ROOT, "rooms.json"), encoding="utf-8"))
room_ids = {r["id"] for r in ROOMS_RAW}
groups = {r["group"] for r in ROOMS_RAW if r.get("group")}
check("21 tasks (none that only a private pack fills)", len(RAW) == 21 and not {"swap", "sharpen"} & {t["id"] for t in RAW}, len(RAW))
check("ids unique", len({t["id"] for t in RAW}) == len(RAW))
check("orders ascend", [t["order"] for t in RAW] == sorted(t["order"] for t in RAW))
WORDS = {"id", "verb", "short", "line", "group", "order", "room"}
check("only the task words (and musicvideo's action) are in the file",
      all(set(t) == WORDS | ({"action"} if t["id"] == "musicvideo" else set()) for t in RAW), [sorted(t) for t in RAW if set(t) - WORDS][:2])
check("musicvideo is an app action, room cutting", next(t for t in RAW if t["id"] == "musicvideo").get("action") == "music-video"
      and next(t for t in RAW if t["id"] == "musicvideo")["room"] == "cutting")
check("the action passes through tasks()", BY["musicvideo"].get("action") == "music-video" and BY["musicvideo"]["ways"] == []
      and "action" not in BY["t2i"])
check("every group is one of rooms.json's groups", all(t["group"] in groups for t in RAW), [t["id"] for t in RAW if t["group"] not in groups])
check("every room is a rooms.json room", all(t["room"] in room_ids for t in RAW), [t["id"] for t in RAW if t["room"] not in room_ids])
check("the real packs add no fallback task", [t["id"] for t in TASKS] == [t["id"] for t in RAW], [t["id"] for t in TASKS][len(RAW):])

print()
print("every built-in mode lands in exactly one task")
placed = {}
for t in TASKS:
    for key in modes_of(t):
        placed.setdefault(key, []).append(t["id"])
check("the real packs declare modes", len(ALL_MODES) > 0)
for cap, mode in ALL_MODES:
    check("%s/%s is in exactly one task" % (cap, mode), len(placed.get((cap, mode), [])) == 1, placed.get((cap, mode)))
check("no task lists a mode no pack provides", set(placed) <= set(ALL_MODES), set(placed) - set(ALL_MODES))

print()
print("the declared mapping (which modes each task holds today)")
WANT = {
    "song": ["song", "yue2"], "music": ["music"], "cover": ["cover"], "sfx": ["sfx"], "beats": ["grid"],
    "rework": ["fit", "arrange", "mix"], "t2i": ["t2i"], "edit": ["edit"], "charsheet": ["charsheet"],
    "pixelart": ["pixelart"], "cutout": ["cutout"], "upscale": ["upscale"], "video": ["ltx", "ltx_loop"],
    "animate": ["fl2va"], "people": ["ref2v"], "continue": ["continue"], "talking": ["talking"],
    "mesh": ["mesh"], "turntable": ["turntable"],
}
for tid, want in WANT.items():
    got = [w["mode"] for w in BY.get(tid, {}).get("ways") or []]
    check("%s ways == %r" % (tid, want), got == want, got)
for tid in ("textures", "musicvideo"):
    check("%s has ways == [] (nothing installed makes it)" % tid, BY[tid]["ways"] == [], BY[tid]["ways"])
check("a way carries a label", all(w["label"] for t in TASKS for w in t["ways"]))
check("song's second way is labelled by the pack", BY["song"]["ways"][1]["label"] == "Melody planned first")

print()
print("accepts is derived from primary upload fields")
check("cover accepts a song", BY["cover"]["accepts"] == ["song"], BY["cover"]["accepts"])
check("cutout accepts a picture", BY["cutout"]["accepts"] == ["picture"])
check("turntable accepts a model", BY["turntable"]["accepts"] == ["model"])
check("a words-only task accepts nothing", BY["t2i"]["accepts"] == [] and BY["song"]["accepts"] == [])
check("people accepts pictures and clips, in that order", BY["people"]["accepts"] == ["picture", "clip"], BY["people"]["accepts"])
check("continue accepts a clip", BY["continue"]["accepts"] == ["clip"])

print()
print("fallback tasks, proved on a SYNTHETIC pack (no entry, unknown task, advanced-only upload)")
SYNTH = {
    "id": "zz-synthetic", "cap": "image", "roles": {}, "provides": {}, "words": {},
    "graphs": {"bare": lambda a, m: {}, "odd": lambda a, m: {}, "hasfile": lambda a, m: {}, "worded": lambda a, m: {}},
    "describe": lambda m: "",
    "mode_words": {"bare": "A bare thing", "odd": "An odd thing"},
    "mode_rooms": {"odd": "picture"},
    "mode_notes": {"bare": "makes a bare thing"},
    "mode_task_words": {"worded": {"verb": "Word it", "short": "Word", "line": "A built-in's own words.", "group": "OBJECT"}},
    "mode_tasks": {"worded": {"task": "syn-worded"}, "odd": {"task": "no-such-task", "way": "Odd", "tier": "final", "final_of": "bare"},
                   "hasfile": {"task": "edit", "way": "Has a file"}},
    "fields": {"hasfile": [{"id": "a", "label": "A", "type": "audio", "tier": "advanced"},
                           {"id": "b", "label": "B", "type": "video_list", "tier": "primary"}]},
}
def _lone(cap, mode):
    return {"id": "zz-lone-" + cap, "cap": cap, "roles": {}, "provides": {}, "words": {},
            "graphs": {mode: lambda a, m: {}}, "describe": lambda m: "", "mode_words": {mode: "Lone " + cap}}
real = engines._discover()
engines._PACKS = real + [SYNTH, _lone("video", "vlone"), _lone("zzcap", "zlone")]
try:
    ft = engines.tasks()
finally:
    engines._PACKS = real
fby = {t["id"]: t for t in ft}
bare = fby.get("image-bare")
check("no mode_tasks entry -> task id <cap>-<mode>", bare is not None, [t["id"] for t in ft][len(RAW):])
if bare:
    check("  verb is the mode's label", bare["verb"] == "A bare thing", bare["verb"])
    check("  line is the mode's blurb (its mode_notes)", bare["line"] == "makes a bare thing", bare["line"])
    check("  no known room: grouped with the rooms.json room that already holds a picture mode", bare["group"] == "PICTURE", bare["group"])
    check("  ordered 1000+", bare["order"] >= 1000, bare["order"])
    check("  holds exactly that mode, labelled by the mode", [(w["mode"], w["label"]) for w in bare["ways"]] == [("bare", "A bare thing")],
          bare["ways"])
check("an unknown-room video mode joins the group that already holds video (no second VIDEO group)",
      fby.get("video-vlone", {}).get("group") == "MOTION", fby.get("video-vlone"))
check("a cap no rooms.json room holds keeps its cap word, upper-cased",
      fby.get("zzcap-zlone", {}).get("group") == engines.cap_word("zzcap").upper(), fby.get("zzcap-zlone"))
odd = fby.get("image-odd")
check("a task id tasks.json lacks -> a task of its own", odd is not None)
if odd:
    check("  group comes from the room mode_rooms names", odd["group"] == "PICTURE" and odd["room"] == "picture", (odd["group"], odd["room"]))
    check("  its final_of cannot pair across tasks: it stays a plain way", odd["ways"][0]["mode"] == "odd" and odd["ways"][0]["final"] is None)
check("a built-in's mode_task_words create its task", fby.get("syn-worded", {}).get("group") == "OBJECT"
      and fby["syn-worded"]["room"] == "3d" and [w["mode"] for w in fby["syn-worded"]["ways"]] == ["worded"], fby.get("syn-worded"))
check("fallback tasks sort after the declared ones", [t["id"] for t in ft][:len(RAW)] == [t["id"] for t in RAW])
hf = [w for w in fby["edit"]["ways"] if w["mode"] == "hasfile"]
check("a declared mode joins its tasks.json task, labelled by `way`", len(hf) == 1 and hf[0]["label"] == "Has a file", hf)
check("accepts reads the primary field only (advanced audio ignored, primary clip kept)",
      fby["edit"]["accepts"] == ["picture", "clip"], fby["edit"]["accepts"])
check("the real tasks are untouched by the synthetic pack", [w["mode"] for w in BY["edit"]["ways"]] == ["edit"])

print()
print("an empty tasks file keeps a task with no pack as ways: []")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
    json.dump([{"id": "nobody", "verb": "V", "short": "S", "line": "L", "group": "X", "order": 1, "room": "r"}], tf)
try:
    only = engines.tasks(tf.name)
finally:
    os.unlink(tf.name)
nb = [t for t in only if t["id"] == "nobody"]
check("a task nothing fills keeps ways: []", bool(nb) and nb[0]["ways"] == [], nb)
check("every real mode then falls back to its own task, still exactly once",
      sorted(k for t in only for k in modes_of(t)) == sorted(ALL_MODES))

print()
print("a missing or broken tasks.json falls back to per-mode tasks, never raises")
logging.disable(logging.WARNING)
try:
    for label, content in (("missing", None), ("unparseable", "{not json"), ("wrong shape", '{"tasks": 1}'),
                           ("task without an id", '[{"verb": "x"}]')):
        if content is None:
            fpath = os.path.join(WORK, "no-such-tasks.json")
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
                tf.write(content)
            fpath = tf.name
        try:
            got, err = engines.tasks(fpath), None
        except Exception as e:      # the defect this pins
            got, err = [], e
        finally:
            if content is not None:
                os.unlink(fpath)
        check("%s tasks.json: tasks() does not raise" % label, err is None, repr(err))
        check("%s tasks.json: every mode still lands in exactly one task" % label,
              sorted(k for t in got for k in modes_of(t)) == sorted(ALL_MODES))
finally:
    logging.disable(logging.NOTSET)
_, lines = with_log(lambda: engines.tasks(os.path.join(WORK, "no-such-tasks.json")))
check("a broken tasks.json logs one warning", len(lines) == 1 and "tasks.json" in lines[0], lines)

print()
print("data packs (tests/fixtures/task_packs): pairing, ordering, blurbs")
DATA = tempfile.mkdtemp(prefix="bwf-tasks-data-")
shutil.copytree(os.path.join(HERE, "fixtures", "task_packs"), os.path.join(DATA, "packs"))
rescan(DATA)
check("the fixture packs load", sorted(p["id"] for p in engines.packs() if p.get("user_pack")) ==
      sorted(os.listdir(os.path.join(DATA, "packs"))) and engines.user_pack_problems() == [], engines.user_pack_problems())
UT, lines = with_log(engines.tasks)
UB = {t["id"]: t for t in UT}
anim = UB["animate"]
check("built-in packs come first inside a task", [w["mode"] for w in anim["ways"]][0] == "fl2va", [w["mode"] for w in anim["ways"]])
check("a plain data-pack way follows, the orphan final becomes a plain way",
      [w["mode"] for w in anim["ways"]] == ["fl2va", "oldway", "orphanfinal"], [w["mode"] for w in anim["ways"]])
check("the keeper pairs with its draft", anim["ways"][0]["final"] == {"cap": "video", "mode": "keepfinal"}, anim["ways"][0]["final"])
check("a paired final is not also a way of its own", "keepfinal" not in [w["mode"] for w in anim["ways"]])
check("a way label comes from the pack's `way`", anim["ways"][1]["label"] == "Re-render the old way")
ppl = UB["people"]["ways"][0]
check("pictures: n pairs the keeper under that count", ppl["finals_by_count"] == {"2": {"cap": "video", "mode": "keeptwo"}} and ppl["final"] is None, ppl)
check("a final_of naming no draft logs a warning and names the mode",
      any("orphanfinal" in l and "nosuchdraft" in l for l in lines), lines)
loner = UB.get("video-loner")
check("a data pack naming an unknown task becomes a task of its own", loner is not None and [w["mode"] for w in loner["ways"]] == ["loner"])
check("a data pack with no task becomes a task of its own", "video-plainmode" in UB)
check("every mode (built-in and data pack) is in exactly one task",
      sorted(k for t in UT for k in modes_of(t)) == sorted((cap, m) for cap in engines.caps() for m in engines.modes_for(cap)))
check("accepts is derived for data-pack modes too (the fixture's picture field)", "picture" in UB["people"]["accepts"])
check("blurb: a built-in falls back to its mode_notes line", engines.mode_blurb("video", "ltx") == engines.mode_note("video", "ltx") != "")
check("blurb: a data pack's own blurb", engines.mode_blurb("video", "keepfinal") == "The keeper: more steps, same picture.")
check("blurb: a data pack with no blurb is '' (its note is never shown)",
      engines.mode_note("video", "plainmode") == "a lab note" and engines.mode_blurb("video", "plainmode") == "")
check("blurb: a data pack with a note AND a blurb shows the blurb", engines.mode_note("video", "keepfinal") == "lab note: do not show"
      and "lab" not in engines.mode_blurb("video", "keepfinal"))

t2 = UB["t2i"]
check("two finals for one draft: the first pairs, the second is demoted", t2["ways"][0]["final"] == {"cap": "video", "mode": "keepa"}, t2["ways"][0]["final"])
check("a demoted final keeps its place in the pack ordering (before a later pack's plain way)",
      [w["mode"] for w in t2["ways"]] == ["t2i", "keepb", "zplain"], [w["mode"] for w in t2["ways"]])
tw = UB.get("tw-shared")
check("task_words bring a task tasks.json lacks; the first declarer's words win",
      tw is not None and tw["verb"] == "Shared first" and tw["group"] == "MOTION", tw and tw["verb"])
if tw:
    check("  later packs join it, in pack order", [w["mode"] for w in tw["ways"]] == ["twone", "twtwo", "twthree"], [w["mode"] for w in tw["ways"]])
    check("  a joiner's `way` labels it", tw["ways"][2]["label"] == "Third")
    check("  no room given: the first rooms.json room of its group", tw["room"] == "video", tw["room"])
    check("  ordered 900+, after tasks.json's, before fallbacks", 900 < tw["order"] < 1000, tw["order"])
check("  it sorts after every tasks.json task and before every fallback",
      [t["id"] for t in UT].index("tw-shared") > [t["id"] for t in UT].index("turntable")
      and [t["id"] for t in UT].index("tw-shared") < [t["id"] for t in UT].index("video-loner"))
check("a known room named by the pack is used", UB["tw-sound"]["room"] == "sfx" and UB["tw-sound"]["group"] == "SOUND", UB.get("tw-sound"))
check("an unknown room falls back to the group's first room", UB["tw-nowhere"]["room"] == "music", UB["tw-nowhere"]["room"])
check("an unknown task WITHOUT task_words keeps the per-mode fallback", "video-loner" in UB and "no-such-task" not in UB)

print()
print("data-pack manifest keys are validated")
GRAPH = {"1": {"class_type": "TextBox", "inputs": {"text": "hello"}}}
BASE = {"schema": 1, "id": "vp", "cap": "video", "mode": "vpmode", "label": "VP", "graph": "graph.api.json",
        "fields": [{"id": "prompt", "label": "Prompt", "type": "text", "bind": [{"node": "1", "input": "text"}]}]}


def load(extra):
    d = tempfile.mkdtemp(prefix="bwf-tasks-vp-", dir=WORK)
    root = os.path.join(d, "packs", "vp")
    os.makedirs(root)
    json.dump(GRAPH, open(os.path.join(root, "graph.api.json"), "w"))
    json.dump(dict(BASE, **extra), open(os.path.join(root, "manifest.json"), "w"))
    logging.disable(logging.WARNING)
    try:
        engs, problems = up.load_user_packs(d, [])
    finally:
        logging.disable(logging.NOTSET)
    return engs, problems

ok, probs = load({"task": "animate", "way": "W", "tier": "final", "final_of": "fl2va", "pictures": 3, "blurb": "B"})
check("a full set of task keys loads", len(ok) == 1 and not probs, probs)
if ok:
    check("  mapped to mode_tasks", ok[0]["mode_tasks"] == {"vpmode": {"task": "animate", "way": "W", "tier": "final",
                                                                        "final_of": "fl2va", "pictures": 3}}, ok[0].get("mode_tasks"))
    check("  mapped to mode_blurbs", ok[0]["mode_blurbs"] == {"vpmode": "B"})
ok, probs = load({})
check("no task keys: nothing mapped", len(ok) == 1 and "mode_tasks" not in ok[0] and "mode_blurbs" not in ok[0])
ok, probs = load({"task": "animate"})
check("task alone is enough", len(ok) == 1 and ok[0]["mode_tasks"] == {"vpmode": {"task": "animate"}}, probs)
BAD = [
    ("tier other than final", {"task": "animate", "tier": "draft", "final_of": "fl2va"}),
    ("tier without final_of", {"task": "animate", "tier": "final"}),
    ("final_of without tier", {"task": "animate", "final_of": "fl2va"}),
    ("pictures 0", {"task": "animate", "tier": "final", "final_of": "fl2va", "pictures": 0}),
    ("pictures 10", {"task": "animate", "tier": "final", "final_of": "fl2va", "pictures": 10}),
    ("pictures a string", {"task": "animate", "tier": "final", "final_of": "fl2va", "pictures": "2"}),
    ("pictures true", {"task": "animate", "tier": "final", "final_of": "fl2va", "pictures": True}),
    ("pictures without tier", {"task": "animate", "pictures": 2}),
    ("way without task", {"way": "W"}),
    ("tier without task", {"tier": "final", "final_of": "fl2va"}),
    ("empty blurb", {"blurb": "  "}),
    ("task_words without task", {"task_words": {"verb": "v", "short": "s", "line": "l", "group": "SOUND"}}),
    ("task_words missing a key", {"task": "x", "task_words": {"verb": "v", "short": "s", "group": "SOUND"}}),
    ("task_words extra key", {"task": "x", "task_words": {"verb": "v", "short": "s", "line": "l", "group": "SOUND", "z": "1"}}),
    ("task_words empty string", {"task": "x", "task_words": {"verb": " ", "short": "s", "line": "l", "group": "SOUND"}}),
    ("task_words bad group", {"task": "x", "task_words": {"verb": "v", "short": "s", "line": "l", "group": "VIDEO"}}),
    ("task_words not an object", {"task": "x", "task_words": "words"}),
    ("empty task", {"task": ""}),
    ("non-string way", {"task": "animate", "way": 4}),
    ("empty final_of", {"task": "animate", "tier": "final", "final_of": ""}),
]
for label, extra in BAD:
    ok, probs = load(extra)
    check("rejected: %s" % label, not ok and len(probs) == 1, (ok, probs))
ok, probs = load({"task": "x", "task_words": {"verb": "v", "short": "s", "line": "l", "group": "OBJECT"}})
check("accepted: task_words, mapped to mode_task_words", len(ok) == 1 and ok[0]["mode_task_words"]["vpmode"]["group"] == "OBJECT", probs)
for n in (1, 9):
    ok, probs = load({"task": "animate", "tier": "final", "final_of": "fl2va", "pictures": n})
    check("accepted: pictures %d" % n, len(ok) == 1, probs)

print()
print("tasks.json names no engine and no plumbing word")
# "turntable" is a plain word and the task's own name, so it is not an engine token.
ENGINE_TOKENS = {p["id"] for p in real if p["id"] not in engines.caps() and not p.get("user_pack")
                 and p["id"] != "turntable"} | {
    "qwen", "minimax", "h3", "ace", "yue", "stable audio", "ltx", "trellis", "sdxl", "birefnet", "esrgan", "rife", "flux",
    "comfyui", "sage", "seedvr2",
}
ENGINE_NAME = re.compile(r"\b(" + "|".join(re.escape(t) for t in ENGINE_TOKENS) + r")\b", re.I)
PLUMBING = re.compile(r"\b(pipeline|model|workflow|cfg|vae|sampler|scheduler|latent|checkpoint|lora)s?\b", re.I)
for t in RAW:
    for k in ("verb", "short", "line"):
        check("tasks.json %s.%s: no engine name / plumbing word" % (t["id"], k),
              not ENGINE_NAME.search(t[k]) and not PLUMBING.search(t[k]), t[k])
check("RED: a planted engine name is caught", bool(ENGINE_NAME.search("Make it with LTX")))

print()
print("/api/engines carries `tasks` beside the caps, and a per-mode blurb")
rescan(WORK)
import server as srv
payload = srv.Handler.engines_payload(None, {})
check("payload has a tasks list equal to engines.tasks()", payload.get("tasks") == engines.tasks() and len(payload["tasks"]) == 21)
caps = [k for k in payload if k not in ("rooms", "helper", "helper_guide_on", "tasks")]
check("every remaining key is a cap with modes", all(isinstance(payload[c], dict) and "modes" in payload[c] for c in caps), caps)
check("every mode carries a blurb string", all(isinstance(m.get("blurb"), str) for c in caps for m in payload[c]["modes"]))
check("a built-in's blurb is its mode_notes line",
      next(m for m in payload["audio"]["modes"] if m["id"] == "sfx")["blurb"] == engines.mode_note("audio", "sfx"))
json.dumps(payload)     # must serialise

shutil.rmtree(DATA, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)
print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    sys.exit(1)
print("all passed")
