"""Gate for P3b, the Sound writers (pack level, part 1): the four writers on
P2's contract, the several-multiline-keys and keep_token parser, and the
music check (the owner's "hot garbage" caption included). Part 2 is
tests/test_sound_checks.py; the API half is tests/test_sound_writer_api.py.

Run: python3 tests/test_sound_writers.py
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

# The caption of owner jobs c05bad44bbfb / eb7682dcebf4 (2026-09-24), cut: Markdown, a smell, no lyrics.
OWNER_CAPTION = ("**Global Metadata**\n**Genre:** 1990s East Coast Boom Bap\n**Setting:** A cramped New York City "
                 "apartment in 1995, smelling of dust and electronics\n\n**Vocal Details**\n**Style:** Raspy, "
                 "authoritative Brooklyn-style flow with a conversational delivery\n\n**Arrangement**\n**Intro:** "
                 "Begins with 4 bars of heavy vinyl crackle.")
PLAIN = ("Global Metadata\n" + "Gritty 90s boom bap with dusty drums. " * 30 + "\nVocal Details\nA male rapper with "
         "a low, gravelly voice and a relaxed flow.\nArrangement\nIntro: a vinyl crackle, then the drums drop.")
RAP = "\n\n".join("[%s]\n" % s + "\n".join(["line of the verse right here"] * 8) for s in ("Verse", "Hook", "Verse", "Hook"))

print("contract: four new writers")
W = {m: engines.writer("audio", m) for m in ("music", "yue2", "cover", "sfx")}
check("each has a label, a prompt and a check", all(w and w["label"] and w["prompt"].strip() and callable(w["check"])
                                                  for w in W.values()))
check("music: CAPTION then LYRICS are multiline, 2048 tokens", W["music"]["multiline"] == ["CAPTION", "LYRICS"]
      and W["music"]["keys"] == {"SECONDS": "seconds", "CAPTION": "caption", "LYRICS": "lyrics"}
      and W["music"]["max_tokens"] == 2048)
check("cover: KEEP leaves the lyrics, needs the source track", W["cover"]["keep_token"] == "KEEP"
      and W["cover"]["needs"] == {"source_audio_name": audio.COVER_NEEDS_TRACK})
check("sfx: PROMPT, NEGATIVE, SECONDS", W["sfx"]["keys"] == {"SECONDS": "seconds", "NEGATIVE": "negative", "PROMPT": "prompt"})

print("parser: several multiline keys, keep_token")
r = engines.parse_writer_reply(W["music"], "SECONDS: 150\nNOTE: I chose a male voice.\nCAPTION:\nGlobal Metadata\n"
                               "Dusty drums.\nVocal Details\nA rapper.\nLYRICS:\n[Verse]\nline one\n\n[Hook]\nline two")
check("caption runs to the LYRICS line", r["values"]["caption"] == "Global Metadata\nDusty drums.\nVocal Details\nA rapper.", r)
check("lyrics run to the end, seconds and note kept", r["values"]["lyrics"] == "[Verse]\nline one\n\n[Hook]\nline two"
      and r["values"]["seconds"] == "150" and r["note"] == "I chose a male voice.", r)
r = engines.parse_writer_reply(W["music"], "CAPTION:\nGlobal Metadata\nx\nNOTE: done\nLYRICS:\nNONE")
check("a NOTE ends one part; the next still starts; NONE is empty",
      r["values"] == {"caption": "Global Metadata\nx", "lyrics": ""} and r["note"] == "done", r)
try:
    engines.parse_writer_reply(W["music"], "LYRICS:\n[Verse]\nhi")
    check("no CAPTION line raises", False)
except ValueError:
    check("no CAPTION line raises", True)
check("KEEP leaves the lyrics field out", engines.parse_writer_reply(W["cover"], "STYLE: swing, male vocals\nLYRICS:\nKEEP")
      == {"values": {"style": "swing, male vocals"}, "note": ""})
check("a question still wins", engines.parse_writer_reply(W["music"], "QUESTION: Sung, rapped, or instrumental?\n"
      "OPTIONS: Sung | Rapped | Instrumental")["options"] == ["Sung", "Rapped", "Instrumental"])

print("music check")
p = audio.music_check({"caption": OWNER_CAPTION, "lyrics": "", "seconds": 150.0}, {})
check("the owner's caption: Markdown, a smell, no words for a rapper", all(any(w in x for x in p) for w in (
      "Markdown", "names smelling", "no real words")) and not any("has no" in x for x in p), p)
p = audio.music_check({"caption": PLAIN, "lyrics": RAP, "seconds": 150.0}, {})
check("a plain caption with a rapper and lyrics for 150 s passes", p == [], p)
p = audio.music_check({"caption": PLAIN, "lyrics": "[Verse]\na\n[Hook]\nb", "seconds": 150.0}, {})
check("too few sections for 150 s", len(p) == 1 and "at least 4" in p[0], p)
inst = PLAIN.replace("A male rapper with a low, gravelly voice and a relaxed flow.", "Instrumental, no vocals. A flute leads.")
check("an instrumental with no lyrics passes", audio.music_check({"caption": inst, "lyrics": "", "seconds": 150.0}, {}) == [])
check("an instrumental with lyrics: the words would be lost", "describe no voice" in " ".join(
    audio.music_check({"caption": inst, "lyrics": RAP, "seconds": 150.0}, {})))
p = audio.music_check({"caption": "chill lo-fi beat, soft piano", "lyrics": ""}, {})
check("a bare caption names the missing sections", len(p) == 1 and "Global Metadata or Vocal Details or Arrangement" in p[0], p)
p = audio.music_check({"caption": "Global Metadata\nlo-fi.\nVocal Details\nInstrumental.\nArrangement\npiano.", "lyrics": ""}, {})
check("a short structured caption is named as short", len(p) == 1 and "words; this model wants about 250-450" in p[0], p)

print()
print("FAILED: %d" % len(FAILED))
for n in FAILED:
    print("  - " + n)
sys.exit(1 if FAILED else 0)
