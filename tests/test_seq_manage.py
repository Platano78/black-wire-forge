"""Acceptance gate for C1 -- sequence names, list rows, rename, delete
(findings 1 + 8).

ISOLATION: every byte this test writes goes under a fresh tempfile.mkdtemp()
directory. GENCENTER_DATA and GENCENTER_CONFIG are set BEFORE server.py is
imported or launched, so the module uses a scratch data dir and a scratch
config (one lane on a closed local port).

    python3 tests/test_seq_manage.py             the whole gate
"""
import hashlib, importlib.util, json, os, shutil, socket, sys, tempfile, time
import urllib.error, urllib.request
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port

SCRATCH = tempfile.mkdtemp(prefix="bwf_seq_manage_")
DATA_A = os.path.join(SCRATCH, "data")
os.makedirs(DATA_A)
CONFIG = os.path.join(SCRATCH, "config.json")
PORT = free_port()
json.dump({"port": PORT, "bind": "127.0.0.1", "title": "c1 test",
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1",
                      "port": free_port(), "caps": ["image", "video", "audio"]}]},
          open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = DATA_A

import importlib
srv = importlib.import_module("server")


def op(sid, name, **kw):
    """GET the seq for its rev, then run the op."""
    d, _ = srv.seq_get(sid)
    rev = d.get("rev")
    return srv.seq_op(dict(kw, id=sid, rev=rev, op=name))

def row(sid):
    """The row of srv.seq_list()[0] with that id, or None."""
    lst = srv.seq_list()[0]
    for r in lst:
        if r.get("id") == sid:
            return r
    return None

# -----------------------------------------------------------------------
print("auto titles, first-beat naming, set_title immunity")

try:
    # 1. blank title
    body, code = srv.seq_create({"title": "", "mode": "sequence"})
    title = body.get("title", "")
    check("blank title: created (200)", code == 200)
    check("blank title: not 'Untitled sequence'", title != "Untitled sequence")
    check("blank title: names the year it was made", time.strftime("%Y") in title)
    check("blank title: marked as a name the app chose", body.get("title_auto") is True)
    sid1 = body["id"]

    # 2. no title key
    body, code = srv.seq_create({"mode": "sequence"})
    title = body.get("title", "")
    check("no title key at all: same date name", time.strftime("%Y") in title)
    sid2 = body["id"]

    # 3. insert_beat auto-titles
    beat_text = ("A paper airplane leaves a fourth-floor window and catches "
                 "the wind over the rooftops at dawn.\nIt climbs.")
    b, c = op(sid1, "insert_beat", text=beat_text, kind="film", slot_id=None)
    check("insert_beat ok", c == 200)
    check("first beat names it: its opening words",
          b.get("title", "").startswith("A paper airplane leaves"))
    check("first beat title: one line, no newline", "\n" not in b.get("title", ""))
    check("first beat title: short (<= 60 chars)", len(b.get("title", "")) <= 60)
    check("still auto after the beat", b.get("title_auto") is True)
    rev1 = body["rev"]

    # 4. edit first beat renames auto title
    b, c = op(sid1, "update_beat", beat_id="b1", text="The plane dives past a pigeon.")
    check("editing the first beat renames an auto title",
          b.get("title", "").startswith("The plane dives past"))

    # 5. set_title names it, then beat edit no longer renames
    b, c = op(sid1, "set_title", title="Paper plane")
    check("set_title: the user's name", b.get("title") == "Paper plane")
    check("set_title: no longer auto", "title_auto" not in b)
    b, c = op(sid1, "update_beat", beat_id="b1", text="The plane lands on a tram roof.")
    check("after set_title a beat edit never renames", b.get("title") == "Paper plane")
    rev1 = b["rev"]

    # 6. named sequence is never renamed
    body, code = srv.seq_create({"title": "My film", "mode": "storyboard"})
    sid6 = body["id"]
    b, c = op(sid6, "insert_beat", text="Rain on a window.", kind="film", slot_id=None)
    check("a named sequence is never renamed by its first beat", b.get("title") == "My film")
    check("a named sequence is not auto", "title_auto" not in b)

except Exception:
    FAILED.append("the suite crashed")
    import traceback; traceback.print_exc()

# -----------------------------------------------------------------------
print("list rows: first_beat, created, shots, thumb")

try:
    r1 = row(sid1)
    check("row: first_beat is the first beat's text",
          r1.get("first_beat") == "The plane lands on a tram roof.")
    check("row: created time", isinstance(r1.get("created"), (int, float)) and r1["created"] > 0)
    # sid1 has one film beat that makes one video slot
    check("row: shots counts the video lane", r1.get("shots") == 1)
    check("row: no thumb before anything is picked", r1.get("thumb") is None)

    # sid2 has no beats
    r2 = row(sid2)
    check("row with no beats: first_beat is None", r2.get("first_beat") is None)

except Exception:
    FAILED.append("the suite crashed")
    import traceback; traceback.print_exc()

# -----------------------------------------------------------------------
print("thumb from a picked take")

try:
    now = time.time()
    with srv.JOBS_LOCK:
        srv.JOBS["thumbjob1"] = {
            "id": "thumbjob1", "lane": "t", "kind": "video", "status": "done",
            "created": now, "started": now, "prompt": "test", "outputs": [
                {"filename": "a.mp4", "subfolder": "", "type": "output", "media": "video"}]}
        if "thumbjob1" not in srv.JOB_ORDER:
            srv.JOB_ORDER.append("thumbjob1")
    with srv.SEQ_LOCK:
        seq = srv._seq_read(sid1)
        video_slot = [s for s in seq["slots"] if s.get("lane") == "video"][0]
        video_slot["takes"].append({
            "job_id": "thumbjob1", "made": now, "beat_rev": None,
            "inputs": {"refs": [], "cables": {}},
            "file": os.path.join("takes", "thumbjob1.mp4")})
        video_slot["pick"] = "thumbjob1"
        srv._seq_write(seq)
    r1 = row(sid1)
    check("row: thumb names the picked take's local file",
          r1.get("thumb") == {"path": "takes/thumbjob1.mp4", "media": "video"})

except Exception:
    FAILED.append("the suite crashed")
    import traceback; traceback.print_exc()

# -----------------------------------------------------------------------
print("seq_delete")

try:
    seq = srv._seq_read(sid2)
    sid_del = seq["id"]
    path = os.path.join(srv.SEQ_DIR, sid_del + ".json")
    check("(setup) the file exists", os.path.exists(path))

    d, code = srv.seq_delete({"id": sid_del})
    check("delete: 200 ok", code == 200 and d.get("ok") is True)
    check("delete: the file left sequences/", not os.path.exists(path))
    check("delete: kept under sequences/deleted/",
          os.path.isfile(os.path.join(srv.SEQ_DIR, "deleted", sid_del + ".json")))
    check("delete: gone from the list", row(sid_del) is None)
    check("delete: GET says no such sequence", srv.seq_get(sid_del)[1] == 404)

    d, code = srv.seq_delete({"id": sid_del})
    check("delete twice: 404 with a sentence",
          code == 404 and "no such sequence" in d.get("error", ""))

    d, code = srv.seq_delete({"id": "../../etc"})
    check("delete a bad id: 400", code == 400)

    d, code = srv.seq_delete("nope")
    check("delete a non-object: 400", code == 400)

except Exception:
    FAILED.append("the suite crashed")
    import traceback; traceback.print_exc()

# -----------------------------------------------------------------------
shutil.rmtree(SCRATCH, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
