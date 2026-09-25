"""Gate for P3d's server paths after a render: the turntable fixer on POST
/api/guide/revise (settings onto its own fields, a new source picture for the
Picture room, advice only) and POST /api/carry ("Edit this result": a
finished picture onto a lane as an input, results only). In process.

Run: python3 tests/test_picture_revise.py
"""
import os
import sys
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import _scratch_config  # noqa: E402,F401
import _picture_server as ps  # noqa: E402
from _picture_server import FAILED, SRC, check  # noqa: E402

srv, STORE, LANE = ps.start("picture_revise")
REQ = ps.HELPER_STATE["requests"]
TJOB = {"id": "ttjob1", "lane": "cpu", "lane_name": "This machine", "kind": "3d", "mode": "turntable", "status": "done",
        "prompt": "", "args": {"frames": 48, "size": 384, "samples": 16}, "created": time.time(),
        "outputs": [{"filename": "turntable.mp4", "subfolder": "ttjob1", "type": "local", "media": "video"},
                    {"filename": "poster.png", "subfolder": "ttjob1", "type": "local", "media": "image"}]}
JOB = {"id": "picjob1", "lane": "t", "lane_name": "Test lane", "kind": "image", "mode": "t2i", "status": "done",
       "prompt": "p", "created": time.time(),
       "outputs": [{"filename": "result.png", "subfolder": "", "type": "output", "media": "image"},
                   {"filename": "clip.mp4", "subfolder": "", "type": "output", "media": "video"}]}
with srv.JOBS_LOCK:
    srv.JOBS.update({"ttjob1": TJOB, "picjob1": JOB, "runjob1": dict(JOB, id="runjob1", status="running")})


def revise(reply, complaint="the spin is jerky"):
    REQ.clear()
    ps.HELPER_STATE["replies"] = [reply]
    return srv.guide_revise({"room": "3d", "job_id": "ttjob1", "complaint": complaint})


try:
    print("revise: the turntable fixer")
    b, code = revise("DIAGNOSIS: too few frames.\nFIX: settings\nPROMPT:\n"
                     "SETTINGS: frames = 120; Render samples = 64; size = 9000\nNOTE: smoother.")
    user = REQ[0]["messages"][1]["content"]
    check("settings land on the turntable's own fields; a bad one is named and left out",
          code == 200 and b.get("fields") == {"frames": 120, "samples": 64} and len(b.get("problems") or []) == 1
          and b["target"]["mode"] == "turntable" and b["target"]["room"] == "3d", b)
    check("the settings it was made with are sent, and no empty prompt line",
          "The settings it was made with: Frames (one full turn): 48" in user and "The prompt that made it" not in user, user)
    check("no vision: the still is not sent, and the brain is told",
          user.endswith("[The user attached a picture, but this helper cannot see pictures.]"), user[-90:])
    b, code = revise("DIAGNOSIS: the handle is fused.\nFIX: picture\nPROMPT: " + SRC + "\nSETTINGS:")
    check("a picture fix goes to the Picture room's prompt", b.get("fills") == "prompt" and b["prompt"] == SRC
          and b["target"]["room"] == "picture" and b["target"]["mode"] == "t2i", b)
    b, code = revise("DIAGNOSIS: The back is not in this still.\nFIX: none\nPROMPT:", "does the back look right?")
    check("a none fix is advice only", code == 200 and b.get("fix") == "none" and "target" not in b and "fills" not in b, b)
    check("settings with no real field: the shape error", revise("DIAGNOSIS: x\nFIX: settings\nSETTINGS: colour = red")[1] == 502)
    check("the picture fixer's words are not the turntable's", revise("DIAGNOSIS: x\nFIX: reroll\nPROMPT: y")[1] == 502)

    print("carry: Edit this result")
    with srv.STATE_LOCK:
        srv.LANE_STATE.setdefault("t", {})["up"] = True
        srv.LANE_STATE.setdefault("off", {})["up"] = False
    b, code = srv.carry_result({"job_id": "picjob1", "output": 0, "lane": "t"})
    stored = os.path.join(STORE, "inputs", (b.get("file") or {}).get("name") or "none")
    check("the picture goes onto the lane as an input", code == 200 and b["file"]["original"] == "result.png"
          and os.path.isfile(stored), b)
    with open(os.path.join(STORE, "outputs", "result.png"), "rb") as f:
        check("byte for byte", os.path.isfile(stored) and open(stored, "rb").read() == f.read())
    check("the answer carries a name, never bytes", set(b.get("file") or {}) == {"name", "original", "job_id"}, b)
    for label, req, want in (("an unknown job", {"job_id": "nope", "output": 0, "lane": "t"}, 404),
                             ("a job not done", {"job_id": "runjob1", "output": 0, "lane": "t"}, 400),
                             ("an output past the end", {"job_id": "picjob1", "output": 5, "lane": "t"}, 400),
                             ("an output that is a bool", {"job_id": "picjob1", "output": True, "lane": "t"}, 400),
                             ("a clip, not a picture", {"job_id": "picjob1", "output": 1, "lane": "t"}, 400),
                             ("an unknown lane", {"job_id": "picjob1", "output": 0, "lane": "x"}, 400),
                             ("a process lane", {"job_id": "picjob1", "output": 0, "lane": "cpu"}, 400),
                             ("an offline lane", {"job_id": "picjob1", "output": 0, "lane": "off"}, 409),
                             ("not an object", ["picjob1"], 400)):
        b, code = srv.carry_result(req)
        check("refuses %s (%d) with a sentence" % (label, want), code == want and not b["ok"] and b.get("error"), (code, b))
finally:
    LANE.terminate()

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
