"""Gate for P3c, the Motion rooms' writers and the clip fixer: the ltx /
ltx_loop shot writers, the H3 fl2va / ref2v / continue writers, the Talking
Head line writer (the line sized to the clip by the pack, 8n+1 frames), their
checks (also the Make-time guard), each mode's "Try this" against its own
check, and the clip fixer on ltx and the H3 modes (a still is not the clip;
reroll is the only fix). The brain is a scripted stand-in; no browser
(tests/test_motion_writers_ui.py covers the page).

Run: python3 tests/test_motion_writers.py
"""
import importlib.util
import json
import math
import os
import shutil
import sys
import tempfile
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


import engines  # noqa: E402
from engines import ltx, minimax_h3  # noqa: E402

WRITERS = {"ltx": "Shot writer", "ltx_loop": "Long take writer", "talking": "Line writer",
           "fl2va": "Shot writer", "ref2v": "Reference shot writer", "continue": "Carry-on writer"}
FIXERS = ("ltx", "ltx_loop", "fl2va", "ref2v", "continue")
DRAFT = {"topic": "a topic", "answer": None, "answers": []}   # what guide_skill passes a check

print("contract: a writer for every Motion mode")
for mode, label in WRITERS.items():
    w = engines.writer("video", mode)
    fields = {f["id"] for f in engines.fields("video", mode)}
    check("%s: writer %r" % (mode, label), bool(w) and w["label"] == label, w and w.get("label"))
    check("%s: every key fills a real field" % mode, w and set(w["keys"].values()) <= fields, w and w["keys"])
    check("%s: the multiline key is one of the keys, NONE the empty word" % mode,
          w and w["multiline"] in w["keys"] and w["none_token"] == "NONE")
    check("%s: carries a check" % mode, w and callable(w.get("check")))
check("talking: LINE is the multiline key and fills the line; LOOK the shot note",
      engines.writer("video", "talking")["keys"] == {"LENGTH": "length", "LOOK": "look", "LINE": "line"})
check("talking: the pack derives the length", callable(engines.writer("video", "talking").get("derive")))
check("continue: its writer never fills the previous-shot cable",
      "prev_video" not in engines.writer("video", "continue")["keys"].values())

print("contract: the clip fixer")
for mode in FIXERS:
    r = engines.reviser("video", mode)
    check("%s: a Clip fixer that fills the prompt" % mode, bool(r) and r["label"] == "Clip fixer" and r["fills"] == "prompt"
          and any(f["id"] == "prompt" for f in engines.fields("video", mode)), r and {k: r[k] for k in r if k != "prompt"})
    check("%s: reroll is the only fix, no edit mode" % mode, r and r["fixes"] == ["reroll"] and r["edit_mode"] is None)
check("talking has no clip fixer (its words are a line, not a prompt)", engines.reviser("video", "talking") is None)
check("the picture fixer still offers edit", engines.reviser("image", "t2i").get("fixes") is None
      and engines.reviser("image", "t2i")["edit_mode"] == "edit")

print("parser: a clip fixer refuses FIX edit; the picture fixer still takes it")
REPLY = "QUESTION:\nDIAGNOSIS: x.\nFIX: %s\nPROMPT: A fox runs.\nNOTE:\nTWEAK:"
R = engines.reviser("video", "ltx")
check("clip fixer: FIX reroll parses", engines.parse_reviser_reply(R, REPLY % "reroll")["fix"] == "reroll")
try:
    engines.parse_reviser_reply(R, REPLY % "edit")
    check("clip fixer: FIX edit raises ValueError", False)
except ValueError as e:
    check("clip fixer: FIX edit raises ValueError", "reroll" in str(e), e)
check("picture fixer: FIX edit still parses",
      engines.parse_reviser_reply(engines.reviser("image", "t2i"), REPLY % "edit")["fix"] == "edit")

print("sizing: the Talking Head line to the clip (words / 2.2 + 15%, up to 8n+1)")
check("frames_up: 8n+1 at or above", [ltx.frames_up(x) for x in (1, 9, 10, 125.4, 168, 169)] == [1, 9, 17, 129, 169, 169])
check("skills.md's worked example: 10 words -> 129 frames", ltx.talking_frames(10) == 129)
check("knowledge.md's live-trial line: 13 words -> 169 frames", ltx.talking_frames(13) == 169)
check("a short line keeps the 97-frame default", all(ltx.talking_frames(n) == 97 for n in range(1, 8)))
for n in range(1, 80):
    got = ltx.talking_frames(n)
    raw = math.ceil(n / 2.2 * 1.15 * 24)
    if not (got % 8 == 1 and got >= max(97, raw) and got - 8 < max(97, raw)):
        check("talking_frames(%d) is the smallest 8n+1 at or above the formula" % n, False, got)
        break
