"""Two copies of the same take at once must both succeed.

_cut_ensure_take_file copies a take's bytes into data/seq/<id>/takes/. It runs
in the background (the Sing along song-length probe) AND from a Make
(resolve_slot_sing), so two callers can copy the same take at the same moment.
With one shared "<dest>.tmp" the first os.replace moved the file away and the
second raised FileNotFoundError out of seq_generate (seen in a full suite run).

This suite forces that interleaving: both callers have written their temp file
before either renames it. Both must return the destination path, the file must
hold the take's bytes, and no temp file may be left behind.
Run: python3 tests/test_take_file_race.py
"""
import importlib.util, json, os, shutil, socket, sys, tempfile, threading
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond or detail == "" else "  " + str(detail)))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port

SCRATCH = tempfile.mkdtemp(prefix="bwf_take_race_")
try:
    CONFIG = os.path.join(SCRATCH, "config.json")
    json.dump({"port": free_port(), "bind": "127.0.0.1",
               "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 1.0},
               "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": free_port(),
                          "caps": ["audio"]}]}, open(CONFIG, "w"))
    os.environ["GENCENTER_CONFIG"] = CONFIG
    DATA = os.path.join(SCRATCH, "data")
    os.environ["GENCENTER_DATA"] = DATA
    spec = importlib.util.spec_from_file_location("srv_take_race", os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SEQ_DIR = os.path.join(DATA, "sequences")
    mod.SEQ_MEDIA_DIR = os.path.join(DATA, "seq")

    SID, SLOT, JOB = "s_0a1b2c3d", "song_s_0a1b2c3d", "job_race_1"
    BYTES = b"fLaC" + os.urandom(4096)
    with mod.JOBS_LOCK:
        mod.JOBS[JOB] = {"id": JOB, "lane": "t", "status": "done", "outputs": [{"filename": "song.flac"}]}
    mod._carry_source_bytes = lambda job, out, cache_path=None: BYTES

    # Hold the first rename until the second caller has also written its temp
    # file and reached its own rename: the order that lost the file before.
    real_replace = os.replace
    arrived, first_done = threading.Event(), threading.Event()
    lock, calls = threading.Lock(), []
    def gated_replace(src, dst, *a, **k):
        with lock:
            calls.append(src)
            n = len(calls)
        if n == 1:
            arrived.wait(5)
            real_replace(src, dst, *a, **k)
            first_done.set()
        elif n == 2:
            arrived.set()
            first_done.wait(5)
            real_replace(src, dst, *a, **k)
        else:
            real_replace(src, dst, *a, **k)
    mod.os.replace = gated_replace

    results = [None, None]
    def worker(i):
        try:
            results[i] = ("ok", mod._cut_ensure_take_file(SID, SLOT, JOB))
        except Exception as e:
            results[i] = ("error", "%s: %s" % (type(e).__name__, e))
    ts = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(15)
    mod.os.replace = real_replace

    print("two callers copy the same take at once")
    check("both calls return the take's path", all(r and r[0] == "ok" for r in results), results)
    dest = os.path.join(mod.SEQ_MEDIA_DIR, SID, "takes", JOB + ".flac")
    check("the take's file holds its bytes", os.path.isfile(dest) and open(dest, "rb").read() == BYTES)
    left = [f for f in os.listdir(os.path.dirname(dest)) if f != JOB + ".flac"] if os.path.isdir(os.path.dirname(dest)) else []
    check("no temp file is left behind", left == [], left)
finally:
    shutil.rmtree(SCRATCH, ignore_errors=True)

print("\nFAILED: %d%s" % (len(FAILED), "".join("\n  - " + n for n in FAILED)))
sys.exit(1 if FAILED else 0)
