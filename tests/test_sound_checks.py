"""Gate for P3b, the Sound writers (pack level, part 2): the yue2, cover and
sfx checks, each writer's prompt rules, and every Sound "Try this" example
passing its own mode's check. Part 1 is tests/test_sound_writers.py.

Run: python3 tests/test_sound_checks.py
"""
import os
import sys

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


import engines  # noqa: E402
from engines import audio  # noqa: E402

W = {m: engines.writer("audio", m) for m in ("music", "yue2", "cover", "sfx")}
RAP = "\n\n".join("[%s]\n" % s + "\n".join(["line of the verse right here"] * 8) for s in ("Verse", "Hook", "Verse", "Hook"))

print("yue2, cover and sfx checks")
check("yue2: lyrics and no voice in the style", "names no voice" in " ".join(audio.yue2_check(
    {"style": "orchestral folk", "lyrics": RAP, "max_duration": 150.0}, {})))
check("yue2: sized against the ceiling", "at least 5" in " ".join(audio.yue2_check(
    {"style": "folk, warm female voice", "lyrics": RAP, "max_duration": 300.0}, {})))
check("yue2: an instrumental passes", audio.yue2_check({"style": "folk", "lyrics": "", "max_duration": 300.0}, {}) == [])
check("cover: a voice and no words is the no-words case", "no real words" in " ".join(
    audio.cover_check({"style": "reggae, male vocals", "lyrics": ""}, {})))
check("cover: words and no voice", "names no voice" in " ".join(audio.cover_check({"style": "reggae", "lyrics": "[Verse]\nx"}, {})))
check("cover: words and a voice, or neither, pass", audio.cover_check({"style": "reggae, male vocals", "lyrics": "[Verse]\nx"}, {})
      == [] and audio.cover_check({"style": "reggae", "lyrics": ""}, {}) == [])
p = audio.sfx_check({"prompt": "a man saying hello, then a door slam"}, {})
check("sfx: speech is named, with Talking Head", len(p) == 1 and "saying" in p[0] and "Talking Head" in p[0], p)
check("sfx: 'a rap on the door' and a creak pass", audio.sfx_check({"prompt": "a rap on the door"}, {}) == []
      and audio.sfx_check({"prompt": "a wooden door creaking open"}, {}) == [])

print("prompts: each writer's rules")
for mode, needles in (("music", ("no Markdown", "ONLY WHAT CAN BE HEARD", "Never NONE for a song or a rap", "Global Metadata\n",
                                 "Sung, rapped, or instrumental?", "Who should sing it?", "How long should it be?")),
                      ("yue2", ("STYLE MUST name the voice", "only a ceiling", "Sung, or instrumental?")),
                      ("cover", ("never its words", "Keep the original words, or write new ones?", "exactly KEEP",
                                 "Paste the original words into the Lyrics box")),
                      ("sfx", ("Talking Head room", "Music room", "PROMPT comes last", "2 for a one-shot"))):
    missing = [n for n in needles if n not in W[mode]["prompt"]]
    check("%s prompt: %s" % (mode, ", ".join(needles)), not missing, missing)
check("music prompt: no trial topic (boom bap, sneaker, brother)", not any(
    t in W["music"]["prompt"].lower() for t in ("boom bap", "sneaker", "brother")))
check("music prompt_guide: plain text, only what can be heard", "no Markdown" in engines.prompt_guide("audio", "music")
      and "only what can be heard" in engines.prompt_guide("audio", "music"))

print("'Try this' examples pass their own mode's check")
for mode in ("music", "yue2", "cover", "sfx"):
    ex = engines.examples("audio", mode)[0]
    vals = {f["id"]: f.get("default") for f in engines.fields("audio", mode)}
    vals.update(next((x["values"] for x in engines.presets("audio", mode) if x["id"] == ex.get("recipe")), {}))
    vals.update(ex["values"])
    probs = W[mode]["check"](vals, {})
    check("%s: %s passes" % (mode, ex["id"]), probs == [], probs)
check("music example: a plain caption of 250+ words, with lyrics",
      len(engines.examples("audio", "music")[0]["values"]["caption"].split()) >= 250
      and engines.examples("audio", "music")[0]["values"]["lyrics"].strip())

print()
print("FAILED: %d" % len(FAILED))
for n in FAILED:
    print("  - " + n)
sys.exit(1 if FAILED else 0)