else:
    check("talking_frames(1..79) is always the smallest 8n+1 at or above the formula", True)
check("one clip (361 frames) holds 28 words, as knowledge.md says", ltx.talking_max_words() == 28)
check("spoken words: a dash is not a word", ltx.spoken_words("Hey there, welcome back — I saved your spot right here.") == 10)
check("derive: the length from the line", ltx.talking_derive({"line": "Hey there, welcome back, I saved your spot right here.",
                                                              "fps": 24}) == {"length": 129})
check("derive: a listening shot (no line) derives nothing", ltx.talking_derive({"line": "", "fps": 24}) == {})

print("check: Talking Head")
LINE10 = "Hey there, welcome back, I saved your spot right here."
p = ltx.talking_check({"line": LINE10, "length": 97, "fps": 24, "look": ""}, {})
check("a 10-word line in 97 frames is cut off: the fix names 129 frames",
      len(p) == 1 and "10 words" in p[0] and "Set Length to 129 frames" in p[0], p)
check("the right length passes", ltx.talking_check({"line": LINE10, "length": 129, "fps": 24}, DRAFT) == [])
check("a longer length passes (it plays out as listening)", ltx.talking_check({"line": LINE10, "length": 241, "fps": 24}, DRAFT) == [])
LONG = " ".join(["word"] * 40)
p = ltx.talking_check({"line": LONG, "length": 97, "fps": 24}, {})
check("a 40-word line is too long for one clip: shorten or split", len(p) == 1 and "40 words" in p[0]
      and "Shorten it, or split it across two clips" in p[0] and "28 words" in p[0], p)
check("a long line the user already gave a long enough clip passes",
      ltx.talking_check({"line": LONG, "length": ltx.talking_frames(40), "fps": 24}, {}) == [])
p = ltx.talking_check({"line": "hi", "length": 97, "fps": 24, "look": "make her say it warmly"}, {})
check("a shot note that carries speech is a problem", len(p) == 1 and "shot note" in p[0], p)
check("a plain shot note passes", ltx.talking_check({"line": "hi", "length": 97, "fps": 24, "look": "warm lamp light, close-up"}, {}) == [])
p = ltx.talking_check({"line": "hi", "length": 100, "fps": 24}, DRAFT)
check("drafting: a length that is not 8n+1 is a problem naming 97 or 105", len(p) == 1 and "97 or 105" in p[0], p)
check("Make time: the graph's own refusal stands for 8n+1, no confirm asked",
      ltx.talking_check({"line": "hi", "length": 100, "fps": 24}, {"lane": "t"}) == [])

print("check: ltx / ltx_loop shots")
p = ltx.shot_check({"prompt": "a (fox:1.3) runs through [snow]", "length": 97}, {})
check("token weights and brackets are a problem", len(p) == 1 and "(fox:1.3)" in p[0] and "[snow]" in p[0], p)
check("plain sentences pass", ltx.shot_check({"prompt": "A red fox runs across a snowy field.", "length": 97}, DRAFT) == [])
p = ltx.shot_check({"prompt": "A fox.", "length": 240, "width": 1000, "height": 576}, DRAFT)
check("drafting: length and width off the grid", len(p) == 2 and "233 or 241" in p[0] and "992 or 1024" in p[1], p)
check("Make time: the grid is left to the graph", ltx.shot_check({"prompt": "A fox.", "length": 240}, {"lane": "t"}) == [])

print("check: H3")
check("fl2va / ref2v: plain prompt passes", minimax_h3.h3_shot_check({"prompt": "Live-action, cinematic, a fox runs."}, {}) == [])
check("fl2va / ref2v: weights are a problem", len(minimax_h3.h3_shot_check({"prompt": "a ((fox)) runs"}, {})) == 1)
p = minimax_h3.h3_continue_check({"prompt": "Cut to a close-up of the driver from a different angle."}, {})
check("continue: a new composition is a problem naming what gave it away and where it belongs",
      len(p) == 1 and '"cut to"' in p[0] and '"a close-up of"' in p[0] and "hard cut" in p[0]
      and minimax_h3.ENGINE_MODE_WORDS["fl2va"] in p[0], p)
