"""Acceptance gate for portability fix P4: when _measure_true_peak (F5)
returns None -- the measuring pass itself failed -- and the cut is not
silent, the alimiter above is still a real backstop but nothing confirmed
it held, so the cut record must say so (`peak_unverified: true`) rather
than reading as an ordinary, checked cut.

Drives _run_cut directly (unit level, real ffmpeg, monkeypatching only
_measure_true_peak) against a tiny real 2s clip -- everything up to the
measurement runs for real.

RED on the pre-fix tree: peak_unverified is simply absent from a "done" cut
whose true-peak measurement failed.

Run: python3 tests/test_cut_peak_unverified.py
"""
import importlib.util, json, os, shutil, socket, subprocess, sys, tempfile, uuid
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# Finding #21: ffmpeg is an optional dep of the app itself -- its absence on
# a clean clone is a SKIP, not a failure.
if not shutil.which("ffmpeg"):
    print("SKIP: ffmpeg not on PATH -- this suite needs it")
    sys.exit(0)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond else ""))
    if not cond: FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

SCRATCH = tempfile.mkdtemp(prefix="bwf_p4_")
CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "p4",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "t", "name": "T", "host": "127.0.0.1", "port": 1, "caps": ["image"]}]},
          open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")

spec = importlib.util.spec_from_file_location("srv_p4", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

# A tiny real clip (2s, 320x240, testsrc video + sine audio) -- has_audio
# True, so loudness_mode won't end up "silent" (which would legitimately
# skip the peak_unverified note).
clip_path = os.path.join(SCRATCH, "clip.mp4")
subprocess.run(["ffmpeg", "-y", "-hide_banner",
                 "-f", "lavfi", "-i", "testsrc=size=320x240:rate=24:duration=2",
                 "-f", "lavfi", "-i", "sine=frequency=1000:duration=2:sample_rate=48000",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", clip_path],
               check=True, capture_output=True)

seq, _ = srv.seq_create({"title": "p4-seq", "mode": "sequence"})
sid = seq["id"]
clip_plans = [{"slot_id": "s1", "path": clip_path, "in": 0.0, "len": 2.0,
               "w": 320, "h": 240, "has_audio": True, "title": None}]
cut_id = "k" + uuid.uuid4().hex[:8]
out_path = os.path.join(srv.SEQ_MEDIA_DIR, sid, "cuts", cut_id + ".mp4")
entry = {"id": cut_id, "status": "queued", "made": 0, "file": None, "log": "",
         "shots": [], "left_out": [], "loudness": None}
with srv.SEQ_LOCK:
    s2 = srv._seq_read(sid)
    s2.setdefault("cuts", []).append(entry)
    s2["rev"] += 1
    srv._seq_write(s2)

# Force the true-peak measurement to fail, the way a broken/absent ebur128
# pass would -- everything else in the pipeline runs for real.
srv._measure_true_peak = lambda *a, **k: None

srv._run_cut(sid, cut_id, clip_plans, None, out_path, 320, 240)

s3 = srv._seq_read(sid)
c3 = next(c for c in s3["cuts"] if c["id"] == cut_id)
print("cut record:", json.dumps(c3, indent=2)[:1000])
check("cut finished (not stuck queued/running)", c3["status"] in ("done", "error"), c3["status"])
check("cut status is done", c3["status"] == "done", c3.get("log"))
check("loudness is not silent (real audio was fed in)", c3.get("loudness") != "silent", c3.get("loudness"))
check("cut record carries peak_unverified: true", c3.get("peak_unverified") is True, c3)

print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All P4 (peak_unverified) checks passed.")
