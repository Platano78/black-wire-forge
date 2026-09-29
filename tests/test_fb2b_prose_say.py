"""Gate for FB-2b: a writer reply with NO recognised head (no writer key, no
NOTE/QUESTION/OPTIONS/MISSING/SAY) that was not cut off is the guide talking:
POST /api/guide/skill's guide_skill() returns {"ok": true, "say": <reply>}
instead of the shape error. Any recognised key keeps today's parse; a cut-off
reply keeps the cut-off error. The brain is a stub of _helper_chat. No browser.

Run: python3 tests/test_fb2b_prose_say.py
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
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402,F401
import engines  # noqa: E402

spec = importlib.util.spec_from_file_location("srv_fb2b", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

BALLAD = 'There is no single "best" BPM for a ballad, but they are typically slow, ranging from 60 to 80 BPM.'
TWO = "Ballads sit around 60 to 80 BPM.\n\nGo slower for a heavier mood, faster for a hopeful one."
song = engines.writer("audio", "song")


def parse(text, prose_say=True):
    try:
        try:
            return engines.parse_writer_reply(song, text, prose_say=prose_say)
        except TypeError:   # a build without the kwarg (the RED run): the plain call
            return engines.parse_writer_reply(song, text)
    except ValueError as e:
        return {"error": str(e)}


print("parse_writer_reply: prose with no recognised head")
check("the ballad reply is a say", parse(BALLAD) == {"say": BALLAD}, parse(BALLAD))
check("two paragraphs are both kept", parse("  " + TWO + "\n") == {"say": TWO}, parse(TWO))
check("without prose_say it still raises (default unchanged)", "error" in parse(BALLAD, prose_say=False))
check("an empty reply is no say", "error" in parse("  \n"))

print("parse_writer_reply: drafts stay drafts")
check("plain keys -> values", parse("TAGS: warm pop\nLYRICS:\n[Verse]\nhi").get("values", {}).get("tags") == "warm pop")
check("lower-case keys -> values", parse("tags: warm pop\nlyrics:\n[Verse]\nhi").get("values", {}).get("tags") == "warm pop")
check("**bold** keys -> values", parse("**Tags:** warm pop\n**Lyrics:**\n[Verse]\nhi").get("values", {}).get("tags") == "warm pop")
check("a key with no multiline still errors, not say", "error" in parse("TAGS: warm pop"))
check("a NOTE-only reply still errors, not say", "error" in parse("NOTE: hello"))

print("guide_skill through the server")
CALLS = []
REPLY = {"text": BALLAD, "finish": "stop"}


def fake_chat(messages, max_tokens=512, timeout=None):
    CALLS.append(max_tokens)
    return REPLY["text"], REPLY["finish"]


srv._helper_chat = fake_chat
srv.HELPER = {"url": "http://127.0.0.1:1/v1", "model": "stub", "timeout_s": 5}
REQ = {"room": "music", "mode": "song", "topic": "which BPM is best for a ballad?"}
b, code = srv.guide_skill(dict(REQ))
check("prose -> 200 ok say", code == 200 and b.get("ok") is True and b.get("say") == BALLAD and "fields" not in b, (code, b))
REPLY.update(text=TWO)
b, code = srv.guide_skill(dict(REQ))
check("two paragraphs -> say, both kept", code == 200 and b.get("say") == TWO, (code, b))
REPLY.update(text=BALLAD, finish="length")
b, code = srv.guide_skill(dict(REQ))
check("finish=length -> the cut-off error, 502", code == 502 and b.get("ok") is False
      and b.get("error") == srv.HELPER_CUT_OFF, (code, b.get("error")))
REPLY.update(text="", finish="stop")
b, code = srv.guide_skill(dict(REQ))
check("empty -> the shape error", code == 502 and b.get("error") == srv.GUIDE_SKILL_SHAPE_ERROR, (code, b.get("error")))

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
