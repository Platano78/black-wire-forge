"""Gate for P3d's pack contract: the Picture guide's t2i and edit writers, the
3D room's source-picture writer (its words land in the Picture room's t2i
mode), the turntable fixer's own fix words, and edit_in ("Edit this
result"), plus the rules each writer's prompt must hold. Parsers and checks:
tests/test_picture_checks.py; the server: tests/test_picture_skill.py and
tests/test_picture_revise.py; the page: tests/test_guide_ui.py.

Run: python3 tests/test_picture_writers.py
"""
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engines  # noqa: E402
from engines import mesh3d, qwen_image  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


print("contract: the writers and the fixer")
T2I, EDIT, MESH = engines.writer("image", "t2i"), engines.writer("image", "edit"), engines.writer("3d", "mesh")
TT = engines.reviser("3d", "turntable")
check("t2i writer: prompt, width, height and negative", T2I and T2I["keys"] ==
      {"WIDTH": "width", "HEIGHT": "height", "NEGATIVE": "negative", "PROMPT": "prompt"}, T2I and T2I["keys"])
check("t2i writer: PROMPT multiline; its check never runs at Make time",
      T2I["multiline"] == "PROMPT" and callable(T2I["check"]) and T2I["make_time"] is False)
check("edit writer: writes about the mode's own pictures field", EDIT and EDIT["pictures"] == "ref_images"
      and EDIT["keys"] == {"NEGATIVE": "negative", "PROMPT": "prompt"} and callable(EDIT["missing"]))
check("edit writer: ref_images is the edit mode's real image list",
      any(f["id"] == "ref_images" and f["type"] == "image_list" for f in engines.fields("image", "edit")))
check("3D writer: its words are for the Picture room's t2i mode",
      MESH and MESH["target"] == {"cap": "image", "mode": "t2i"} and engines.mode_room("image", "t2i") == "picture")
check("3D writer: its keys are t2i's own fields",
      set(MESH["keys"].values()) <= {f["id"] for f in engines.fields("image", "t2i")}, MESH["keys"])
check("3D writer: a topic label (the mode has no text field), never at Make time",
      MESH["topic_label"] and MESH["make_time"] is False
      and not any(f["type"] in ("text", "textarea") for f in engines.fields("3d", "mesh")))
check("turntable fixer: picture -> t2i's prompt, settings -> its own fields, none -> advice",
      TT and TT["fixes"] == {"picture": {"target": {"cap": "image", "mode": "t2i"}, "fills": "prompt"},
                             "settings": {"settings": True}, "none": {}} and "SETTINGS" in TT["keys"])
check("edit_in: t2i and edit results open in edit",
      engines.edit_in("image", "t2i") == "edit" and engines.edit_in("image", "edit") == "edit")
check("edit_in: nobody else", engines.edit_in("image", "pixelart") is None and engines.edit_in("3d", "mesh") is None)

print("prompts: each writer holds its rules")
P, E, S, R = qwen_image.T2I_WRITER_PROMPT, qwen_image.EDIT_WRITER_PROMPT, mesh3d.SOURCE_PICTURE_PROMPT, TT["prompt"]
RULES = (
    (P, "t2i", (("the finished picture", "Describe the FINISHED picture"), ("walks the frame", "Then walk the frame"),
                ("opening sentence", "Open with one sentence naming the style"), ("counts", "COUNTS: say how many"),
                ("named subject by appearance", "Keep the name AND describe how it looks"),
                ("each named subject on its own", "Describe each named subject in its own sentence"),
                ("positive only", "Positive only"), ("no ratio in the prompt", "Never write a ratio"),
                ("a short brief, a full description", "about 80 to 150 words"), ("asks the subject", "SUBJECT:"),
                ("asks a count", "COUNT:"), ("asks the style", "STYLE:"), ("asks the shape", "SHAPE:"),
                ("never asks what it can choose", "Never ask about the light"))),
    (E, "edit", (("picture 1 / picture 2", "picture 1, picture 2"), ("the engine's label", "call them <image1>, <image2>"),
                 ("size follows picture 1", "takes its size and shape from <image1>"), ("the canvas", "CANVAS"),
                 ("a MISSING reply", "MISSING:"), ("never past the count", "Never name a picture number higher"),
                 ("the grounding line", "The last line of the message says how many pictures"))),
    (S, "3D source", (("one object", "ONE object"), ("the whole object", "The WHOLE object in frame"),
                      ("three-quarter view", "A three-quarter view"), ("even light", "Soft, even studio light"),
                      ("contrasting background", "A plain background in a colour that stands apart"),
                      ("thin parts", "Thin parts"), ("positive only", "Positive only"))),
    (R, "turntable fixer", (("one angle", "one angle only"), ("never the unseen side", "never describe or judge a side"),
                            ("the cause table", "KNOWN CAUSES"), ("never both", "never both"),
                            ("likeness", "cannot be judged from one still"))))
for text, who, needles in RULES:
    for label, needle in needles:
        check("%s prompt: %s" % (who, label), needle in text)
for name, w, h in qwen_image.T2I_SHAPES:
    check("t2i shape %s: in the prompt, multiples of 16, inside the fields' range" % name,
          ("%s = %d x %d" % (name, w, h)) in P and w % 16 == 0 and h % 16 == 0 and 256 <= min(w, h) and max(w, h) <= 2048)
check("t2i prompt: its examples avoid the trial topics",
      not any(t in P.split("EXAMPLE 1")[1].lower() for t in ("godzilla", "ghidorah", "dog", "moon", "lighthouse", "eiffel")))
for f in engines.fields("3d", "turntable"):
    if f["type"] in ("int", "number", "select"):
        check("turntable fixer prompt: lists setting %s" % f["id"], ("%s = %s" % (f["id"], f["label"])) in R)

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
