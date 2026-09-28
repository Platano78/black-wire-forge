"""Acceptance gate for UX-2 #5: "Remove" also deletes the lane's own output
file, opt-in per lane (config.json "outputs.dir"), off by default. Tests the
REAL functions (lane_output_path, api_forget), never a reimplementation:

1. Off by default: a lane with no "outputs" configured deletes nothing.
2. lane_output_path's containment: a plain relative filename resolves
   under outputs.dir; ".." in the filename or subfolder, an absolute
   filename, and a symlink escaping outputs.dir are all refused.
3. api_forget end to end: Remove deletes the file when the lane opted in,
   never touches disk when it didn't, and still refuses (same as before)
   a job a Cutting Room sequence still uses -- before any deletion happens.

Run: python3 tests/test_output_delete_safety.py
"""
import importlib.util
import json
import os
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402,F401 -- must run before server.py's own exec_module below
spec = importlib.util.spec_from_file_location("srv_output_delete", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

OUT_DIR = tempfile.mkdtemp(prefix="bwf_outputs_")
LANE_OFF = {"id": "off", "name": "Off lane"}
LANE_ON = {"id": "on", "name": "On lane", "outputs": {"dir": OUT_DIR}}

print("lane_output_path: off by default")
check("a lane with no \"outputs\" configured returns None (nothing to delete, nothing refused)",
      srv.lane_output_path(LANE_OFF, {"filename": "x.png", "subfolder": ""}) is None)

print()
print("lane_output_path: containment, against the opted-in lane")
with open(os.path.join(OUT_DIR, "real.png"), "wb") as f:
    f.write(b"fake png bytes")
p = srv.lane_output_path(LANE_ON, {"filename": "real.png", "subfolder": ""})
check("a plain filename with no subfolder resolves under outputs.dir",
      p == os.path.realpath(os.path.join(OUT_DIR, "real.png")), p)
os.makedirs(os.path.join(OUT_DIR, "job123"), exist_ok=True)
with open(os.path.join(OUT_DIR, "job123", "sub.png"), "wb") as f:
    f.write(b"fake")
p2 = srv.lane_output_path(LANE_ON, {"filename": "sub.png", "subfolder": "job123"})
check("filename + subfolder resolves under outputs.dir/subfolder",
      p2 == os.path.realpath(os.path.join(OUT_DIR, "job123", "sub.png")), p2)


def refused(output):
    try:
        srv.lane_output_path(LANE_ON, output)
        return False
    except ValueError:
        return True


check("\"..\" in the filename is neutralised by basename() (lands inside outputs.dir, same "
      "precedent as local_output_path), never a traversal outside it",
      srv.lane_output_path(LANE_ON, {"filename": "../escape.png", "subfolder": ""})
      == os.path.realpath(os.path.join(OUT_DIR, "escape.png")))
check("a filename that's ONLY \"..\" (no basename left to neutralise to) is refused",
      refused({"filename": "..", "subfolder": ""}))
check("an absolute filename is refused (basename()'d, so it can't reach elsewhere)",
      refused({"filename": "/etc/passwd", "subfolder": ""})
      or srv.lane_output_path(LANE_ON, {"filename": "/etc/passwd", "subfolder": ""})
      == os.path.realpath(os.path.join(OUT_DIR, "passwd")))
check("\"..\" in the subfolder is refused", refused({"filename": "x.png", "subfolder": ".."}))
check("a subfolder with a path separator is refused", refused({"filename": "x.png", "subfolder": "a/b"}))
check("an empty filename is refused", refused({"filename": "", "subfolder": ""}))

if hasattr(os, "symlink"):
    escape_target = tempfile.mkdtemp(prefix="bwf_outside_")
    with open(os.path.join(escape_target, "secret.png"), "wb") as f:
        f.write(b"secret")
    link_path = os.path.join(OUT_DIR, "escape_link.png")
    try:
        os.symlink(os.path.join(escape_target, "secret.png"), link_path)
        check("a symlink escaping outputs.dir is refused",
              refused({"filename": "escape_link.png", "subfolder": ""}))
    except OSError as e:
        print("  SKIP  symlink test (no symlink permission in this sandbox): %s" % e)

print()
print("api_forget end to end: deletes the file when the lane opted in, off by default otherwise, "
      "and the sequence-in-use refusal still runs BEFORE any deletion")
srv.LANE_BY_ID[LANE_OFF["id"]] = LANE_OFF
srv.LANE_BY_ID[LANE_ON["id"]] = LANE_ON


class FakeHandler:
    def __init__(self, body):
        self._body = body

    def read_json(self):
        return self._body

    def send_json(self, obj, code=200):
        return (obj, code)


def make_job(jid, lane_id, filename):
    return {"id": jid, "lane": lane_id, "status": "done",
            "outputs": [{"filename": filename, "subfolder": "", "type": "output"}]}


srv.JOBS.clear()
srv.JOB_ORDER.clear()
jid_off = "j-off"
srv.JOBS[jid_off] = make_job(jid_off, "off", "real.png")   # lane not opted in
srv.JOB_ORDER.append(jid_off)
with open(os.path.join(OUT_DIR, "off_no_delete.png"), "wb") as f:
    f.write(b"stays")
srv.JOBS[jid_off]["outputs"][0]["filename"] = "off_no_delete.png"

obj, code = srv.Handler.api_forget(FakeHandler({"job_id": jid_off}))
check("off lane: Remove succeeds", obj.get("ok") is True, obj)
check("off lane: deleted_files is False", obj.get("deleted_files") is False, obj)
check("off lane: the file on disk is UNTOUCHED", os.path.exists(os.path.join(OUT_DIR, "off_no_delete.png")))

jid_on = "j-on"
with open(os.path.join(OUT_DIR, "on_delete_me.png"), "wb") as f:
    f.write(b"goes")
srv.JOBS[jid_on] = make_job(jid_on, "on", "on_delete_me.png")
srv.JOB_ORDER.append(jid_on)
obj2, code2 = srv.Handler.api_forget(FakeHandler({"job_id": jid_on}))
check("on lane: Remove succeeds", obj2.get("ok") is True, obj2)
check("on lane: deleted_files is True", obj2.get("deleted_files") is True, obj2)
check("on lane: the file is actually gone", not os.path.exists(os.path.join(OUT_DIR, "on_delete_me.png")))

jid_used = "j-used"
with open(os.path.join(OUT_DIR, "still_used.png"), "wb") as f:
    f.write(b"kept")
srv.JOBS[jid_used] = make_job(jid_used, "on", "still_used.png")
srv.JOB_ORDER.append(jid_used)
sid = "s_00000001"   # SEQ_ID_RE: "s_" + 8 hex chars, the id is also a filename
with srv.SEQ_LOCK:
    srv._seq_write({"id": sid, "title": "Test sequence",
                     "slots": [{"id": "s1", "lane": "video", "takes": [{"job_id": jid_used}]}]})
obj3, code3 = srv.Handler.api_forget(FakeHandler({"job_id": jid_used}))
check("a job a sequence still uses is REFUSED (409, same as before UX-2 #5)", obj3.get("ok") is False and code3 == 409, (obj3, code3))
check("RED-shaped proof: the file is NOT deleted when the refusal fires (deletion never runs before the check)",
      os.path.exists(os.path.join(OUT_DIR, "still_used.png")))
check("the job record is still there (Remove never popped it either)", jid_used in srv.JOBS)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