check("continue: 'in a close-up' is caught", len(minimax_h3.h3_continue_check(
    {"prompt": "The camera keeps tracking, focusing on his face in a close-up."}, {})) == 1)
check("continue: the same shot carrying on passes", minimax_h3.h3_continue_check(
    {"prompt": "Live-action, cinematic, the car keeps driving along the coast road. The camera keeps tracking alongside."}, {}) == [])

print("'Try this': every Motion example passes its own mode's check")
for mode in WRITERS:
    w = engines.writer("video", mode)
    for ex in engines.examples("video", mode):
        vals = {f["id"]: f.get("default") for f in engines.fields("video", mode) if f.get("default") is not None}
        vals.update(next((x["values"] for x in engines.presets("video", mode) if x["id"] == ex.get("recipe")), {}))
        vals.update(ex["values"])
        for label, req in (("Make time", {}), ("drafting", DRAFT)):
            got = w["check"](vals, req)
            check("%s %s: passes (%s)" % (mode, ex["id"], label), got == [], got)


print("prompts: each writer carries its engine's rules")
P = ltx.LTX_WRITER_PROMPT
for needle in ("At most TWO actions", "ONE move or a still camera", "NEVER write token weights",
               "the sound: what is heard", "spoken words in quotation marks", "QUESTION:", "OPTIONS:",
               "LENGTH stays NONE when the request gives no number of seconds", "10 s: 241",
               "a tall or square frame comes out with distorted motion"):
    check("ltx: %r" % needle, needle in P)
check("ltx_loop: NO SOUND, one unbroken take, nothing about sound",
      "NO SOUND" in ltx.LTX_LOOP_WRITER_PROMPT and "one long unbroken take" in ltx.LTX_LOOP_WRITER_PROMPT
      and "write nothing about sound" in ltx.LTX_LOOP_WRITER_PROMPT and "Water trickles" not in ltx.LTX_LOOP_WRITER_PROMPT)
T = ltx.TALKING_WRITER_PROMPT
for needle in ("LINE is only the spoken words", "LINE is those words, unchanged", "in that speaker's own voice",
               "at most 28 spoken words", "LOOK is the light and framing only", "the app sizes the clip to the line",
               "OPTIONS: Shorten it | Split it into two clips", "A named speaker (a cowboy", "LINE comes last"):
    check("talking: %r" % needle, needle in T)
for mode, prompt in (("fl2va", minimax_h3.H3_FL2VA_WRITER_PROMPT), ("ref2v", minimax_h3.H3_REF2V_WRITER_PROMPT),
                     ("continue", minimax_h3.H3_CONTINUE_WRITER_PROMPT)):
    for needle in ("Live-action, cinematic,", "INSIDE the prompt, in quotation marks", "push in, pull out, pan left",
                   "never below 124", "LENGTH is NONE unless the request gives a number of seconds"):
        check("%s: %r" % (mode, needle), needle in prompt)
check("ref2v: names each reference", "the woman from the reference picture" in minimax_h3.H3_REF2V_WRITER_PROMPT)
C = minimax_h3.H3_CONTINUE_WRITER_PROMPT
check("continue: the SAME shot carrying on", "SAME shot carrying on" in C and "hard cut" in C)
check("continue: the carry-on question, with its two options",
      "QUESTION: Carry on the same shot, or cut to a new one? OPTIONS: Carry on the same shot | Cut to a new shot" in C)
check("continue: the delivered piece is about a second short", "8 seconds is LENGTH 216" in C)
TOPICS = ("fox", "windmill", "chef", "skateboard", "pirate", "monday", "balloon", "samurai", "astronaut",
          "spaceship", "wedding", "runner", "coast road")
for name, prompt in (("ltx", P), ("ltx_loop", ltx.LTX_LOOP_WRITER_PROMPT), ("talking", T),
                     ("fl2va", minimax_h3.H3_FL2VA_WRITER_PROMPT), ("ref2v", minimax_h3.H3_REF2V_WRITER_PROMPT)):
    check("%s: its worked examples avoid the trial's topics" % name, not any(t in prompt.lower() for t in TOPICS),
          [t for t in TOPICS if t in prompt.lower()])
for name, prompt in (("ltx", ltx.LTX_REVISER_PROMPT), ("H3", minimax_h3.H3_REVISER_PROMPT)):
    check("%s fixer: a still is not the clip" % name, "A STILL IS NOT THE CLIP" in prompt
          and "never say yes or no to any of those" in prompt and "how the camera moved" in prompt)
    check("%s fixer: reroll only" % name, "FIX: reroll\n" in prompt and "FIX is always reroll" in prompt)
    check("%s fixer: asks when nothing is named" % name, "never guess a cause" in prompt
          and "QUESTION: What looks or sounds wrong" in prompt)
