"""Beat-snap acceptance on the REAL "The Return" clips.

Not a test_* suite: it needs /models/scratch media (the Return's master mp3, plan.json, select.json, the 1080p P clips
rendered long on H3's 17n+5 grid, Claude's re-seated B/C/S parts at exact length, the north-star song map) and
bench/north-star's scorer, and SKIPs when they are absent. CPU only: the cut is a 1080p x264 encode of 4262 frames.

server.py runs in-process against a fake ComfyUI lane (it hands back the real files) and a process lane that runs the
Producer's real grid check (run B). Nothing here touches a live BWF: a scratch data dir, free ports only.

  Run A  Claude's song map (master.song_map.json) through set_song_map, bar_snap on, the fixture's bars, cut.
  Run B  BWF's OWN path: POST /api/sequence/analyze on the master (producer `grid`, beat_this), wait for the attach,
         set_bar_snap, the same bars, cut. Needs BWF_PRODUCER_PYTHON (a Python with beat_this) and a process lane.
Each run is scored with m4_sync.py against master.song_map.json and the plan's planned cuts, split into between-clip
and inside-clip cuts, and checked boundary by boundary (intended downbeat, f0/f1 vs plan.json, nearest downbeat).

Run: python3 tests/accept_bar_snap_return.py [--runs A,B] [--boundaries] [--out DIR] [--media DIR] [--ns DIR]
Exit 0 only when every gate of the chosen runs passes (SKIP exits 0 with a SKIP line).
"""
import argparse
import atexit
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

ap = argparse.ArgumentParser()
ap.add_argument("--runs", default="A,B")
ap.add_argument("--boundaries", action="store_true", help="print the per-boundary table")
ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "beatsnap-s3"))
ap.add_argument("--decoded-duration", action="store_true",
                help="DIAGNOSTIC: measure the master's length by decoding it (ffmpeg < 5 reports a container duration "
                     "that can be ~33 ms long for an mp3), instead of server.py's format.duration probe")
ap.add_argument("--media", default="/models/scratch/edrick/round10_the_return/video")
ap.add_argument("--ns", default=os.environ.get("BWF_NORTH_STAR", "/nonexistent/north-star"))
ARGS = ap.parse_args()
RUNS = [r for r in ARGS.runs.upper().split(",") if r]
MEDIA, NS, OUTDIR = ARGS.media, ARGS.ns, ARGS.out

NEEDED = ["plan.json", "select.json", "songmap/master.mp3", "songmap/master.song_map.json"]
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg/ffprobe not on PATH")
    sys.exit(0)
if not all(os.path.isfile(os.path.join(MEDIA, f)) for f in NEEDED) or not os.path.isfile(NS + "/m4_sync.py"):
    print("SKIP: the Return's media (%s) or the north-star scorer (%s) is not on this machine" % (MEDIA, NS))
    sys.exit(0)
try:
    import numpy as np  # noqa: F401
except ImportError:
    print("SKIP: numpy is not installed")
    sys.exit(0)

import engines  # noqa: E402
import beatsnap  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


def note(msg):
    print("  ....  " + msg)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


def counted_frames(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-count_frames", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames", path], capture_output=True, text=True)
    v = (json.loads(r.stdout).get("streams") or [{}])[0].get("nb_read_frames") if r.stdout else None
    return int(v) if v not in (None, "N/A") else None


os.makedirs(OUTDIR, exist_ok=True)
PRODUCER_PY = os.environ.get("BWF_PRODUCER_PYTHON", "")
if "B" in RUNS and not (PRODUCER_PY and os.access(PRODUCER_PY, os.X_OK)):
    print("SKIP: run B needs BWF_PRODUCER_PYTHON pointing at a Python with beat_this (the Producer's grid check)")
    sys.exit(0)

SCRATCH = tempfile.mkdtemp(prefix="bwf_accept_return_")
print("scratch dir: %s" % SCRATCH)
STORE = os.path.join(SCRATCH, "store_t")
OUT = os.path.join(STORE, "outputs")
os.makedirs(OUT)
PORT_T = free_port()
FAKE_T = subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
                           "--port", str(PORT_T), "--store", STORE], cwd=ROOT,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cleanup():
    if FAKE_T.poll() is None:
        FAKE_T.terminate()
    shutil.rmtree(SCRATCH, ignore_errors=True)


atexit.register(_cleanup)
for _ in range(100):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % PORT_T, timeout=0.5)
        break
    except Exception:
        time.sleep(0.05)
