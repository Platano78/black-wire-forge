"""Gate for P3b, the Sound writers (API half): POST /api/guide/skill's
guide_skill() for music (two multiline fields, the 2048-token budget), cover
(refused in one plain sentence with no track, KEEP with one) and sfx, and the
Make-time guard for music and sfx in generate(). The brain is a stub of
_helper_chat that records what it was asked. No browser, no network.

Run: python3 tests/test_sound_writer_api.py
"""
import importlib.util
import os
import sys
import time

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
spec = importlib.util.spec_from_file_location("srv_sound_writers", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
from engines import audio  # noqa: E402

ASKED, REPLIES = [], []


def fake_chat(messages, max_tokens=512, timeout=None):
    ASKED.append({"messages": messages, "max_tokens": max_tokens})
    return REPLIES.pop(0), "stop"


srv._helper_chat = fake_chat
srv.HELPER = {"url": "http://127.0.0.1:1/v1", "model": "stub", "timeout_s": 5}
CAPTION = ("Global Metadata\n" + "Gritty 90s boom bap with dusty drums and a jazz piano loop. " * 20
           + "\nVocal Details\nA male rapper with a low, gravelly voice.\nArrangement\nIntro: vinyl crackle, then drums.")
LYRICS = "\n\n".join("[%s]\n" % s + "\n".join(["kicks on the shelf and the box is new"] * 8) for s in ("Verse", "Hook", "Verse", "Hook"))
GOOD = "SECONDS: 150\nNOTE: I chose a male voice.\nCAPTION:\n" + CAPTION + "\nLYRICS:\n" + LYRICS

print("music: CAPTION and LYRICS both land, with a 2048-token budget")
REPLIES[:] = [GOOD]
b, code = srv.guide_skill({"room": "music", "mode": "music", "topic": "a 90s rap about my brother",
                           "context": {"mode": "music", "fields": {"seconds": 150}}})
check("200 with no problems, no retry", code == 200 and b.get("problems") == [] and b.get("retried") is False, b)
check("caption, lyrics and seconds are the fields", b.get("fields") == {"caption": CAPTION, "lyrics": LYRICS, "seconds": 150.0},
      b.get("fields"))
check("the brain got the music prompt and 2048 tokens", ASKED[-1]["max_tokens"] == 2048
      and ASKED[-1]["messages"][0]["content"] == audio.ENGINE["writers"]["music"]["prompt"])
REPLIES[:] = ["CAPTION:\n**Global Metadata**\nrap\nLYRICS:\nNONE"] * 2
b, code = srv.guide_skill({"room": "music", "mode": "music", "topic": "a rap"})
check("a Markdown caption is retried once, its problems returned", b.get("retried") is True
      and any("Markdown" in p for p in b.get("problems", [])) and "Your draft had these problems:" in ASKED[-1]["messages"][1]["content"], b)

print("cover: no track, one plain sentence and no brain call")
n = len(ASKED)
b, code = srv.guide_skill({"room": "cover", "mode": "cover", "topic": "a reggae version", "context": {"mode": "cover", "fields": {}}})
check("409 with the pack's sentence, naming the field", code == 409 and b.get("error") == audio.COVER_NEEDS_TRACK
      and b.get("needs") == "source_audio_name", b)
check("the brain was not asked", len(ASKED) == n)
REPLIES[:] = ["STYLE: English, reggae, offbeat guitar, warm male vocals\nMODE: NONE\nLYRICS:\nKEEP"]
ctx = {"mode": "cover", "fields": {"source_audio_name": "ballad.mp3", "lyrics": "[Verse]\nold words"}}
b, code = srv.guide_skill({"room": "cover", "mode": "cover", "topic": "a reggae version, same words", "context": ctx})
check("with a track: KEEP leaves the lyrics, the style lands", code == 200 and b.get("fields") ==
      {"style": "English, reggae, offbeat guitar, warm male vocals"} and b.get("problems") == [], b)
check("the room line names the track", 'Source track: "ballad.mp3"' in ASKED[-1]["messages"][1]["content"])

print("sfx: speech is pointed elsewhere, a sound is written")
REPLIES[:] = ["QUESTION: This room makes sounds, not speech: for a voice saying words, use the Talking Head room. "
              "Is there a sound without words you want here instead?"]
b, code = srv.guide_skill({"room": "sfx", "mode": "sfx", "topic": "a man saying welcome"})
check("the pointer comes back as the guide's one line", code == 200 and "Talking Head" in (b.get("question") or ""), b)
REPLIES[:] = ["SECONDS: 3\nNEGATIVE: NONE\nPROMPT: A heavy wooden door slamming shut in a stone hall, high-quality, stereo"]
b, code = srv.guide_skill({"room": "sfx", "mode": "sfx", "topic": "a door slam in a castle"})
check("prompt and seconds land", b.get("fields") == {"seconds": 3.0, "prompt": "A heavy wooden door slamming shut in a stone "
      "hall, high-quality, stereo"}, b)

print("Make-time guard")
lane = srv.LANE_BY_ID["x"]
lane["caps"] = ["audio"]
lane["models"] = {"music3_unet": "m.safetensors", "music3_clip": "c.safetensors", "music3_vae": "v.safetensors",
                  "sao_ckpt": "s.safetensors", "sao_clip": "t.safetensors"}
with srv.STATE_LOCK:
    srv.LANE_STATE["x"] = {"up": True, "checked": time.time(), "err": ""}
srv.dispatch = lambda *a: {"ok": True, "job": {"id": "j", "status": "queued"}}
b, code = srv.generate({"lane": "x", "kind": "audio", "mode": "music", "caption": "**Genre:** boom bap, a rapper",
                        "lyrics": "", "seconds": 150})
check("music: a Markdown caption with no lyrics needs a confirm", code == 409 and b.get("needs_confirm")
      and any("Markdown" in p for p in b["problems"]), b)
b, code = srv.generate({"lane": "x", "kind": "audio", "mode": "music", "caption": CAPTION, "lyrics": LYRICS, "seconds": 150})
check("music: the writer's own shape renders", code == 200 and b.get("ok"), b)
b, code = srv.generate({"lane": "x", "kind": "audio", "mode": "sfx", "prompt": "a woman saying hello", "seconds": 2})
check("sfx: speech needs a confirm", code == 409 and b.get("needs_confirm"), b)

print()
print("FAILED: %d" % len(FAILED))
for n in FAILED:
    print("  - " + n)
sys.exit(1 if FAILED else 0)
