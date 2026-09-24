"""Acceptance gate for C3.1 -- the sequence store and the save_jobs race fix
(the internal sequence/storyboard design spec, §8 row C3.1).

ISOLATION: every byte this test writes goes under a fresh tempfile.mkdtemp()
directory. GENCENTER_DATA and GENCENTER_CONFIG are set BEFORE server.py is
imported or launched, so the module and the real server processes both use a
scratch data dir and a scratch config (one lane on a closed local port, so
nothing ever reaches a real machine). The real data/jobs.json is hashed
before and after, and the test fails if it changed.

    python3 tests/test_sequences.py              the whole gate
    BWF_SERVER=/path/to/other/server.py python3 tests/test_sequences.py --race-only
                                                 only the save_jobs race, against
                                                 another server.py (e.g. the pre-C3.1
                                                 one, to watch this check go RED)
"""
import hashlib, importlib.util, json, os, random, shutil, socket, subprocess, sys
import tempfile, threading, time, types, urllib.error, urllib.request
import http.client as http_client
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

def sha(path):
    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()
    except FileNotFoundError:
        return None

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port

REAL_JOBS = os.path.join(ROOT, "data", "jobs.json")
REAL_JOBS_SHA = sha(REAL_JOBS)
SCRATCH = tempfile.mkdtemp(prefix="bwf_c31_")
DATA_A = os.path.join(SCRATCH, "data_inproc")     # the imported module's data dir
DATA_B = os.path.join(SCRATCH, "data_process")    # the launched server's data dir
RACE_DIR = os.path.join(SCRATCH, "race")
for d in (DATA_A, DATA_B, RACE_DIR):
    os.makedirs(d)