else:
    raise SystemExit("fake lane did not come up")

MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
for _role in engines.model_keys():
    if not MODELS.get(_role):
        MODELS[_role] = "stub_%s.safetensors" % _role


def load_server(tag):
    """server.py in this process, pointed at its own scratch data dir, the fake lane and (run B) a process lane."""
    data = os.path.join(SCRATCH, "data_" + tag)
    cfg = os.path.join(SCRATCH, "config_%s.json" % tag)
    lanes = [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": PORT_T, "caps": ["image", "video", "audio"]}]
    if "B" in RUNS:
        lanes.append({"id": "here", "kind": "process", "name": "CPU lane", "slots": 1, "caps": ["producer"]})
    json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "accept " + tag,
               "timing": {"poll_seconds": 30, "job_poll_seconds": 3, "http_timeout": 5.0}, "lanes": lanes},
              open(cfg, "w"))
    os.environ["GENCENTER_CONFIG"] = cfg
    os.environ["GENCENTER_DATA"] = data
    spec = importlib.util.spec_from_file_location("srv_accept_" + tag, os.path.join(ROOT, "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.JOBS_FILE = os.path.join(data, "jobs.json")
    m.SEQ_DIR = os.path.join(data, "sequences")
    m.SEQ_MEDIA_DIR = os.path.join(data, "seq")
    m.CHAIN_DIR = os.path.join(data, "chain")
    m.LOCAL_OUTPUTS_DIR = os.path.join(data, "outputs")
    m.UPLOADS_DIR = os.path.join(data, "uploads")
    for d in (m.SEQ_DIR, m.SEQ_MEDIA_DIR, m.CHAIN_DIR, m.LOCAL_OUTPUTS_DIR, m.UPLOADS_DIR):
        os.makedirs(d, exist_ok=True)
    m.LANE_BY_ID["t"]["models"] = dict(MODELS)
    with m.STATE_LOCK:
        m.LANE_STATE["t"] = {"up": True, "checked": time.time(), "err": ""}
    if "B" in RUNS:
        here = m.LANE_BY_ID["here"]
        threading.Thread(target=m.lane_poller, args=(here,), daemon=True).start()
    return m


def decoded_duration(path):
    sr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate",
                         "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-"], capture_output=True).stdout
    return len(pcm) / 2.0 / float(sr)


