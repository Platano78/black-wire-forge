"""Gate for FB-2 R5, the writer's SAY reply: engines.parse_writer_reply returns
{"say": ...} for it, and POST /api/guide/skill's guide_skill() returns
{"ok": true, "say": ...} with no fields, every writer's system prompt carrying
the one shared SAY rule. The brain is a stub of _helper_chat. No browser.

Run: python3 tests/test_fb2_say.py
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


import _scratch_config  # noqa: E402,F401 -- must run before server.py's own exec_module below
import engines  # noqa: E402

spec = importlib.util.spec_from_file_location("srv_fb2_say", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

print("parse_writer_reply: SAY")


def parse(w, text):
    """-> the parsed reply, or {"error": ...} when it does not parse (so a RED run fails a check, not the run)."""
    try:
        return engines.parse_writer_reply(w, text)
    except ValueError as e:
        return {"error": str(e)}


song = engines.writer("audio", "song")
t2i = engines.writer("image", "t2i")
check("a SAY line is the reply", parse(song, "SAY: Music3 writes longer songs.")
      == {"say": "Music3 writes longer songs."})
check("its text runs on over the next lines, up to a key", parse(
    song, "SAY: Music3 writes longer songs.\nACE-Step is quicker.\nNOTE: ignored")
      == {"say": "Music3 writes longer songs.\nACE-Step is quicker."})
check("**bold** SAY and a think block are read too", parse(
    t2i, "<think>hm</think>\n**SAY:** Pick Balanced.") == {"say": "Pick Balanced."})
check("a QUESTION before it still wins", "question" in parse(song, "QUESTION: Sung?\nSAY: hi"))
check("a SAY after the fields started is no SAY", "values" in parse(
    t2i, "PROMPT: a lighthouse\nSAY: hi"))
check("an empty SAY is no SAY", "values" in parse(t2i, "SAY:\nPROMPT: a lighthouse"))

print("guide_skill: a SAY reply comes back as say, nothing filled")
ASKED = []


def fake_chat(messages, max_tokens=512, timeout=None):
    ASKED.append(messages)
    return "SAY: Music3 writes longer songs; ACE-Step is quicker.", "stop"


srv._helper_chat = fake_chat
srv.HELPER = {"url": "http://127.0.0.1:1/v1", "model": "stub", "timeout_s": 5}
rule = getattr(srv, "GUIDE_SKILL_SAY_RULE", None)
check("the server defines one shared SAY rule", isinstance(rule, str) and "SAY:" in rule, rule)
b, code = srv.guide_skill({"room": "music", "mode": "song", "topic": "what's the difference between Music3 and ACE?"})
check("200, ok, say, no fields", code == 200 and b.get("ok") is True and b.get("say")
      == "Music3 writes longer songs; ACE-Step is quicker." and "fields" not in b and "question" not in b, b)
check("one call, no retry", len(ASKED) == 1 and b.get("retried") is False, len(ASKED))
system = ASKED[-1][0]["content"] if ASKED else ""
check("the system prompt is the pack writer's own, then the SAY rule",
      bool(rule) and system == song["prompt"] + "\n\n" + rule, system[-200:])
b, code = srv.guide_skill({"room": "picture", "mode": "t2i", "topic": "which size is best?"})
check("t2i too: say comes back", code == 200 and "say" in b and "fields" not in b, b)
check("t2i: its writer's prompt carries the same rule", bool(rule) and ASKED[-1][0]["content"].endswith("\n\n" + rule))

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
