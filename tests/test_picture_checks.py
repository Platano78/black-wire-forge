"""Gate for P3d's parsers and checks: the t2i shape reply, the edit writer's
MISSING answer, the turntable fixer's own fix words and SETTINGS line, and
each writer's check (t2i, edit, the edit's picture count, the 3D source
picture). No server: tests/test_picture_skill.py covers that.

Run: python3 tests/test_picture_checks.py
"""
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engines  # noqa: E402
from engines import mesh3d, qwen_image  # noqa: E402
from _picture_server import FAILED, GOOD, SRC, check  # noqa: E402


T2I, EDIT, TT = engines.writer("image", "t2i"), engines.writer("image", "edit"), engines.reviser("3d", "turntable")
print("parsers")
p = engines.parse_writer_reply(T2I, "WIDTH: 1664\nHEIGHT: 928\nNEGATIVE: NONE\nNOTE: a film still.\nPROMPT: Two monsters.")
check("t2i reply: shape and prompt, NONE left out",
      p == {"values": {"width": "1664", "height": "928", "prompt": "Two monsters."}, "note": "a film still."}, p)
check("edit reply: MISSING is its own answer",
      engines.parse_writer_reply(EDIT, "MISSING: Add the dog as picture 2.\nPROMPT: x") == {"missing": "Add the dog as picture 2."})
check("a writer without pictures: MISSING is not a key", "missing" not in engines.parse_writer_reply(T2I, "MISSING: x\nPROMPT: y"))
full = engines.parse_reviser_reply(TT, "DIAGNOSIS: grain.\nFIX: settings\nPROMPT:\nSETTINGS: frames = 120; Render samples=64; bad")
check("turntable reply: settings by id or label, junk left out",
      full.get("settings") == {"frames": "120", "render samples": "64"} and full["prompt"] == "", full)
pic = engines.parse_reviser_reply(TT, "DIAGNOSIS: handle.\nFIX: picture\nPROMPT: A mug.\nSETTINGS:")
check("turntable reply: a picture fix carries its prompt", pic["prompt"] == "A mug." and "settings" not in pic, pic)
check("turntable reply: none needs no prompt", engines.parse_reviser_reply(TT, "DIAGNOSIS: x\nFIX: none")["fix"] == "none")
for label, text in (("a picture fix with no prompt", "FIX: picture\nPROMPT:"),
                    ("a settings fix with no settings", "FIX: settings\nSETTINGS:"),
                    ("the default words, not this fixer's", "FIX: reroll\nPROMPT: x")):
    try:
        engines.parse_reviser_reply(TT, text)
        check("turntable reply: %s raises" % label, False)
    except ValueError:
        check("turntable reply: %s raises" % label, True)
check("the picture fixer keeps edit/reroll",
      engines.parse_reviser_reply(engines.reviser("image", "t2i"), "FIX: edit\nPROMPT: gold")["fix"] == "edit")

print("checks")
sq = {"width": 1328, "height": 1328}
check("t2i check: a full positive prompt is fine", qwen_image.t2i_check(dict(sq, prompt=GOOD), {}) == [])
pr = qwen_image.t2i_check(dict(sq, prompt=GOOD + " There are no people."), {})
check("t2i check: a negation is named", len(pr) == 1 and '"no"' in pr[0] and "Things to avoid" in pr[0], pr)
check("t2i check: a thin prompt is named", "only 3 words" in " ".join(qwen_image.t2i_check({"prompt": "godzilla vs ghidorah"}, {})))
check("t2i check: width and height from different shapes are named", any(
    "not one of the shapes" in x for x in qwen_image.t2i_check({"prompt": GOOD, "width": 1664, "height": 1328}, {})))
check("edit missing: none attached",
      qwen_image.edit_missing("make it night", 0) == "Add the picture to change under Pictures to work from first, then ask again.")
m = qwen_image.edit_missing("make the man in image two have the clothes of the man in picture one", 1)
check("edit missing: picture 2 named, 1 attached", m and "picture 2" in m and "only 1 is attached" in m, m)
check("edit missing: ordinal words count", "picture 3" in (qwen_image.edit_missing("the second photo and the third picture", 2) or ""))
check("edit missing: enough attached is None", qwen_image.edit_missing("swap picture 1 and picture 2", 2) is None
      and qwen_image.edit_missing("make it night", 1) is None)
ec = qwen_image.edit_check({"prompt": "Put the lamp from picture 2 into <image1> and <image3>."}, {"pictures": 2})
check("edit check: a bare picture number and a label past the count", len(ec) == 2 and "<image2>" in ec[0] and "<image3>" in ec[1], ec)
check("edit check: labels within the count are fine",
      qwen_image.edit_check({"prompt": "Put the lamp from <image2> on the desk in <image1>."}, {"pictures": 2}) == [])
check("3D source check: the rules met is fine", mesh3d.source_picture_check({"prompt": SRC}, {}) == [])
sc = mesh3d.source_picture_check({"prompt": "A brass lantern from the front, not cropped."}, {})
check("3D source check: no view, no whole, no background, a negation", len(sc) == 4, sc)

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