class Rig:
    def __init__(self, m):
        self.m = m

    def op(self, sid, name, **kw):
        m = self.m
        return m.seq_op(dict(kw, id=sid, rev=m.seq_get(sid)[0]["rev"], op=name))

    def ok(self, sid, name, **kw):
        body, code = self.op(sid, name, **kw)
        assert code == 200, (name, kw, body)
        return body

    def get(self, sid):
        return self.m.seq_get(sid)[0]

    def stored(self, sid):
        with open(os.path.join(self.m.SEQ_DIR, sid + ".json")) as f:
            return json.load(f)

    def new_seq(self, title):
        seq, _ = self.m.seq_create({"title": title, "mode": "sequence"})
        self.ok(seq["id"], "set_canvas", width=1920, height=1080)
        return seq["id"]

    def shot(self, sid, length, mode="fl2va"):
        v = {f["id"]: f["default"] for f in engines.fields("video", mode) if f.get("default") is not None}
        v.update(prompt="a shot", length=length, width=1920, height=1080)
        if any(f["id"] == "first_frame" for f in engines.fields("video", mode)):
            v["first_frame"] = "start.png"
        return self.ok(sid, "add_slot", lane="video", cap="video", mode=mode, values=v)["added_slot_id"]

    def import_master(self, sid, data, name):
        body, code = self.m.seq_master_import(sid, self.get(sid)["rev"], name, data)
        assert code == 200, body
        return body

    def make(self, sid, v, fname):
        """seq_generate for real (the take records the window it was made for), then the lane 'renders' fname."""
        req = urllib.request.Request("http://127.0.0.1:%d/_control/accept" % PORT_T,
                                     data=json.dumps({"outputs": [{"filename": fname, "subfolder": "", "type": "output"}]}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        body, code = self.m.seq_generate({"id": sid, "slot_id": v})
        if code != 200:
            return body, code
        jid = body["job"]["id"]
        with self.m.JOBS_LOCK:
            self.m.JOBS[jid] = {"id": jid, "lane": "t", "kind": "video", "mode": "fl2va", "status": "done", "prompt": "x",
                                "outputs": [{"filename": fname, "subfolder": "", "type": "output", "media": "video"}]}
        self.ok(sid, "pick_take", slot_id=v, job_id=jid)
        return body, code

    def cut(self, sid, timeout=1500):
        body, code = self.m.seq_cut_start({"id": sid})
        if code != 200:
            return None, body, code
        cut_id = body["cut_id"]
        end = time.time() + timeout
        while time.time() < end:
            entry = next((c for c in self.get(sid).get("cuts") or [] if c["id"] == cut_id), None)
            if entry and entry.get("status") in ("done", "error", "interrupted"):
                return entry, body, code
            time.sleep(1.0)
        return None, {"error": "cut timed out"}, 0


# -- the Return: plan, fixture, files ----------------------------------------------------------------------------------
PLAN = json.load(open(os.path.join(MEDIA, "plan.json")))
SELECT = json.load(open(os.path.join(MEDIA, "select.json")))
FX = json.load(open(os.path.join(HERE, "fixtures", "beatsnap_song_a.json")))
SEGS = FX["segments"]
PSEG = PLAN["segments"]
SCORE_MAP = json.load(open(os.path.join(MEDIA, "songmap/master.song_map.json")))
DOWN = SCORE_MAP["downbeats"]
MASTER_PATH = os.path.join(MEDIA, "songmap/master.mp3")
MASTER_BYTES = open(MASTER_PATH, "rb").read()


def clip_file(seg):
    """P: the render-long 1080p clip in vsr/ (select.json's pick, else P_<id>); B/C/S: Claude's exact-W part."""
    if seg["type"] == "P":
        return "vsr/%s.mp4" % SELECT.get(seg["id"], "P_" + seg["id"])
    return "seg/final/%s.mp4" % seg["id"]


print()
print("the inputs: the fixture's 34 segments are the plan's, and the master is the one the song map was made on")
check("34 segments in the fixture and in plan.json, ids in the same order", len(SEGS) == len(PSEG) == 34)
check("the fixture's f0/f1 equal plan.json's for every segment",
      all((a["f0"], a["f1"]) == (b["f0"], b["f1"]) for a, b in zip(SEGS, PSEG)),
      [i for i, (a, b) in enumerate(zip(SEGS, PSEG)) if (a["f0"], a["f1"]) != (b["f0"], b["f1"])])
check("the fixture's downbeats equal master.song_map.json's", FX["downbeats"] == DOWN)
check("the master mp3's sha256 is the song map's source sha256",
      hashlib.sha256(MASTER_BYTES).hexdigest() == SCORE_MAP["source"]["sha256"])
FILES = [clip_file(s) for s in PSEG]
check("every segment has its real clip on disk", all(os.path.isfile(os.path.join(MEDIA, f)) for f in FILES),
      [f for f in FILES if not os.path.isfile(os.path.join(MEDIA, f))])
if FAILED:
    sys.exit(1)
for i, f in enumerate(FILES):                       # the fake lane serves these by name
    os.symlink(os.path.join(MEDIA, f), os.path.join(OUT, "ret_%02d.mp4" % i))

mod = load_server("main")
R = Rig(mod)
if ARGS.decoded_duration:
    _orig_probe = mod._probe_duration
    mod._probe_duration = lambda path: decoded_duration(path) if "_master" in os.path.basename(path) else _orig_probe(path)
    note("DIAGNOSTIC: the master staged for analyze is measured by decoding, not by format.duration")
if "B" in RUNS:
    lane_up = None
    for _ in range(100):
        with mod.STATE_LOCK:
            lane_up = (mod.LANE_STATE.get("here") or {}).get("up")
        if lane_up and mod._grid_lane("here"):
            break
        time.sleep(0.1)
    check("run B: the process lane is up and can run the Producer's grid check", bool(mod._grid_lane("here")),
          (mod.LANE_STATE.get("here"), mod.models_for(mod.LANE_BY_ID["here"])))


# -- the leading-silence slice filter on THIS machine's ffmpeg, with the real master ------------------------------------
def decode_mono44(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "44100", "-f", "f32le", "-"], capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64)


def lag_samples(ref, x, lo, hi):
    """Samples by which x trails ref (x[n] ~ ref[n - lag]), found over x[lo:hi] by FFT cross-correlation."""
    seg = x[lo:hi]
    n = 1 << int(np.ceil(np.log2(len(ref) + len(seg))))
    cc = np.fft.irfft(np.fft.rfft(ref, n) * np.conj(np.fft.rfft(seg, n)), n)
    best = int(np.argmax(cc[:len(ref)]))              # ref index where seg starts matching
    return lo - best


def leading_silence_check():
    """Spec S3: the filter S2 uses for a window that starts before the master (`adelay=<ms>:all=1,apad`) must work on
    this ffmpeg (a box with 4.4.2; S2 was only tested on 8). (1) through the server: a tail-anchored, singing
    first shot whose slice starts before 0, as uploaded to the lane; (2) the same filter chain on its own, 750 ms."""
    ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    print()
    print("ffmpeg leading-silence filter on the real master (%s)" % ver)
    ref = decode_mono44(MASTER_PATH)
    sing = {}
    for pack in engines.packs():
        if pack["cap"] == "video":
            sing.update(pack.get("sing_along") or {})
    head = next(k for k, v in sing.items() if not v.get("carried_frames"))
    cont = next(k for k, v in sing.items() if v.get("carried_frames"))
    jack = next(f["id"] for f in engines.fields("video", cont) if f.get("type") == "video")
    sid = R.new_seq("lead silence")
    j1, j2 = R.shot(sid, 62, head), R.shot(sid, 62, cont)
    R.import_master(sid, MASTER_BYTES, "master.mp3")
    R.ok(sid, "set_song_map", song_map=SCORE_MAP)
    R.ok(sid, "patch", **{"from": j1, "to": j2, "field": jack})
    R.ok(sid, "set_audio_led", on=True)
    R.ok(sid, "set_bar_snap", on=True)
    R.ok(sid, "set_shot_bars", slot_id=j1, bars=2)
    R.ok(sid, "set_sing", slot_id=j1, on=True)
    w = next(x for x in R.get(sid)["slots"] if x["id"] == j1)["window"]
    check("the first shot is tail-anchored and its slice starts before the file (%.4f s)" % w["slice_start"],
          w["keep"] == "tail" and w["slice_start"] < 0, w)
    body, code = R.make(sid, j1, "ret_00.mp4")
    check("generate on it succeeds", code == 200, body)
    lp = json.load(open(os.path.join(STORE, "last_prompt.json")))
    load = [n for n in lp.values() if n.get("class_type") == "LoadAudio"]
    wav = os.path.join(STORE, "inputs", load[0]["inputs"]["audio"]) if len(load) == 1 else None
    x = decode_mono44(wav) if wav else np.zeros(1)
    lead = int(round(-w["slice_start"] * 44100))
    check("the uploaded wav runs render_frames/24 + 1/24 = %.4f s" % (w["render_frames"] / 24.0 + 1 / 24.0),
          abs(len(x) / 44100.0 - (w["render_frames"] / 24.0 + 1 / 24.0)) < 0.002, len(x) / 44100.0)
    check("its first %d samples (the lead-in, %.1f ms) are digital silence" % (lead - 2, lead / 44.1),
          len(x) > lead and float(np.abs(x[:max(0, lead - 2)]).max()) == 0.0)
    lag = lag_samples(ref, x, lead + 4410, min(len(x), lead + 4410 + 44100))
    check("the music is at the lead-in + its place in the master: lag %d samples (%.2f ms), expected %d +-1 ms"
          % (lag, lag / 44.1, lead), abs(lag - lead) <= 44, lag)
    # (2) the same chain on its own: a window that starts 750 ms before the file
    pos = -0.75
    af, seek, length = "adelay=%d:all=1,apad" % int(round(-pos * 1000)), 0.0, 5.0
    out = os.path.join(SCRATCH, "lead750.wav")
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "%.6f" % seek, "-i", MASTER_PATH,
                        "-af", af, "-t", "%.6f" % length, "-ar", "44100", "-ac", "2", out], capture_output=True, text=True)
    y = decode_mono44(out) if r.returncode == 0 else np.zeros(1)
    check("750 ms lead: the ffmpeg chain runs, %.3f s long" % (len(y) / 44100.0),
          r.returncode == 0 and abs(len(y) / 44100.0 - length) < 0.002, r.stderr[-300:])
    check("750 ms lead: the first 750 ms are digital silence", float(np.abs(y[:33000]).max()) == 0.0)
    lag = lag_samples(ref, y, 33075 + 4410, 33075 + 4410 + 44100)
    check("750 ms lead: the music trails the master by %d samples (%.2f ms), expected 33075 +-1 ms" % (lag, lag / 44.1),
          abs(lag - 33075) <= 44, lag)
    return {"ffmpeg": ver, "server_slice_start_s": w["slice_start"], "server_lead_samples": lead}


