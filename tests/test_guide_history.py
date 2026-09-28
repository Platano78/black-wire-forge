"""Gate for B2: guide history on the server, data/guide_history/<key>.json --
round-trip (including a "done" action marker), key sanitising (a path
traversal key is refused; only a real room, plus a real Cutting Room
sequence, is ever accepted), the turn/byte caps (oldest dropped first),
history shape validation, and per-key isolation. In process, no browser.

Run: python3 tests/test_guide_history.py
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
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


SCRATCH = tempfile.mkdtemp(prefix="bwf_guide_history_")
cfg = {"port": 1, "bind": "127.0.0.1",
       "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": 1, "caps": ["video"]}]}
with open(os.path.join(SCRATCH, "config.json"), "w") as f:
    json.dump(cfg, f)
os.environ["GENCENTER_CONFIG"] = os.path.join(SCRATCH, "config.json")
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")
spec = importlib.util.spec_from_file_location("srv_guide_history", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

VIDEO_KEY = "bwf.guide.hist.video"
TURN1 = {"role": "user", "content": "make it night"}
TURN2 = {"role": "assistant", "content": "PROMPT: ...", "w": "abc123", "done": [0, 1]}

print("round-trip: a plain-room key (no sequence)")
b, code = srv.guide_history_get(VIDEO_KEY)
check("nothing stored yet: an empty history, not an error", (b, code) == ({"ok": True, "history": []}, 200), (b, code))
b, code = srv.guide_history_set({"key": VIDEO_KEY, "history": [TURN1, TURN2]})
check("saved ok", (b, code) == ({"ok": True}, 200), (b, code))
b, code = srv.guide_history_get(VIDEO_KEY)
check("read back exactly, including the done marker on the turn that has one",
      code == 200 and b["history"] == [TURN1, TURN2], b)
check("it really is one small file under data/guide_history/",
      os.path.isfile(os.path.join(srv.GUIDE_HIST_DIR, "video.json")))

print("round-trip: a Cutting Room key (room + sequence)")
seq, _ = srv.seq_create({"title": "Shots", "mode": "storyboard"})
SEQ_KEY = "bwf.guide.hist.cutting." + seq["id"]
b, code = srv.guide_history_set({"key": SEQ_KEY, "history": [TURN1]})
check("saved ok", code == 200, (b, code))
b, code = srv.guide_history_get(SEQ_KEY)
check("read back", code == 200 and b["history"] == [TURN1], b)
check("its own file, named room.sequence",
      os.path.isfile(os.path.join(srv.GUIDE_HIST_DIR, "cutting." + seq["id"] + ".json")))

print("isolation: two different keys never collide")
b, code = srv.guide_history_get(VIDEO_KEY)
check("the plain-room key is unaffected by the sequence-room write", b["history"] == [TURN1, TURN2], b)

print("key sanitising: only a real room (+ a real sequence) is ever accepted -- path traversal refused")
BAD_KEYS = ("bwf.guide.hist." + "../../../etc/passwd", "bwf.guide.hist.video/../../etc/passwd",
           "bwf.guide.hist.", "bwf.guide.hist.notaroom", "bwf.guide.hist.cutting.not_a_real_seq",
           "bwf.guide.hist.video.s_deadbeef",   # a sequence suffix on a non-Cutting room
           "../../etc/passwd", "video", "", None, 123)
for bad in BAD_KEYS:
    b, code = srv.guide_history_get(bad)
    check("GET refuses %r" % (bad,), code == 400 and b["ok"] is False, b)
    b, code = srv.guide_history_set({"key": bad, "history": []})
    check("POST refuses %r" % (bad,), code == 400 and b["ok"] is False, b)
check("no bad key ever escaped data/guide_history/: only the two real files exist",
      sorted(os.listdir(srv.GUIDE_HIST_DIR)) == sorted(["cutting." + seq["id"] + ".json", "video.json"]))

print("shape: history must be a list of {role, content, ...} turns")
for bad_hist in ("not a list", [{"role": "user"}], [{"role": "user", "content": 1}],
                [{"role": "system", "content": "x"}], [1, 2], None):
    b, code = srv.guide_history_set({"key": VIDEO_KEY, "history": bad_hist})
    check("refused: %r" % (bad_hist,), code == 400 and b["ok"] is False, b)
check("a malformed POST body (not an object) is refused, not a 500",
      srv.guide_history_set("not an object")[1] == 400)

print("caps: more than 200 turns keeps the newest 200, oldest dropped first")
many = [{"role": "user", "content": "t%d" % i} for i in range(srv.GUIDE_HIST_MAX_TURNS + 20)]
b, code = srv.guide_history_set({"key": VIDEO_KEY, "history": many})
check("saved ok", code == 200, (b, code))
b, code = srv.guide_history_get(VIDEO_KEY)
h = b.get("history", [])
check("trimmed to the turn cap, the newest kept",
      code == 200 and len(h) == srv.GUIDE_HIST_MAX_TURNS and h[-1]["content"] == "t%d" % (len(many) - 1)
      and h[0]["content"] == many[-srv.GUIDE_HIST_MAX_TURNS]["content"], len(h))

print("caps: an oversized turn beyond the byte cap drops the OLDEST turn(s) first, keeping the newest")
big = {"role": "user", "content": "x" * srv.GUIDE_HIST_MAX_BYTES}
mid = {"role": "user", "content": "mid"}
newest = {"role": "user", "content": "newest"}
b, code = srv.guide_history_set({"key": VIDEO_KEY, "history": [big, mid, newest]})
check("saved ok", code == 200, (b, code))
b, code = srv.guide_history_get(VIDEO_KEY)
h = b.get("history", [])
check("the oversized oldest turn was dropped; the small newer ones kept", code == 200 and h == [mid, newest], h)

print("idempotency: a done marker on an action turn round-trips byte-identical")
DONE_TURN = {"role": "assistant", "content": "ACTION: add_beats | scene one\nACTION: add_beats | scene two",
            "done": [0]}
srv.guide_history_set({"key": VIDEO_KEY, "history": [DONE_TURN]})
b, code = srv.guide_history_get(VIDEO_KEY)
check("the done marker survives a round trip exactly (a stale/cleared marker is what lets an action re-fire)",
      code == 200 and b["history"] == [DONE_TURN], b)

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