check("H3 fixer: never a likeness verdict", "check it at full size" in minimax_h3.H3_REVISER_PROMPT)

print("guide: skills.md describes what the writers do")
skills = open(os.path.join(ROOT, "guides", "motion", "skills.md"), encoding="utf-8").read()
for needle in ("Shot writer", "Long take writer", "Reference shot writer", "Carry-on writer", "Line writer",
               "Clip fixer", "the app works the length out", "Carry on the same shot | Cut to a new shot",
               "never claims camera movement"):
    check("skills.md: %r" % needle, needle in skills)

# ---------------------------------------------------------------------------
# guide_skill / guide_revise / generate, with a scripted brain
# ---------------------------------------------------------------------------
SCRATCH = tempfile.mkdtemp(prefix="bwf_motion_writers_")
LTX_MODELS = {"ltx_transformer": "ltx-2.5.gguf", "ltx_clip": "ltx_te.safetensors", "ltx_vae_video": "ltx_vae_video.safetensors",
              "ltx_vae_audio": "ltx_vae_audio.safetensors", "ltx_upscaler": "ltx_spatial.safetensors"}
cfg = {"port": 1, "bind": "127.0.0.1", "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": 1,
                                                  "caps": ["video"], "models": LTX_MODELS}],
       "helper": {"url": "http://127.0.0.1:9/v1", "model": "test-model", "timeout_s": 2, "vision": False}}
with open(os.path.join(SCRATCH, "config.json"), "w") as f:
    json.dump(cfg, f)
