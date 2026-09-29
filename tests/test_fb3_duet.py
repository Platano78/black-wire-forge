"""Gate for FB-3: lyric lines that are stage directions are flagged by every
audio mode with lyrics, the music writer learns the two-voice recipe (role
tags, never [Singer A]/[Singer B] tag lines), and the one-voice engines say so.
The brain is a stub of _helper_chat. No browser, no network.

Run: python3 tests/test_fb3_duet.py
"""
import importlib.util
import os
import re
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402,F401 -- must run before server.py's own exec_module below
import engines  # noqa: E402
from engines import audio  # noqa: E402

BODY = "\n\n".join("[%s]\n" % s + "\n".join(["kicks on the shelf and the box is new"] * 8) for s in ("Verse", "Hook", "Verse", "Hook"))
CAP = ("Global Metadata\n" + "Gritty 90s boom bap with dusty drums and a jazz piano loop. " * 20
       + "\nVocal Details\nA male rapper with a low, gravelly voice.\nArrangement\nIntro: vinyl crackle, then drums.")

print("R1: stage-direction lines are flagged in every mode with lyrics")
for line in ("(Heavy boom bap beat kicks in)", "(Rap) Move the Funkos", "  (High energy dancehall flow)  "):
    lyr = BODY + "\n\n[Outro]\n" + line
    got = {
        "song": audio.song_check({"tags": "boom bap, male vocals", "lyrics": lyr, "duration": 150.0}, {}),
        "music": audio.music_check({"caption": CAP, "lyrics": lyr, "seconds": 150}, {}),
        "yue2": audio.yue2_check({"style": "hip hop, male vocals", "lyrics": lyr, "max_duration": 150.0}, {}),
        "cover": audio.cover_check({"style": "reggae, male vocals", "lyrics": lyr}, {}),
    }
    for mode, probs in got.items():
        hit = [p for p in probs if "will be sung as words" in p and line.strip() in p]
        check("%s flags %r once" % (mode, line.strip()), len(hit) == 1, probs)
clean = BODY + "\n\n[Chorus]\nWe rise (together)\n[rap vocal]\n[sung vocal]"
check("[Chorus], role tags and an inline (together) are not flagged", getattr(audio, "stage_direction_problems", lambda t: ["missing"])(clean) == [])
check("cover: no lyrics and no voice still passes", audio.cover_check({"style": "reggae", "lyrics": ""}, {}) == [])

print("R2/R3: writer prompts")
W = {m: engines.writer("audio", m) for m in ("song", "music", "yue2", "cover")}
mp = W["music"]["prompt"]
for needle in ("Singer A", "anti-choir", "couplet", "Vocal Gender & Timbre", "[rap vocal]", "[male vocal]",
               "never line by line", "first chorus", "make it twice", "NO DIRECTIONS IN THE LYRICS"):
    check("music prompt has %r" % needle, needle in mp)
check("music prompt never asks for [Singer A]/[Singer B] tag lines", "Never write [Singer A] or [Singer B] as a tag" in mp)
for mode in ("song", "yue2", "cover"):
    p = W[mode]["prompt"]
    check("%s prompt: directions rule and one voice, points at the Music3 mode" % mode,
          "NO DIRECTIONS IN THE LYRICS" in p and "sings with one voice" in p
          and "Background music, with or without singing" in p and "Singer A" not in p, p[-600:])
check("the picker label the prompts name exists", audio.ENGINE["mode_labels"]["music"] == "Background music, with or without singing"
      if "mode_labels" in audio.ENGINE else "Background music, with or without singing" in repr(audio.ENGINE))

print("R2: a duet draft through the server keeps its role tags")
spec = importlib.util.spec_from_file_location("srv_fb3", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
DUET_CAP = ("Global Metadata\n" + "Gritty boom bap crossed with dancehall, dusty drums and a heavy bass. " * 20
            + "\nVocal Details\nVocal Gender & Timbre: Singer A is a male rapper, Singer B is a female dancehall singer. "
              "Singer A opens. Only one voice at a time, never a choir.\nArrangement\nIntro: drums.")
DUET_LYR = "\n\n".join("[%s]\n[%s]\n" % (s, r) + "\n".join(["kicks on the shelf and the box is new"] * 8)
                       for s, r in (("Verse", "rap vocal"), ("Chorus", "sung vocal"), ("Verse", "rap vocal"), ("Chorus", "sung vocal")))
ASKED = []
srv._helper_chat = lambda messages, max_tokens=512, timeout=None: (ASKED.append(messages) or
    ("SECONDS: 150\nNOTE: Two-voice casting is chancy: make it twice.\nCAPTION:\n" + DUET_CAP + "\nLYRICS:\n" + DUET_LYR, "stop"))
srv.HELPER = {"url": "http://127.0.0.1:1/v1", "model": "stub", "timeout_s": 5}
b, code = srv.guide_skill({"room": "music", "mode": "music", "topic": "a duet, one rapper and one dancehall singer",
                           "context": {"mode": "music", "fields": {"seconds": 150}}})
lyr = (b.get("fields") or {}).get("lyrics", "")
check("accepted: 200, no problems, no retry", code == 200 and b.get("problems") == [] and b.get("retried") is False, b)
check("the role-tag lines survive the parse", lyr.count("[rap vocal]") == 2 and lyr.count("[sung vocal]") == 2, lyr[:200])
check("no [Singer A]/[Singer B] tag line", not re.search(r"(?im)^\s*\[\s*singer [ab]\s*\]\s*$", lyr))
check("the brain was given the duet rules", "Singer A" in ASKED[-1][0]["content"])

print()
print("FAILED: %d" % len(FAILED))
for n in FAILED:
    print("  - " + n)
sys.exit(1 if FAILED else 0)
