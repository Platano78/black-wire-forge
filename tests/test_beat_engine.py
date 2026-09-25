"""Gate for P3e (owner ruling 2026-09-24 late: storyboard beats match their
shot's engine). Two halves, no browser, no network:

  1. A beat copied into a shot (pre-filled at import, or "Copy beat into
     prompt") loses ONE leading task prefix declared by ANOTHER pack than the
     shot's own (engines.foreign_prefix_stripped, each pack's "task_prefix");
     the shot's own engine keeps it; text with no prefix is untouched; any
     other bracket stays, so the Make-time check still asks.
  2. "Write this shot": POST /api/guide/skill for the shot's own mode, from
     that mode's room, with the neighbouring beats in context.neighbours. The
     brain is a stub of _helper_chat that records what it was asked.

Run: python3 tests/test_beat_engine.py
"""
import importlib.util
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402,F401 -- must run before server.py's own exec_module below
spec = importlib.util.spec_from_file_location("srv_beat_engine", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
import engines  # noqa: E402
from engines import ltx, minimax_h3  # noqa: E402

PREFIX = "[reference generation]"
BODY = "The target video pushes slowly into <Subject 1> as <Subject 2> sets two cups down."
H3_BEAT = PREFIX + " " + BODY

print("the pack declares its own prefix; the core strips only another pack's")
check("the H3 pack declares a task_prefix", bool(minimax_h3.ENGINE.get("task_prefix")))
check("no pack but H3 declares one (nothing else to strip yet)",
      [p["id"] for p in engines.packs() if p.get("task_prefix")] == [minimax_h3.ENGINE["id"]],
      [p["id"] for p in engines.packs() if p.get("task_prefix")])
fps = getattr(engines, "foreign_prefix_stripped", None)
check("engines.foreign_prefix_stripped exists", callable(fps))
if callable(fps):
    check("an LTX shot loses the other engine's prefix", fps("video", "ltx", H3_BEAT) == BODY, fps("video", "ltx", H3_BEAT))
    check("an H3 shot keeps its own prefix (ref2v)", fps("video", "ref2v", H3_BEAT) == H3_BEAT)
    check("an H3 shot keeps its own prefix (fl2va)", fps("video", "fl2va", H3_BEAT) == H3_BEAT)
    check("a picture shot loses it too (any other pack)", fps("image", "t2i", H3_BEAT) == BODY)
    check("text with no prefix is untouched", fps("video", "ltx", BODY) == BODY)
    check("a bracket that is not leading stays", fps("video", "ltx", "a fox [runs] home") == "a fox [runs] home")
    check("only ONE leading prefix goes", fps("video", "ltx", PREFIX + " " + PREFIX + " x") == PREFIX + " x")
    print("the prefix is the vendor's closed task vocabulary, never any bracket")
    check("[reference generation] a lighthouse: stripped in an LTX shot",
          fps("video", "ltx", "[reference generation] a lighthouse") == "a lighthouse")
    check("[reference generation] a lighthouse: kept in an H3 shot",
          fps("video", "fl2va", "[reference generation] a lighthouse") == "[reference generation] a lighthouse")
    check("[video continuation + keyframe completion]: stripped in an LTX shot",
          fps("video", "ltx", "[video continuation + keyframe completion] the car drives on") == "the car drives on")
    for cap, mode in (("video", "ltx"), ("video", "fl2va"), ("video", "ref2v"), ("image", "t2i")):
        check("[wind] rustling trees: NOT stripped (%s)" % mode,
              fps(cap, mode, "[wind] rustling trees") == "[wind] rustling trees")
    check("[wind] rustling trees: the LTX check still asks about it",
          len(ltx.shot_check({"prompt": "[wind] rustling trees"}, {})) == 1)
    check("[wind] rustling trees: the H3 check still asks about it",
          len(minimax_h3.h3_shot_check({"prompt": "[wind] rustling trees"}, {})) == 1)
    check("the H3 check still reads past a real prefix", minimax_h3.h3_shot_check(
        {"prompt": "[video editing + reference generation + audio reuse] Live-action, a fox runs."}, {}) == [])
    check("the strip and the H3 check use the same pattern",
          minimax_h3.ENGINE["task_prefix"] == minimax_h3._TASK_PREFIX_RE.pattern)

print("the storyboard ops: import pre-fill and Copy beat into prompt")
srv.SEQ_DIR = os.path.join(os.environ["GENCENTER_DATA"], "sequences")
os.makedirs(srv.SEQ_DIR, exist_ok=True)
seq, code = srv.seq_create({"title": "P3e", "mode": "storyboard"})
sid = seq["id"]
b, code = srv.seq_op({"id": sid, "rev": seq["rev"], "op": "import_script",
                      "text": "INT. LUNAR APARTMENT - NIGHT.\n\n" + H3_BEAT + "\n\n"
                              + PREFIX + " a fox [runs] across the table.\n\nA plain beat with no prefix."})
check("import_script -> 200", code == 200, b)
slots = {s["beat_id"]: s for s in b.get("slots") or []}
film = [x for x in b.get("beats") or [] if x["kind"] == "film"]
check("three film beats, each with an LTX shot", len(film) == 3 and all(slots[x["id"]]["mode"] == "ltx" for x in film),
      [(x["kind"], slots.get(x["id"], {}).get("mode")) for x in film])
if len(film) == 3:
    first, stray, plain = film
    check("import pre-fill: the LTX shot's prompt has no H3 prefix",
          slots[first["id"]]["values"].get("prompt") == BODY, slots[first["id"]]["values"])
    check("the beat's own text is kept whole", first["text"] == H3_BEAT)
    stray_prompt = slots[stray["id"]]["values"].get("prompt")
    check("a stray bracket after the prefix stays", stray_prompt == "a fox [runs] across the table.", stray_prompt)
    check("...and the LTX Make-time check still asks about it",
          len(ltx.shot_check({"prompt": stray_prompt}, {})) == 1 and "[runs]" in ltx.shot_check({"prompt": stray_prompt}, {})[0])
    check("...but not about the stripped prefix", ltx.shot_check({"prompt": BODY}, {}) == [])
    check("a beat with no prefix is copied as it is",
          slots[plain["id"]]["values"].get("prompt") == "A plain beat with no prefix.")
    shot = slots[first["id"]]["id"]
    b, code = srv.seq_op({"id": sid, "rev": b["rev"], "op": "update_slot", "slot_id": shot, "mode": "ref2v"})
    check("the shot moves to the H3 reference mode", code == 200, b)
    b, code = srv.seq_op({"id": sid, "rev": b["rev"], "op": "copy_beat_to_prompt", "beat_id": first["id"]})
    got = next(s for s in b["slots"] if s["id"] == shot)["values"].get("prompt")
    check("Copy beat into prompt: an H3 shot keeps its own prefix", got == H3_BEAT, got)
    b, code = srv.seq_op({"id": sid, "rev": b["rev"], "op": "update_slot", "slot_id": shot, "mode": "ltx"})
    b, code = srv.seq_op({"id": sid, "rev": b["rev"], "op": "copy_beat_to_prompt", "beat_id": first["id"]})
    got = next(s for s in b["slots"] if s["id"] == shot)["values"].get("prompt")
    check("Copy beat into prompt: an LTX shot loses the H3 prefix", got == BODY, got)

print("Write this shot: the shot's own writer, with the neighbouring beats")
ASKED, REPLIES = [], []


def fake_chat(messages, max_tokens=512, timeout=None):
    ASKED.append(messages)
    return REPLIES.pop(0), "stop"


srv._helper_chat = fake_chat
srv.HELPER = {"url": "http://127.0.0.1:1/v1", "model": "stub", "timeout_s": 5}
NEIGH = {"before": "INT. LUNAR APARTMENT - NIGHT.", "after": PREFIX + " The target video holds a close-up on <Subject 2>."}
LTX_PROMPT = "A woman sets two porcelain cups on a steel table. The camera pushes in slowly."
REPLIES[:] = ["LENGTH: NONE\nPROMPT: " + LTX_PROMPT]
body, code = srv.guide_skill({"room": "video", "mode": "ltx", "topic": H3_BEAT,
                              "context": {"mode": "ltx", "fields": {}, "neighbours": NEIGH}})
check("LTX shot: 200 with the prompt written", code == 200 and body.get("fields", {}).get("prompt") == LTX_PROMPT, body)
system, user = (ASKED[-1][0]["content"], ASKED[-1][1]["content"]) if ASKED else ("", "")
check("LTX shot: the brain got LTX's own writer prompt", system == ltx.ENGINE["writers"]["ltx"]["prompt"])
check("LTX shot: the previous beat is in the request, one line", "Before: INT. LUNAR APARTMENT - NIGHT." in user, user)
check("LTX shot: the next beat is in the request, without the other engine's prefix",
      "After: The target video holds a close-up on <Subject 2>." in user and PREFIX not in user, user)
check("LTX shot: the beat itself is the request, without the other engine's prefix", "Request: " + BODY in user, user)
check("LTX shot: it is told to write only its own shot", "Write THIS shot" in user, user)

H3_PROMPT = PREFIX + " Live-action, cinematic, the woman from the reference picture sets two cups down."
REPLIES[:] = ["LENGTH: NONE\nPROMPT: " + H3_PROMPT]
ASKED[:] = []
body, code = srv.guide_skill({"room": "video", "mode": "ref2v", "topic": H3_BEAT,
                              "context": {"mode": "ref2v", "fields": {}, "neighbours": NEIGH}})
check("H3 shot: 200, its own prefix kept and no problem raised",
      code == 200 and body.get("fields", {}).get("prompt") == H3_PROMPT and body.get("problems") == [], body)
system, user = (ASKED[-1][0]["content"], ASKED[-1][1]["content"]) if ASKED else ("", "")
check("H3 shot: the brain got H3's own reference writer prompt", system == minimax_h3.ENGINE["writers"]["ref2v"]["prompt"])
check("H3 shot: the beat goes whole, prefix and all", "Request: " + H3_BEAT in user, user)
check("H3 shot: the next beat keeps the prefix too", "After: " + NEIGH["after"] in user, user)

print("refusals: neighbours must be small, known and text")
for bad, why in (({"before": 3}, "a number"), ({"later": "x"}, "an unknown key"),
                 ({"before": "x" * 501}, "too long"), ("before", "not an object")):
    REPLIES[:] = ["LENGTH: NONE\nPROMPT: x"]
    body, code = srv.guide_skill({"room": "video", "mode": "ltx", "topic": "a shot",
                                  "context": {"mode": "ltx", "fields": {}, "neighbours": bad}})
    check("neighbours %s -> 400 in plain words" % why, code == 400 and "neighbour" in body.get("error", ""), (code, body))
REPLIES[:] = ["LENGTH: NONE\nPROMPT: x"]
ASKED[:] = []
body, code = srv.guide_skill({"room": "video", "mode": "ltx", "topic": "a shot", "context": {"mode": "ltx", "fields": {}}})
check("no neighbours: no continuity block", code == 200 and ASKED and "Write THIS shot" not in ASKED[-1][1]["content"], ASKED[-1:])

print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
