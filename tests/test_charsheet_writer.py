"""CHARS-1: the Characters guide's sheet writer. Contract, the parser + check
on the ten-section reply, and POST /api/guide/skill in process with a fake
helper (no network): the reference picture and the name go in, the Sheet
prompt field comes out, never straight to Make.

Run: python3 tests/test_charsheet_writer.py
"""
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import engines  # noqa: E402
import _scratch_config  # noqa: E402,F401
import _picture_server as ps  # noqa: E402
from engines import qwen_image  # noqa: E402
from _picture_server import FAILED, check  # noqa: E402

FILLER = ("The rust-red scarf knots at the left of the neck, the brass buckle sits on the figure's right hip, "
          "and the cropped grey jacket keeps its two chest pockets in every view of the same sheet. ")


def sheet(name="ROOKIE", drop=(), extra="", words=900):
    """A ten-section reply, ~`words` long, one paragraph per numbered line."""
    per = max(1, words // 10 // 30)
    lines = []
    for n in range(1, 11):
        if n in drop:
            continue
        head = "%d. Section %d shows the title \"%s\" and the panel labels \"FRONT\" \"SIDE\" \"BACK\". " % (n, n, name)
        lines.append((head + FILLER * per + (extra if n == 8 else "")).rstrip())
    return "\n".join(lines)


GOOD = sheet()
check("the fixture sheet is inside the word range", 700 <= len(GOOD.split()) <= 2000, len(GOOD.split()))

print("contract")
W = engines.writer("image", "charsheet")
check("a writer is registered for the sheet mode", W is not None)
check("it writes the Sheet prompt field, multiline, never checked at Make time",
      W["keys"] == {"PROMPT": "prompt"} and W["multiline"] == "PROMPT" and W["make_time"] is False
      and callable(W["check"]) and W["max_tokens"] >= 4096)
check("it reads the room's own reference picture field (a real image field)",
      W["pictures"] == "reference"
      and any(f["id"] == "reference" and f["type"] == "image" for f in engines.fields("image", "charsheet")))
check("its keys are real fields", set(W["keys"].values()) <= {f["id"] for f in engines.fields("image", "charsheet")})
check("the room has its own guide, named in rooms.json",
      next(r for r in engines.rooms() if r["id"] == "characters").get("guide") == "characters")
check("the mode lives in the characters room", engines.mode_room("image", "charsheet") == "characters")
P = qwen_image.CHARSHEET_WRITER_PROMPT
for label, needle in (("LOOKING UP, never UPWARD GAZE", "Never write \"UPWARD GAZE\"; write \"LOOKING UP\""),
                      ("labels of 1 to 3 common words", "1 to 3 common words"),
                      ("accessory side from the figure's own left and right", "FIGURE'S OWN left and right"),
                      ("4 to 7 identity locks", "4 to 7 things"), ("3 or 4 signature colours", "3 or 4 colours"),
                      ("ten numbered sections", "numbered 1. to 10."), ("900 to 1600 words", "900 to 1600 words"),
                      ("a one-word title", "The title is one word"), ("machine states for robots", "machine states"),
                      ("asks for a missing name", "What should the sheet be called?"),
                      ("no pixel sizes", "Never write a pixel size")):
    check("writer prompt: " + label, needle in P, needle)
check("writer prompt: no copied source text (nothing from the unlicensed workflow)",
      "ARCHITECT" not in P.upper())

print("parser + check on the reply")
parsed = engines.parse_writer_reply(W, "NOTE: I read it as a scout.\nPROMPT: " + GOOD)
check("the parser accepts ten sections and keeps the note", "values" in parsed and parsed["values"]["prompt"].startswith("1. Section 1")
      and parsed["note"] == "I read it as a scout.", parsed)
check("...all ten survive in the field", [l[:3] for l in parsed["values"]["prompt"].splitlines()][:10]
      == ["1. ", "2. ", "3. ", "4. ", "5. ", "6. ", "7. ", "8. ", "9. ", "10."])
V = {"name": "Rookie"}
check("check: a good sheet has no problems", W["check"](dict(V, prompt=GOOD), {}) == [], W["check"](dict(V, prompt=GOOD), {}))
p = W["check"](dict(V, prompt=sheet(drop=(3, 7))), {})
check("check: dropped sections are named", p and "1, 2, 4, 5, 6, 8, 9, 10" in p[0], p)
p = W["check"](dict(V, prompt="A long paragraph with no numbering. " * 40), {})
check("check: no numbered sections at all", p and "no numbered sections" in p[0], p)
p = W["check"](dict(V, prompt=sheet(extra="The third head shows \"UPWARD GAZE\" under it. ")), {})
check("check: UPWARD GAZE is refused and LOOKING UP offered", any("UPWARD GAZE" in x and "LOOKING UP" in x for x in p), p)
check("check: the ban is case-blind", W["check"](dict(V, prompt=sheet(extra="upward gaze ")), {}) != [])
check("check: LOOKING UP is fine", W["check"](dict(V, prompt=sheet(extra="\"LOOKING UP\" ")), {}) == [])
p = W["check"](dict(V, prompt=sheet(words=100)), {})
check("check: far too short is a problem", any("words" in x for x in p), p)
p = W["check"](dict(V, prompt=sheet(name="ROOKY")), {})
check("check: the name missing from the sheet is named", any("Rookie" in x for x in p), p)
check("check: no name on the form, nothing to check", W["check"](dict(prompt=sheet(name="ANYONE")), {}) == [])

print("POST /api/guide/skill")
srv, STORE, LANE = ps.start("charsheet_writer")
REQ = ps.HELPER_STATE["requests"]
UP = {"lane": "t", "upload": "hero.png"}
CTX = {"mode": "charsheet", "fields": {"name": "Rookie"}}


def skill(body, *replies):
    REQ.clear()
    ps.HELPER_STATE["replies"] = list(replies)
    return srv.guide_skill(body)


try:
    modes = {(c, m["id"]): m for c, v in srv.Handler.engines_payload(srv.Handler.__new__(srv.Handler), {"lane": ["t"]}).items()
             if c not in ("rooms", "helper") for m in v["modes"]}
    w = modes[("image", "charsheet")]["writer"]
    check("/api/engines: the sheet writer, its picture field, and that it fills the Sheet prompt",
          w.get("label") == "Sheet writer" and w.get("pictures") == "reference" and w.get("fills") == ["prompt"], w)

    b, code = skill({"room": "characters", "mode": "charsheet", "topic": "", "attached": [UP], "context": CTX},
                    "NOTE: read as a scout.\nPROMPT: " + GOOD)
    check("picture + name, empty box: 200 and the Sheet prompt field is filled",
          code == 200 and b.get("fields") == {"prompt": GOOD} and b["problems"] == [] and not b["retried"], (code, b.get("error"), b.get("problems")))
    check("...it is the pack's own writer prompt", REQ[0]["messages"][0]["content"] == qwen_image.CHARSHEET_WRITER_PROMPT)
    u = REQ[0]["messages"][1]["content"]
    check("...the request carries the name through the room line", 'Name: "Rookie"' in u, u[:300])
    check("...and says the picture is attached (this helper cannot see it)", "cannot see pictures" in u, u[-120:])
    check("...the note comes back", b.get("note") == "read as a scout.")
    check("...the writer's own long-reply budget was used", REQ[0].get("max_tokens", 0) >= 4096, REQ[0].get("max_tokens"))

    b, code = skill({"room": "characters", "mode": "charsheet", "attached": [UP], "context": CTX}, "PROMPT: " + GOOD)
    check("no topic key at all is fine too", code == 200 and b.get("fields") == {"prompt": GOOD}, (code, b.get("error")))

    b, code = skill({"room": "characters", "mode": "charsheet", "topic": "x" * 9000, "attached": [UP], "context": CTX},
                    "PROMPT: " + GOOD)
    check("a box holding a whole earlier sheet is read as empty, not refused", code == 200 and b.get("fields"), (code, b.get("error")))

    b, code = skill({"room": "characters", "mode": "charsheet", "topic": "", "attached": [], "context": CTX})
    check("no picture: a plain sentence, the brain never asked", b.get("missing", "").startswith("Add a picture") and not REQ, b)

    b, code = skill({"room": "characters", "mode": "charsheet", "topic": "", "attached": [UP], "context": CTX},
                    "PROMPT: " + sheet(extra="\"UPWARD GAZE\" "), "PROMPT: " + GOOD)
    check("UPWARD GAZE in a draft: one retry, the fix named to the writer, the good draft kept",
          b.get("retried") and b["fields"]["prompt"] == GOOD and b["problems"] == []
          and "LOOKING UP" in REQ[1]["messages"][1]["content"], b)

    b, code = skill({"room": "characters", "mode": "charsheet", "topic": "", "attached": [UP], "context": CTX},
                    "PROMPT: " + sheet(drop=(5,)), "PROMPT: " + sheet(drop=(5,)))
    check("a draft that keeps dropping a section ships with the problem named, never silently",
          b["retried"] and b["problems"] and "1, 2, 3, 4, 6, 7, 8, 9, 10" in b["problems"][0], b.get("problems"))

    b, code = skill({"room": "characters", "mode": "charsheet", "topic": "", "attached": [UP],
                     "context": {"mode": "charsheet", "fields": {}}},
                    "QUESTION: What should the sheet be called? One word works best.\nOPTIONS: Rookie | Scout")
    check("no name on the form: the writer's question comes back, with choices",
          b.get("question", "").startswith("What should the sheet be called") and b.get("options") == ["Rookie", "Scout"], b)
finally:
    LANE.terminate()

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