os.environ["GENCENTER_CONFIG"] = os.path.join(SCRATCH, "config.json")
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")
spec = importlib.util.spec_from_file_location("srv_motion_writers", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
BRAIN = {"replies": [], "asked": []}


def fake_chat(messages, max_tokens=None, timeout=None):
    BRAIN["asked"].append(messages)
    return BRAIN["replies"].pop(0), "stop"


srv._helper_chat = fake_chat


def skill(room, mode, topic, replies, **extra):
    BRAIN["replies"][:] = list(replies)
    del BRAIN["asked"][:]
    return srv.guide_skill(dict({"room": room, "mode": mode, "topic": topic}, **extra))


print("skill: Talking Head, the length comes from the line")
PIRATE = "Avast! The moon is high, so off to your bunk with ye, little scallywag."   # live trial reply, 12B brain
b, code = skill("talking", "talking", "a pirate telling a kid to go to bed",
                ["LENGTH: NONE\nLOOK: dim lantern light, close-up\nLINE: " + PIRATE])
check("200 with line, look and the derived length", code == 200 and b.get("fields") == {
    "line": PIRATE, "look": "dim lantern light, close-up", "length": ltx.talking_frames(ltx.spoken_words(PIRATE))}, b)
check("14 words -> 177 frames, no problems, no retry", b.get("fields", {}).get("length") == 177
      and b.get("problems") == [] and b.get("retried") is False, b)
check("the writer's own prompt was sent", BRAIN["asked"][0][0]["content"] == ltx.TALKING_WRITER_PROMPT)
b, code = skill("talking", "talking", "a 10 second clip of her saying hello",
                ["LENGTH: 241\nLOOK: soft light\nLINE: Hello there."])
check("a length the user stated is kept, not derived", code == 200 and b["fields"]["length"] == 241, b)
b, code = skill("talking", "talking", "a 4 second clip where he reads the whole poem",
                ["LENGTH: 97\nLOOK: soft light\nLINE: " + LINE10, "LENGTH: 129\nLOOK: soft light\nLINE: " + LINE10])
check("a stated length too short for the line: one retry with the fix sentence, then clean",
      code == 200 and b["retried"] is True and b["problems"] == [] and b["fields"]["length"] == 129
      and "Set Length to 129 frames" in BRAIN["asked"][1][1]["content"], b)
b, code = skill("talking", "talking", "say something about Mondays",
                ["QUESTION: What tone should it have?\nOPTIONS: Excited | Grumpy | Professional"])
check("a tone question comes back with its options", code == 200 and b.get("question") == "What tone should it have?"
      and b.get("options") == ["Excited", "Grumpy", "Professional"], b)
b, code = skill("talking", "talking", "listen quietly", ["LENGTH: NONE\nLOOK: soft light\nLINE: NONE"])
check("LINE NONE is a listening shot: an empty line, the form keeps its length",
      code == 200 and b["fields"] == {"line": "", "look": "soft light"}, b)

print("skill: the shot writers")
b, code = skill("video", "ltx", "10 seconds of a chef flambeing a pan, the camera pushes in slowly",
                ["LENGTH: 241\nWIDTH: NONE\nHEIGHT: NONE\nPROMPT: A chef flambes a pan. The camera slowly pushes in."])
check("ltx: prompt and length", code == 200 and b["fields"] == {"prompt": "A chef flambes a pan. The camera slowly pushes in.",
                                                                "length": 241} and b["problems"] == [], b)
b, code = skill("video", "ltx", "a fox", ["LENGTH: 240\nPROMPT: A fox runs.", "LENGTH: 241\nPROMPT: A fox runs."])
check("ltx: a length off the grid is retried once with the 8n+1 sentence", code == 200 and b["retried"] is True
      and b["fields"]["length"] == 241 and "8n+1" in BRAIN["asked"][1][1]["content"], b)
b, code = skill("video", "continue", "now show the driver's face in close-up",
                ["LENGTH: NONE\nPROMPT: Cut to a close-up of the driver.",
                 "LENGTH: NONE\nPROMPT: Live-action, cinematic, the car keeps driving; the camera keeps tracking alongside."])
check("continue: a new composition is retried once, then the same shot passes", code == 200 and b["retried"] is True
      and b["problems"] == [] and "keeps driving" in b["fields"]["prompt"], b)

print("guard: the Make-time check on a Talking Head")
srv.dispatch = lambda lane, graph, kind, mode, meta: {"ok": True, "job": {"id": "j1", "status": "queued"}}
with srv.STATE_LOCK:
    srv.LANE_STATE["t"] = {"up": True, "checked": time.time(), "err": ""}
TOO_SHORT = {"lane": "t", "kind": "video", "mode": "talking", "line": LINE10, "length": 97, "face": "face.png"}
body, code = srv.generate(dict(TOO_SHORT))
check("a line that would be cut off -> 409 needs_confirm naming the length", code == 409 and body.get("needs_confirm") is True
      and any("129 frames" in x for x in body.get("problems", [])), body)
body, code = srv.generate(dict(TOO_SHORT, length=129))
check("the right length needs no confirm", body.get("needs_confirm") is None, body)
body, code = srv.generate(dict(TOO_SHORT, line="Hello.", length=100))
check("a length off the grid is not asked to be confirmed (the graph refuses it on its own)",
      body.get("needs_confirm") is None, body)

print("revise: the clip fixer on a finished clip")
job = {"id": "clip1", "lane": "t", "lane_name": "Test lane", "kind": "video", "mode": "ltx", "status": "done",
       "prompt": "Slow push toward the lighthouse lamp as it blazes on.", "length": 97,
       "outputs": [{"filename": "LTX_00005_.mp4", "subfolder": "blackwire", "type": "output", "media": "video"}]}
with srv.JOBS_LOCK:
    srv.JOBS["clip1"] = job
BRAIN["replies"][:] = ["QUESTION:\nDIAGNOSIS: I can't see the clip, so going by what you say: no camera move was stated.\n"
                       "FIX: reroll\nPROMPT: A lighthouse lamp blazes on. The camera slowly pushes in toward it.\nNOTE:\nTWEAK:"]
b, code = srv.guide_revise({"room": "video", "job_id": "clip1", "complaint": "the camera never pushes in"})
check("200: reroll, fills the prompt, no edit mode", code == 200 and b.get("fix") == "reroll" and b.get("fills") == "prompt"
      and b.get("edit_mode") is None and b.get("prompt", "").startswith("A lighthouse lamp"), b)
check("the clip fixer's own prompt was sent", BRAIN["asked"][-1][0]["content"] == ltx.LTX_REVISER_PROMPT)
BRAIN["replies"][:] = ["QUESTION:\nDIAGNOSIS: x\nFIX: edit\nPROMPT: Make the lamp brighter.\nNOTE:\nTWEAK:"]
b, code = srv.guide_revise({"room": "video", "job_id": "clip1", "complaint": "too dark"})
check("FIX edit on a clip -> 502, the shape error (never a broken Edit button)", code == 502 and b.get("ok") is False, b)

shutil.rmtree(SCRATCH, ignore_errors=True)
print()
print("FAILED: %d" % len(FAILED))
for n in FAILED:
    print("  - " + n)
sys.exit(1 if FAILED else 0)