CONFIG = os.path.join(SCRATCH, "config.json")
PORT = free_port()
json.dump({"port": PORT, "bind": "127.0.0.1", "title": "c31 test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 0.5},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": free_port(),
                      "caps": ["image", "video"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = DATA_A
print("scratch dir: %s (real data/ is never written)" % SCRATCH)

SERVER = os.environ.get("BWF_SERVER") or os.path.join(ROOT, "server.py")
spec = importlib.util.spec_from_file_location("srv_c31", SERVER)
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)


# ---------------------------------------------------------------------------
print("save_jobs survives concurrent writers while the poller mutates jobs (%s)" % SERVER)
# The poller and request threads call save_jobs() at any moment, while the
# websocket/poller threads add keys to live job dicts under JOBS_LOCK. This
# runs exactly that, flat out, and counts what the old code swallowed.

def race(seconds=3.0):
    srv.JOBS_FILE = os.path.join(RACE_DIR, "jobs.json")
    if hasattr(srv, "SEQ_DIR"):
        srv.SEQ_DIR = os.path.join(RACE_DIR, "sequences")
    with srv.JOBS_LOCK:
        srv.JOBS.clear(); srv.JOB_ORDER.clear()
        for i in range(300):
            jid = "race%05d" % i
            srv.JOBS[jid] = dict({"id": jid, "status": "running", "lane": "t", "step": 0},
                                 **{"k%02d" % k: "x" * 30 for k in range(40)})
            srv.JOB_ORDER.append(jid)
    swallowed, torn, stop = [], [], threading.Event()
    real_tb = srv.traceback
    srv.traceback = types.SimpleNamespace(print_exc=lambda *a, **k: swallowed.append(repr(sys.exc_info()[1])))
    ids = list(srv.JOBS)

    def mutate():                          # what handle_ws_message / job_poller do
        n = 0
        while not stop.is_set():
            jid = random.choice(ids)
            with srv.JOBS_LOCK:
                j = srv.JOBS[jid]
                j["step"] = n
                if "node" in j:
                    del j["node"]
                else:
                    j["node"] = str(n)
            n += 1

    def save():
        while not stop.is_set():
            srv.save_jobs()

    def read():
        while not stop.is_set():
            try:
                with open(srv.JOBS_FILE) as f:
                    json.load(f)
            except FileNotFoundError:
                pass
            except ValueError as e:
                torn.append(repr(e))

    old_si = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)
    threads = [threading.Thread(target=t) for t in [mutate, mutate, read] + [save] * 6]
    for t in threads: t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads: t.join()
    sys.setswitchinterval(old_si)
    srv.traceback = real_tb
    srv.save_jobs()                        # one quiet save at the end
    final = json.load(open(srv.JOBS_FILE))
    leftovers = sorted(n for n in os.listdir(RACE_DIR) if n.endswith(".tmp"))
    return swallowed, torn, final, leftovers

swallowed, torn, final, leftovers = race()
check("no save was silently lost to an exception (%d swallowed)" % len(swallowed), not swallowed,
      sorted(set(s[:90] for s in swallowed))[:4])
check("a reader never saw a torn jobs.json (%d torn reads)" % len(torn), not torn, torn[:2])
check("no tmp file left behind", not leftovers, leftovers)
check("the final file parses and keeps the newest 200", len(final) == 200 and final[-1]["id"] == "race00299",
      len(final))
with srv.JOBS_LOCK:
    srv.JOBS.clear(); srv.JOB_ORDER.clear()

if "--race-only" in sys.argv:
    shutil.rmtree(SCRATCH, ignore_errors=True)
    print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
    sys.exit(1 if FAILED else 0)

srv.JOBS_FILE = os.path.join(DATA_A, "jobs.json")
srv.SEQ_DIR = os.path.join(DATA_A, "sequences")
os.makedirs(srv.SEQ_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
print("in-process: ids, ops, validation, derived state")

def op(sid, rev, name, **kw):
    return srv.seq_op(dict(kw, id=sid, rev=rev, op=name))

bad_ids = ["../x", "s_1234567g", "S_12345678", "s_12345678\n", "s_123456789", "", 123, None, ["s_12345678"]]
check("GET /api/sequence refuses every bad id with 400",
      all(srv.seq_get(b)[1] == 400 for b in bad_ids if isinstance(b, str)), bad_ids)
check("POST /api/sequence/op refuses every bad id with 400",
      all(op(b, 1, "set_title", title="x")[1] == 400 for b in bad_ids))
body, code = srv.seq_create({"title": "Till Delete", "mode": "sequence"})
sid, rev = body["id"], body["rev"]
check("create: schema 1, rev 1, empty lists, canvas from the pack", code == 200 and body["schema"] == 1 and rev == 1
      and body["refs"] == body["beats"] == body["slots"] == body["cables"] == body["cuts"] == []
      and isinstance(body["canvas"]["width"], int), body)
check("create: nothing derived is stored", "state" not in open(os.path.join(srv.SEQ_DIR, sid + ".json")).read())
# C3.5 made every op §7 names real, so the generic "not available yet"
# refusal (which these two checks used to assert, back when insert_beat and
# import_script were stubs) no longer exists in server.py. Same SHAPE of check
# -- a bad op request is a 400 carrying a sentence, never a stack trace --
# retargeted at what the real op now refuses on. (C3.2a did exactly this when
# add_ref stopped being a stub; see git log for ab2cefb.)
b, c = op(sid, rev, "insert_beat")
check("a beat op with no text is a 400 sentence", c == 400 and isinstance(b.get("error"), str)
      and b["error"] and "not available yet" not in b["error"], b)
b, c = op(sid, rev, "explode")
check("an unknown op is a 400 sentence", c == 400 and "no sequence operation" in b["error"], b)
b, c = op(sid, rev, "add_slot", lane="video", cap="video", mode="ref2v", values={"prompt": "tea", "nonsense": 1})
check("unknown values key refused, naming it", c == 400 and "nonsense" in b["error"], b)
b, c = op(sid, rev, "add_slot", lane="video", cap="video", mode="no-such-mode")
check("unknown mode refused", c == 400, b)
b, c = op(sid, rev, "add_slot", lane="video", cap="video", mode="ref2v", values={"prompt": "tea"})
check("add_slot ok, rev bumped, id v1, state empty", c == 200 and b["rev"] == rev + 1
      and b["slots"][0]["id"] == "v1" and b["slots"][0]["state"] == "empty", b)
rev = b["rev"]
b, c = op(sid, rev - 1, "set_title", title="stale")
check("a stale rev gets 409 with the current object", c == 409 and b["sequence"]["rev"] == rev, b)
b, c = op(sid, rev, "update_slot", slot_id="v1", mode="fl2va")
check("update_slot keeps values that the new mode also declares", c == 200 and b["slots"][0]["values"] == {"prompt": "tea"}, b)
rev = b["rev"]
b, c = op(sid, rev, "set_canvas", width=864, height=480)
check("set_canvas allowed before any video take", c == 200 and b["canvas"] == {"width": 864, "height": 480}, b)
rev = b["rev"]

# A take on the video slot, recorded the way C3.2's generate will.
with srv.JOBS_LOCK:
    srv.JOBS["take0001"] = {"id": "take0001", "lane": "t", "kind": "video", "status": "running",
                            "step": 0, "total": 0, "outputs": []}
    srv.JOB_ORDER.append("take0001")
b = srv.seq_add_take(sid, "v1", "take0001", beat_rev=1)
rev = b["rev"]
b, c = op(sid, rev, "set_canvas", width=960, height=544)
check("set_canvas refused once a video take exists (sentence, 400)", c == 400 and "fixed" in b["error"], b)

def state():
    return srv.seq_get(sid)[0]["slots"][0]

with srv.STATE_LOCK:
    srv.LANE_STATE["t"]["up"] = False
check("running take on a DOWN lane is cannot-tell, never rendering", state()["state"] == "cannot-tell", state())
with srv.STATE_LOCK:
    srv.LANE_STATE["t"]["up"] = True
check("running take on an up lane, total 0 -> rendering '--'", (state()["state"], state()["progress"]) == ("rendering", "--"), state())
with srv.JOBS_LOCK:
    srv.JOBS["take0001"].update(step=3, total=20)
check("rendering shows step/total", state()["progress"] == "3/20", state())
with srv.JOBS_LOCK:
    srv.JOBS["take0001"]["status"] = "error"
check("error -> failed", state()["state"] == "failed", state())
with srv.JOBS_LOCK:
    srv.JOBS["take0001"]["status"] = "interrupted"
check("interrupted -> lost", state()["state"] == "lost", state())
b, c = op(sid, rev, "pick_take", slot_id="v1", job_id="take0001")
check("an unfinished take cannot be picked", c == 400, b)
with srv.JOBS_LOCK:
    srv.JOBS["take0001"]["status"] = "done"
check("done with no pick -> unpicked", state()["state"] == "unpicked", state())
b, c = op(sid, rev, "pick_take", slot_id="v1", job_id="take0001")
rev = b["rev"]
check("done and picked -> ready, not stale", c == 200 and state()["state"] == "ready" and state()["stale"] == [], state())

# Beats arrive in C3.5; plant one directly to prove stale-by-beat_rev is derived.
path = os.path.join(srv.SEQ_DIR, sid + ".json")
raw = json.load(open(path))
raw["beats"] = [{"id": "b1", "kind": "film", "text": "tea", "rev": 2, "slot_id": "v1"}]
raw["slots"][0]["beat_id"] = "b1"
json.dump(raw, open(path, "w"))
check("pick's beat_rev != beat.rev -> stale 'script changed'", state()["stale"] == ["script changed"], state())
check("stale is never written to the file", "stale" not in open(path).read())

for name, kw in [("set_trim", {"trim": {"in": 1.5, "len": 3}}), ("set_trim", {"trim": None}),
                 ("set_title_card", {"title": {"text": "TILL DELETE", "at": 0, "dur": 2}}),
                 ("set_mode", {"mode": "storyboard"}), ("set_title", {"title": "Till Delete II"})]:
    b, c = op(sid, rev, name, slot_id="v1", **kw)
    check("%s %s" % (name, kw), c == 200 and b["rev"] == rev + 1, b)
    rev = b["rev"]
b, c = op(sid, rev, "set_trim", slot_id="v1", trim={"in": -1, "len": 3})
check("a negative trim is refused", c == 400, b)
b, c = op(sid, rev, "add_slot", lane="picture", cap="image", mode="t2i", values={}, at=0)
rev = b["rev"]
b, c = op(sid, rev, "move_slot", slot_id="p1", to=1)
check("move_slot reorders", c == 200 and [s["id"] for s in b["slots"]] == ["v1", "p1"], b)
rev = b["rev"]

# forget: the sentence form names the shot. Driven through the real handler method.
def forget(jid):
    h = srv.Handler.__new__(srv.Handler)
    out = []
    h.read_json = lambda: {"job_id": jid}
    h.send_json = lambda payload, code=200: out.append((payload, code))
    srv.Handler.api_forget(h)
    return out[-1]
b, c = forget("take0001")
check("forget a job used as a take -> 409 'Till Delete II uses this as shot 1. Remove it there first.'",
      c == 409 and b["error"] == "Till Delete II uses this as shot 1. Remove it there first.", b)
b, c = op(sid, rev, "remove_slot", slot_id="v1")
rev = b["rev"]
b, c = forget("take0001")
check("after the shot is removed, forget works", c == 200, b)


# ---------------------------------------------------------------------------
print("real server process: restart, pinning, forget, 50 x 20 concurrent ops")
BASE = "http://127.0.0.1:%d" % PORT

RESETS = []
def http(method, path, body=None):
    """50 clients at once overflow ThreadingHTTPServer's listen backlog (5), and
    the kernel resets the surplus BEFORE accept -- the request never reached a
    handler, so it is retried. If a reset ever came after an op was applied,
    the retry would apply it twice and the rev arithmetic below would catch it."""
    data = None if body is None else json.dumps(body).encode()
    for attempt in range(50):
        req = urllib.request.Request(BASE + path, method=method, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()), r.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read()), e.code
        except (ConnectionResetError, ConnectionRefusedError, http_client.RemoteDisconnected) as e:
            RESETS.append(repr(e))
            time.sleep(0.01 * (attempt + 1))
    raise RuntimeError("gave up after 50 connection resets")

PROC = [None]
def start():
    env = dict(os.environ, GENCENTER_DATA=DATA_B, GENCENTER_CONFIG=CONFIG)
    PROC[0] = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if http("GET", "/api/health")[1] == 200:
                return PROC[0].pid
        except Exception:
            time.sleep(0.1)
    raise SystemExit("server did not come up")

def stop():
    PROC[0].terminate()            # this Popen's own PID only -- never pkill
    PROC[0].wait(timeout=10)

# 250 finished fake pictures, oldest first. #3 (index 2) is far outside the newest 200.
now = time.time()
fake = [{"id": "fake%05d" % i, "lane": "t", "kind": "image", "mode": "t2i", "status": "done",
         "created": now - 1000 + i, "started": now - 1000 + i, "prompt": "p%d" % i,
         "outputs": [{"filename": "f%d.png" % i, "subfolder": "", "type": "output", "media": "image"}]}
        for i in range(250)]
json.dump(fake, open(os.path.join(DATA_B, "jobs.json"), "w"))
JOB3 = fake[2]["id"]

try:
    pid1 = start()
    print("     server pid %d" % pid1)
    seq, c = http("POST", "/api/sequence", {"title": "Till Delete", "mode": "sequence", "seed_job_id": JOB3})
    check("create with a picture seed -> the set ref, by job id, file null",
          c == 200 and seq["refs"] == [{"id": "r1", "role": "set", "label": "Set plate", "job_id": JOB3,
                                        "output": 0, "file": None, "recipe": None}], seq)
    SID = seq["id"]
    b, c = http("POST", "/api/sequence", {"title": "No seed", "seed_job_id": "nosuchjob"})
    check("a seed that is not a job just starts with no set plate", c == 200 and b["refs"] == [], b)
    b, c = http("POST", "/api/sequence/op", {"id": SID, "rev": seq["rev"], "op": "add_slot", "lane": "video",
                                             "cap": "video", "mode": "ref2v", "values": {"prompt": "start"}})
    check("add_slot over HTTP", c == 200, b)
    for bad in ["../x", "..%2Fx", "s_1234567G"]:
        check("GET /api/sequence?id=%s -> 400" % bad, http("GET", "/api/sequence?id=" + bad)[1] == 400)
    check("POST /api/sequence/op id ../x -> 400",
          http("POST", "/api/sequence/op", {"id": "../x", "rev": 1, "op": "set_title", "title": "x"})[1] == 400)
    b, c = http("POST", "/api/sequence/op", {"id": SID, "rev": 2, "op": "import_script"})
    check("a script op over HTTP with nothing to import -> 400 sentence", c == 400
          and isinstance(b.get("error"), str) and b["error"], b)

    before, _ = http("GET", "/api/sequence?id=" + SID)
    lst, _ = http("GET", "/api/sequences")
    check("GET /api/sequences lists it with slots/ready",
          any(s["id"] == SID and s["slots"] == 1 and s["ready"] == 0 for s in lst), lst)
    stop()
    saved = [j["id"] for j in json.load(open(os.path.join(DATA_B, "jobs.json")))]
    check("jobs.json after the first run: newest 200 + pinned #3 (201)", len(saved) == 201 and JOB3 in saved, len(saved))

    pid2 = start()
    print("     restarted, server pid %d" % pid2)
    after, _ = http("GET", "/api/sequence?id=" + SID)
    check("after a real process restart the object is identical", after == before and pid2 != pid1)
    b, c = http("POST", "/api/forget", {"job_id": JOB3})
    check("forget a referenced job -> 409 with the sentence",
          c == 409 and b["error"] == "Till Delete uses this in its reference room. Remove it there first.", b)
    b, c = http("POST", "/api/forget", {"job_id": fake[249]["id"]})
    check("forget an unreferenced job still works", c == 200, b)
    stop()
    pid3 = start()
    print("     restarted, server pid %d" % pid3)
    jobs, _ = http("GET", "/api/jobs?limit=1000")
    ids = [j["id"] for j in jobs["jobs"]]
    check("after 250 jobs, a forget and two restarts, #3 is still present",
          JOB3 in ids and fake[249]["id"] not in ids, len(ids))

    # 50 threads x 20 update_slot, each with the rev it last saw, retrying on 409.
    start_rev = http("GET", "/api/sequence?id=" + SID)[0]["rev"]
    accepted, errors, lock = [], [], threading.Lock()
    def worker(t):
        try:
            work(t)
        except Exception as e:           # a dead thread must fail the gate, not vanish
            with lock: errors.append(("exception", repr(e)))
    def work(t):
        cur = start_rev
        for i in range(20):
            text = "t%02d-%02d" % (t, i)
            while True:
                b, c = http("POST", "/api/sequence/op", {"id": SID, "rev": cur, "op": "update_slot",
                                                         "slot_id": "v1", "values": {"prompt": text}})
                if c == 409:
                    cur = b["sequence"]["rev"]
                    continue
                if c != 200:
                    with lock: errors.append((c, b))
                    break
                with lock: accepted.append((b["rev"], text))
                cur = b["rev"]
                break
    t0 = time.time()
    threads = [threading.Thread(target=worker, args=(t,)) for t in range(50)]
    for t in threads: t.start()
    for t in threads: t.join()
    print("     %d accepted ops in %.1fs (%d connection resets retried)" % (len(accepted), time.time() - t0, len(RESETS)))
    final_obj, _ = http("GET", "/api/sequence?id=" + SID)
finally:
    stop()
on_disk = json.load(open(os.path.join(DATA_B, "sequences", SID + ".json")))
revs = sorted(r for r, _ in accepted)
check("no op errored", not errors, errors[:3])
check("final rev = start + 1000 (%d -> %d)" % (start_rev, on_disk["rev"]),
      on_disk["rev"] == start_rev + 1000 == final_obj["rev"])
check("every accepted op got its own rev (no two writes on one base: no lost write)",
      revs == list(range(start_rev + 1, start_rev + 1001)))
check("the file holds the last accepted write", on_disk["slots"][0]["values"]["prompt"] == max(accepted)[1])
check("no tmp file left in sequences/",
      not [n for n in os.listdir(os.path.join(DATA_B, "sequences")) if n.endswith(".tmp")])

check("the real data/jobs.json was not touched", sha(REAL_JOBS) == REAL_JOBS_SHA)
shutil.rmtree(SCRATCH, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