# -- scoring -----------------------------------------------------------------------------------------------------------
def run_m4(cut_path, tag):
    out_json = os.path.join(OUTDIR, "m4_%s.json" % tag)
    planned = ",".join("%g" % t for t in PLAN["planned_cuts_s"])
    r = subprocess.run([sys.executable, NS + "/m4_sync.py", cut_path, os.path.join(MEDIA, "songmap/master.song_map.json"),
                        "--planned", planned, "--out", out_json], capture_output=True, text=True, cwd=NS)
    if r.returncode != 0 or not os.path.isfile(out_json):
        raise RuntimeError("m4_sync.py failed: " + r.stderr[-800:])
    return json.load(open(out_json)), r.stdout


def boundary_report(wins, m4):
    """One row per segment boundary (33): the intended edge, the derived f0/f1 against plan.json, the detected cut,
    its delta to the intended downbeat, and a status. `wins` are the sequence's derived windows, in lane order."""
    bw = beatsnap.windows(DOWN, FX["end"],
                          [dict({k: v for k, v in z.items() if k in ("bars", "end_at", "to_end", "grid")}, id=str(j))
                           for j, z in enumerate(SEGS)],
                          {"fl2va": {"step": 17, "offset": 5, "min": 124, "max": 362, "carried": 0},
                           "exact": {"step": 1, "offset": 0, "min": 1, "max": 10000, "carried": 0}})
    cuts = [r["cut_s"] for r in m4["offsets"]]
    rows = []
    for i in range(1, len(SEGS)):
        prev = bw[i - 1]
        if prev["edge"] == "bars":
            kind, k_int = "bar", prev["k"] + prev["bars"]
            t_int = DOWN[k_int]
        else:
            kind, k_int, t_int = prev["edge"], None, SEGS[i - 1].get("end_at", FX["end"])
        f_int = round(t_int * 24)
        planned_t = PSEG[i]["f0"] / 24.0
        near = [c for c in cuts if abs(c - planned_t) <= 3 / 24.0]
        row = {"boundary": i, "after": PSEG[i - 1]["id"], "before": PSEG[i]["id"], "edge": kind, "intended_k": k_int,
               "intended_s": round(t_int, 4), "plan_f0": PSEG[i]["f0"], "bwf_f0": wins[i]["f0"],
               "bwf_prev_f1": wins[i - 1]["f1"], "plan_prev_f1": PSEG[i - 1]["f1"],
               "d_f0": wins[i]["f0"] - PSEG[i]["f0"], "d_prev_f1": wins[i - 1]["f1"] - PSEG[i - 1]["f1"]}
        if not near:
            row.update(detected_s=None, delta_ms=None, status="missing")
        else:
            c = min(near, key=lambda x: abs(x - planned_t))
            row.update(detected_s=c, delta_ms=round((c - round(t_int * 24) / 24.0) * 1000, 1))
            if kind == "bar":
                nearest = min(range(len(DOWN)), key=lambda j: (abs(DOWN[j] - c), j))
                row["nearest_k"] = nearest
                if nearest != k_int:
                    row["status"] = "misidentified"
                elif abs(c - round(t_int * 24) / 24.0) > 0.5 / 24:
                    row["status"] = "out-of-tolerance"
                else:
                    row["status"] = "ok"
            else:
                row["status"] = "ok" if abs(c - round(t_int * 24) / 24.0) <= 0.5 / 24 else "out-of-tolerance"
        rows.append(row)
    return rows


