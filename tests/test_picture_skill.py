"""Gate for P3d's POST /api/guide/skill paths, in process: the t2i writer
(shape, retry, no Make-time confirm), the edit writer (attached pictures, a
missing picture answered before the brain), the 3D source picture (its
draft names the Picture room), and /api/engines' writer and edit_in keys.

Run: python3 tests/test_picture_skill.py
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
from engines import mesh3d, qwen_image  # noqa: E402
from _picture_server import FAILED, GOOD, SRC, check  # noqa: E402

srv, STORE, LANE = ps.start("picture_skill")
REQ = ps.HELPER_STATE["requests"]


def skill(body, *replies):
    REQ.clear()
    ps.HELPER_STATE["replies"] = list(replies)
    return srv.guide_skill(body)


def user(i=0):
    return REQ[i]["messages"][1]["content"] if len(REQ) > i else ""


try:
    print("engines payload")
    modes = {(c, m["id"]): m for c, v in srv.Handler.engines_payload(srv.Handler.__new__(srv.Handler), {"lane": ["t"]}).items()
             if c not in ("rooms", "helper") for m in v["modes"]}
    check("t2i, edit: writers and edit_in", modes[("image", "t2i")]["writer"] == {"label": "Picture prompt writer"}
          and modes[("image", "edit")]["writer"] == {"label": "Edit writer", "pictures": "ref_images"}
          and modes[("image", "t2i")]["edit_in"] == modes[("image", "edit")]["edit_in"] == "edit")
    mw = modes[("3d", "mesh")]["writer"]
    check("mesh: its writer's target room and topic label", mw["target"]["room"] == "picture"
          and mw["target"]["room_name"] == "Picture" and mw["topic_label"], mw)
    check("every other mode: edit_in null", all(m["edit_in"] is None for k, m in modes.items() if k[1] not in ("t2i", "edit")))

    print("t2i")
    b, code = skill({"room": "picture", "mode": "t2i", "topic": "a wide shot of godzilla vs mechaking ghidorah"},
                    "WIDTH: 1664\nHEIGHT: 928\nNEGATIVE: NONE\nNOTE: a film still.\nPROMPT: " + GOOD)
    check("t2i: prompt and shape", code == 200 and b.get("fields") == {"width": 1664, "height": 928, "prompt": GOOD}
          and b["problems"] == [] and not b["retried"] and "target" not in b, b)
    check("t2i: the pack's own prompt", REQ[0]["messages"][0]["content"] == qwen_image.T2I_WRITER_PROMPT)
    b, code = skill({"room": "picture", "mode": "t2i", "topic": "godzilla vs ghidorah"},
                    "PROMPT: godzilla vs ghidorah", "NEGATIVE: NONE\nPROMPT: " + GOOD)
    check("t2i: a thin prompt gets one retry, the problem named", b.get("retried") and b["fields"]["prompt"] == GOOD
          and "only 3 words" in user(1), b)
    check("t2i: no Make-time confirm", srv._make_time_problems({"prompt": "a room with no people"}, srv.LANE_BY_ID["t"],
                                                               {"t2i": True}, "image", "t2i") == [])

    print("edit")
    UP, UP2 = {"lane": "t", "upload": "a.png"}, {"lane": "t", "upload": "b.png"}
    b, _ = skill({"room": "picture", "mode": "edit", "topic": "make it night", "attached": []})
    check("edit: none attached: a sentence, the brain never asked", b.get("missing", "").startswith("Add the picture") and not REQ, b)
    b, _ = skill({"room": "picture", "mode": "edit", "topic": "make it night"})
    check("edit: no attached key is none attached", "missing" in b, b)
    b, _ = skill({"room": "picture", "mode": "edit", "topic": "put the dog from picture 2 on the sofa", "attached": [UP]})
    check("edit: picture 2 named, one attached: a sentence, no question", "picture 2" in b.get("missing", "")
          and "question" not in b and not REQ, b)
    b, code = skill({"room": "picture", "mode": "edit", "topic": "the man in picture 2 wears the jacket from picture 1",
                     "attached": [UP, UP2]}, "NEGATIVE: NONE\nNOTE: <image2> is the canvas.\nPROMPT: Replace the clothing "
                    "of the man in <image2> with the jacket from <image1>, keeping everything else in <image2> unchanged.")
    check("edit: two attached, the instruction written", code == 200 and b["fields"]["prompt"].startswith("Replace") and b["problems"] == [], b)
    check("edit: grounded on both, unseen", user().endswith("[The user attached 2 pictures, but this helper cannot see pictures.]"), user()[-90:])
    check("edit: the edit writer's prompt", REQ[0]["messages"][0]["content"] == qwen_image.EDIT_WRITER_PROMPT)
    b, _ = skill({"room": "picture", "mode": "edit", "topic": "make it night", "attached": [UP]},
                 "PROMPT: Change the scene to night.\n\n1 picture attached.")
    check("edit: a copied grounding line never reaches the prompt (live trial)", b["fields"]["prompt"] == "Change the scene to night.", b)
    b, _ = skill({"room": "picture", "mode": "edit", "topic": "the other jacket", "attached": [UP]}, "MISSING: Add the jacket.")
    check("edit: the brain's MISSING comes back as a sentence", b.get("missing") == "Add the jacket.", b)
    b, _ = skill({"room": "picture", "mode": "edit", "topic": "lamp from picture 2 to the desk", "attached": [UP, UP2]},
                 "PROMPT: Put the lamp from picture 2 on the desk in <image1>.", "PROMPT: Put the lamp from <image2> on the desk in <image1>.")
    check("edit: a bare 'picture 2' gets one retry", b.get("retried") and "<image2>" in b["fields"]["prompt"], b)
    check("edit: attached must be a list", skill({"room": "picture", "mode": "edit", "topic": "x", "attached": "a"})[1] == 400)
    check("t2i: attached is refused", skill({"room": "picture", "mode": "t2i", "topic": "x", "attached": []})[1] == 400)

    print("3D source picture")
    b, code = skill({"room": "3d", "mode": "mesh", "topic": "a brass lantern"}, "NEGATIVE: hard shadows\nPROMPT: A brass lantern.",
                    "NEGATIVE: hard shadows, reflections\nNOTE: the handle is thin.\nPROMPT: " + SRC)
    check("3D: the draft fills t2i's fields after one retry", code == 200 and b.get("retried")
          and b.get("fields") == {"negative": "hard shadows, reflections", "prompt": SRC} and "three-quarter" in user(1), b)
    check("3D: it names where it goes", b.get("target") == {"cap": "image", "mode": "t2i", "room": "picture",
          "room_name": "Picture", "mode_label": engines.mode_words("image")["t2i"]}, b.get("target"))
    check("3D: the pack's own prompt", REQ[0]["messages"][0]["content"] == mesh3d.SOURCE_PICTURE_PROMPT)
finally:
    LANE.terminate()

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
