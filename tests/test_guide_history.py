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
check("nothing stored yet: an empty history, not an error",
      # UX-2 #8: GET now also carries "generation" (0 for a brand-new key).
      (b, code) == ({"ok": True, "history": [], "generation": 0}, 200), (b, code))
b, code = srv.guide_history_set({"key": VIDEO_KEY, "history": [TURN1, TURN2]})
# UX-2 #8: SET now also echoes "generation" (0: no client-declared generation, no clear yet).
check("saved ok", (b, code) == ({"ok": True, "generation": 0}, 200), (b, code))
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

print()
print("UX-2 #8: a clear wins everywhere -- the two-device case")
CLEAR_KEY = "bwf.guide.hist.video"
# Two devices, A and B, both start from the same synced state.
b, code = srv.guide_history_set({"key": CLEAR_KEY, "history": [TURN1]})
check("setup: device A and B both start from generation 0", (b, code) == ({"ok": True, "generation": 0}, 200), (b, code))
a_get, _ = srv.guide_history_get(CLEAR_KEY)
b_get, _ = srv.guide_history_get(CLEAR_KEY)
check("both devices fetched generation 0", a_get["generation"] == 0 and b_get["generation"] == 0)
# Device A clears the conversation.
clear_resp, clear_code = srv.guide_history_clear({"key": CLEAR_KEY})
check("A's clear succeeds and bumps the generation", (clear_resp, clear_code) == ({"ok": True, "generation": 1}, 200),
      (clear_resp, clear_code))
after_clear, _ = srv.guide_history_get(CLEAR_KEY)
check("the server's history is empty right after the clear",
      after_clear == {"ok": True, "history": [], "generation": 1}, after_clear)
# Device B, offline during the clear, still has a LONGER local copy built
# from generation 0 and tries to save it -- this must NOT resurrect the
# cleared conversation.
b_stale_hist = [TURN1, TURN2, {"role": "user", "content": "one more from B, before it synced the clear"}]
save_resp, save_code = srv.guide_history_set({"key": CLEAR_KEY, "history": b_stale_hist, "generation": 0})
check("B's stale save (generation 0, now behind) is REFUSED (409), not silently overwritten",
      save_code == 409 and save_resp["ok"] is False, (save_resp, save_code))
check("the refusal hands B the CURRENT (cleared) state to adopt",
      save_resp.get("history") == [] and save_resp.get("generation") == 1, save_resp)
still_after, _ = srv.guide_history_get(CLEAR_KEY)
check("the clear is UNDISTURBED: B's longer copy never reached the stored file",
      still_after == {"ok": True, "history": [], "generation": 1}, still_after)
# B adopts the clear (generation 1) and saves fresh turns from there -- this must succeed.
b_fresh, _ = srv.guide_history_set({"key": CLEAR_KEY, "history": [TURN1], "generation": 1})
check("B saving AFTER adopting the new generation succeeds normally", b_fresh == {"ok": True, "generation": 1}, b_fresh)
# A client that never sends "generation" at all (older code, or a caller
# that doesn't care) keeps working exactly as before -- no CAS enforced.
no_gen_resp, no_gen_code = srv.guide_history_set({"key": CLEAR_KEY, "history": b_stale_hist})
check("a save with NO \"generation\" field is back-compat: always accepted, same as pre-#8 behaviour",
      no_gen_code == 200, (no_gen_resp, no_gen_code))

print()
print("UX-2 #8: /api/guide/history/clear key-sanitising, same refusal shape as GET/POST above")
for bad in ("bwf.guide.hist." + "../../../etc/passwd", "bwf.guide.hist.notaroom", "", None, 123):
    b, code = srv.guide_history_clear({"key": bad})
    check("clear refuses %r" % (bad,), code == 400 and b["ok"] is False, b)
check("a malformed clear body (not an object) is refused, not a 500", srv.guide_history_clear("not an object")[1] == 400)

print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