def print_boundaries(rows):
    print("  %3s %-9s %-4s %5s %9s %8s %8s %6s %11s  %s" % ("#", "after|before", "edge", "k", "intended", "plan f0",
                                                           "bwf f0", "d_f0", "delta ms", "status"))
    for r in rows:
        print("  %3d %-9s %-4s %5s %9.3f %8d %8d %6d %11s  %s" % (
            r["boundary"], r["after"] + "|" + r["before"], r["edge"], r["intended_k"] if r["intended_k"] is not None else "-",
            r["intended_s"], r["plan_f0"], r["bwf_f0"], r["d_f0"],
            "-" if r["delta_ms"] is None else "%.1f" % r["delta_ms"], r["status"]))


def split_between(m4):
    """Between-clip cuts = detected cuts at a segment boundary; the rest are inside multicut B/C clips."""
    bt = [PSEG[i]["f0"] / 24.0 for i in range(1, len(PSEG))]
    between, inside = [], []
    for r in m4["offsets"]:
        (between if any(abs(r["cut_s"] - t) <= 3 / 24.0 for t in bt) else inside).append(abs(r["offset_ms"]))
    return between, inside


RESULTS = {}
RESULTS["leading_silence"] = leading_silence_check()


def one_run(tag):
    print()
    print("=" * 118)
    print("RUN %s: %s" % (tag, "Claude's song map through set_song_map" if tag == "A" else
                           "BWF's own analyze (producer grid on a process lane) -> attach -> snap -> cut"))
    print("=" * 118)
    res = {"run": tag}
    sid = R.new_seq("return " + tag)
    vb = [R.shot(sid, 62) for _ in SEGS]
    R.import_master(sid, MASTER_BYTES, "master.mp3")
    if tag == "A":
        R.ok(sid, "set_song_map", song_map=SCORE_MAP)
    R.ok(sid, "set_audio_led", on=True)
    if tag == "B":
        t0 = time.time()
        body, code = mod.seq_analyze({"id": sid, "lane": "here"})
        check("B: POST /api/sequence/analyze answers {ok, job_id}", code == 200 and body.get("ok"), (code, body))
        jid = body.get("job_id")
        note("grid job %s started on the process lane" % jid)
        end = time.time() + 1800
        while time.time() < end:
            sj = R.stored(sid).get("song_map_job")
            if "song_map" in R.stored(sid) or (sj and sj.get("status") == "error"):
                break
            time.sleep(2)
        res["analyze_s"] = round(time.time() - t0, 1)
        sj = R.stored(sid).get("song_map_job")
        check("B: the beats attached (no error on song_map_job)", "song_map" in R.stored(sid), sj)
        if "song_map" not in R.stored(sid):
            return res
        sm = R.stored(sid)["song_map"]
        job = mod.JOBS.get(jid) or {}
        note("song_map.source.seconds = %r (north-star's duration_s is %r)" % (sm["source"]["seconds"], SCORE_MAP["source"]["duration_s"]))
        note("analyze took %.0f s; from=%s, %d downbeats, beats_per_bar=%s, bpm_median=%s; job notes: %s" % (
            res["analyze_s"], sm["from"], len(sm["downbeats"]), sm["beats_per_bar"], sm["bpm_median"], job.get("notes")))
        own = sm["downbeats"]
        res["tracker_parity"] = {"n_own": len(own), "n_ref": len(DOWN)}
        if len(own) == len(DOWN):
            d = [abs(a - b) * 1000 for a, b in zip(own, DOWN)]
            res["tracker_parity"].update(max_abs_ms=round(max(d), 2), over_10ms=sum(1 for x in d if x > 10),
                                         over_one_frame=sum(1 for x in d if x > 1000 / 24.0),
                                         differing=[(i, round(x, 2)) for i, x in enumerate(d) if x > 10])
        note("tracker parity vs master.song_map.json: %s" % json.dumps(res["tracker_parity"]))
        check("B: BWF's downbeat list has the north-star map's length and no downbeat is a video frame off",
              len(own) == len(DOWN) and res["tracker_parity"].get("over_one_frame") == 0, res["tracker_parity"])
    R.ok(sid, "set_bar_snap", on=True)
    for v, seg in zip(vb, SEGS):
        if "bars" in seg:
            R.ok(sid, "set_shot_bars", slot_id=v, bars=seg["bars"])
        elif "end_at" in seg:
            R.ok(sid, "set_shot_bars", slot_id=v, end_at=seg["end_at"])
        else:
            R.ok(sid, "set_shot_bars", slot_id=v, to_end=True)
    view = R.get(sid)
    wins = [view["slots"][i]["window"] for i in range(len(SEGS))]
    rep = view.get("snap_report") or {}
    check("%s: no snap warnings, nothing unplaced" % tag, not rep.get("warnings") and rep.get("unplaced_bars") == 0, rep)
    for v, i in zip(vb, range(len(SEGS))):
        body, code = R.make(sid, v, "ret_%02d.mp4" % i)
        assert code == 200, (i, body)
    view = R.get(sid)
    check("%s: after generating, no shot is stale" % tag, all(not s["stale"] for s in view["slots"]),
          [s["id"] for s in view["slots"] if s["stale"]])
    t0 = time.time()
    entry, body, code = R.cut(sid)
    check("%s: the snapped cut finishes" % tag, bool(entry and entry.get("status") == "done"),
          (code, body, entry and entry.get("log")))
    if not (entry and entry.get("status") == "done"):
        return res
    cut_src = os.path.join(mod.SEQ_MEDIA_DIR, sid, entry["file"])
    cut_path = os.path.join(OUTDIR, "the_return_bwf_run%s.mp4" % tag)
    shutil.copyfile(cut_src, cut_path)
    res.update(cut=cut_path, cut_s=round(time.time() - t0, 1))
    frames = counted_frames(cut_path)
    res["frames"] = frames
    check("%s: output frames 4262" % tag, frames == 4262, frames)
    m4, stdout = run_m4(cut_path, tag)
    between, inside = split_between(m4)
    rows = boundary_report(wins, m4)
    res.update(m4_n_cuts=m4["n_cuts"], median_ms=m4["median_abs_ms"], p90_ms=m4["p90_abs_ms"], planned=m4["planned"],
               between_n=len(between), between_median_ms=round(statistics.median(between), 1),
               inside_n=len(inside), inside_median_ms=round(statistics.median(inside), 1) if inside else None,
               null_ms=m4["null_median_abs_ms"], rows=rows)
    note("M4: %d cuts, median %.1f ms, p90 %.1f ms, null %.1f ms, planned detected %s" % (
        m4["n_cuts"], m4["median_abs_ms"], m4["p90_abs_ms"], m4["null_median_abs_ms"], m4["planned"]))
    note("split: %d between-clip cuts, median %.1f ms; %d inside-clip cuts, median %s ms" % (
        len(between), res["between_median_ms"], len(inside), res["inside_median_ms"]))
    check("%s: median <= 13.8 ms" % tag, m4["median_abs_ms"] <= 13.8, m4["median_abs_ms"])
    check("%s: p90 <= 42 ms" % tag, m4["p90_abs_ms"] <= 42.0, m4["p90_abs_ms"])
    check("%s: between-clip median <= 14.7 ms" % tag, res["between_median_ms"] <= 14.7, res["between_median_ms"])
    check("%s: planned detected >= 50/52" % tag, m4["planned"]["n"] == 52 and m4["planned"]["detected"] >= 50, m4["planned"])
    count = {s: sum(1 for r in rows if r["status"] == s) for s in ("ok", "out-of-tolerance", "missing", "misidentified")}
    res["boundary_counts"] = count
    note("per boundary: %s" % json.dumps(count))
    if ARGS.boundaries:
        print_boundaries(rows)
    check("%s: per boundary, 0 misidentified" % tag, count["misidentified"] == 0, [r for r in rows if r["status"] == "misidentified"])
    check("%s: per boundary, 0 out of tolerance" % tag, count["out-of-tolerance"] == 0,
          [r for r in rows if r["status"] == "out-of-tolerance"])
    s_edges = {i for i in range(1, len(PSEG)) if PSEG[i]["type"] == "S" or PSEG[i - 1]["type"] == "S"}
    missing = [r for r in rows if r["status"] == "missing"]
    note("missing boundaries: %s" % [(r["boundary"], r["after"] + "|" + r["before"]) for r in missing])
    check("%s: missing only at the freeze edges (S segments), at most 2" % tag,
          all(r["boundary"] in s_edges for r in missing) and len(missing) <= 2, missing)
    bad = [r for r in rows if r["d_f0"] != 0 or r["d_prev_f1"] != 0]
    check("%s: f0/f1 delta vs plan.json is 0 at every boundary" % tag, not bad,
          [(r["boundary"], r["d_f0"], r["d_prev_f1"]) for r in bad])
    res["f0f1_nonzero"] = [(r["boundary"], r["d_f0"], r["d_prev_f1"]) for r in bad]
    return res


for tag in RUNS:
    RESULTS[tag] = one_run(tag)

json.dump(RESULTS, open(os.path.join(OUTDIR, "results.json"), "w"), indent=1)
print()
print("results written to %s" % os.path.join(OUTDIR, "results.json"))
print()
if FAILED:
    print("%d FAILED: %s" % (len(FAILED), FAILED))
    sys.exit(1)
print("ALL PASS")
