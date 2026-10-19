"""Beat-snapped Cutting Room.

An audio-led sequence can carry a stored song map and a `bar_snap` switch: every video shot's window then becomes
whole bars between detected downbeats, its edges rounded to 24 fps frames cumulatively, and it renders on its
model's frame grid at the smallest length that covers the window; the cut trims it back by frame index.

(a) a sequence that never uses this is byte-identical to the code before it (golden captured from b6ad2d1);
(b) the Return's 34 segments through the real ops, generate and cut: 4262 frames, every cut on f0;
(c) bench/north-star/m4_sync.py on that cut (skipped when the north-star files are absent);
(d) H3 sings to an imported master (tone markers); (e) a continue chain, tail-anchored; (f) a bar change makes the
later takes stale and says how many; (g) refusal sentences; (h) a snapped cut refuses a take made for another window;
(i) CFR; (j) a window that starts before the file (leading silence); (k) north-star import and its sha256;
(l) analyze identity (a stubbed grid job); (m) E1 precedence; (n) half-to-even rounding vs the nearest-downbeat tie.

server.py runs in-process against a fake ComfyUI lane and a scratch data dir (the test_sing_along_cut shape).
Run: python3 tests/test_bar_snap.py
Maintainers: python3 tests/test_bar_snap.py --capture OUT.json with BAR_SNAP_CAPTURE_REPO=<an older checkout>
re-captures the golden for (a).
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
OWN_ROOT = os.path.dirname(HERE)
CAPTURE = "--capture" in sys.argv
ROOT = os.environ.get("BAR_SNAP_CAPTURE_REPO") or OWN_ROOT if CAPTURE else OWN_ROOT
sys.path.insert(0, ROOT)

if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg/ffprobe not on PATH -- this suite needs both")
    sys.exit(0)
try:
    import numpy as np
except ImportError:
    print("SKIP: numpy is not installed (requirements.txt) -- this suite's frame and tone checks need it")
    sys.exit(0)

import engines
import beatsnap

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

def ff(args):
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-1500:])

def ffprobe_json(path, extra=()):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json"] + list(extra) + [path],
                       capture_output=True, text=True)
    return json.loads(r.stdout) if r.stdout else {}

def counted_frames(path):
    info = ffprobe_json(path, ["-count_frames", "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames"])
    v = (info.get("streams") or [{}])[0].get("nb_read_frames")
    return int(v) if v not in (None, "N/A") else None

SCRATCH = tempfile.mkdtemp(prefix="bwf_bar_snap_")
print("scratch dir: %s" % SCRATCH)
STORE = os.path.join(SCRATCH, "store_t")
OUT = os.path.join(STORE, "outputs")
os.makedirs(OUT)
PORT_T = free_port()
FAKE_T = subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
                           "--port", str(PORT_T), "--store", STORE],
                          cwd=OWN_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
import atexit
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
    """server.py (from ROOT) in this process, pointed at its own scratch data dir and the fake lane."""
    data = os.path.join(SCRATCH, "data_" + tag)
    cfg = os.path.join(SCRATCH, "config_%s.json" % tag)
    json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "bar snap " + tag,
               "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 5.0},
               "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": PORT_T,
                          "caps": ["image", "video", "audio"]}]}, open(cfg, "w"))
    os.environ["GENCENTER_CONFIG"] = cfg
    os.environ["GENCENTER_DATA"] = data
    spec = importlib.util.spec_from_file_location("srv_bar_snap_" + tag, os.path.join(ROOT, "server.py"))
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
    return m


# -- media -------------------------------------------------------------------------------------------------------------
W, H = 192, 96


def wav_bytes(seconds, bursts=(), sr=8000):
    """Mono 16-bit wav: silence, with a 100 ms 1 kHz tone starting at each time in `bursts`."""
    n = int(round(seconds * sr))
    x = np.zeros(n, dtype=np.float64)
    for t in bursts:
        a = int(round(t * sr))
        k = np.arange(int(0.1 * sr))
        x[a:a + len(k)] = 0.5 * np.sin(2 * np.pi * 1000 * k / sr)
    pcm = (x * 32767).astype("<i2").tobytes()
    import struct
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
    return hdr + b"data" + struct.pack("<I", len(pcm)) + pcm


def decode_mono(path, sr=8000):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64)


def burst_onset(x, sr=8000, thresh=0.1):
    """Seconds of the first sample whose 5 ms envelope exceeds `thresh`, or None."""
    k = max(1, int(0.005 * sr))
    env = np.convolve(np.abs(x), np.ones(k) / k, mode="same")
    hit = np.where(env > thresh)[0]
    return float(hit[0]) / sr if len(hit) else None


def palette(n):
    out = []
    for i in range(n):
        v = (i * 37) % 216
        out.append(((v // 36) * 50, ((v // 6) % 6) * 50, (v % 6) * 50))
    return out


def solid(path, frames, rgb, fps=24, extra=()):
    ff(["-f", "lavfi", "-i", "color=c=0x%02x%02x%02x:size=%dx%d:rate=%s" % (rgb + (W, H, fps)),
        "-frames:v", str(frames), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "48"] + list(extra) + [path])
    return path


def two_part(path, n_first, rgb_first, n_second, rgb_second):
    """n_first frames of one colour then n_second of another (to prove which end of a take the cut kept)."""
    a = solid(path + ".a.mp4", n_first, rgb_first)
    b = solid(path + ".b.mp4", n_second, rgb_second)
    lst = path + ".txt"
    open(lst, "w").write("file '%s'\nfile '%s'\n" % (a, b))
    ff(["-f", "concat", "-safe", "0", "-i", lst, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "24", path])
    return path


def frame_colours(path, pal):
    """Index of the nearest palette colour for every frame of `path`."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=2:2:flags=area,format=rgb24",
                        "-f", "rawvideo", "-"], capture_output=True)
    a = np.frombuffer(r.stdout, dtype=np.uint8).reshape(-1, 4, 3).astype(np.float64).mean(1)
    d = ((a[:, None, :] - np.array(pal, dtype=np.float64)[None]) ** 2).sum(2)
    return d.argmin(1)


# -- the fixture: the Return's downbeats and its 34 segments -----------------------------------------------------------
FX = json.load(open(os.path.join(HERE, "fixtures", "beatsnap_song_a.json")))
SEGS = FX["segments"]
DOWN = FX["downbeats"]


def north_star_map(data, seconds, downbeats, lines=None, sha=None, per_bar=4):
    beats = []
    for a, b in zip(downbeats, downbeats[1:]):
        beats += [a + (b - a) * k / per_bar for k in range(per_bar)]
    beats.append(downbeats[-1])
    return {"schema": "north-star/song_map/v1", "name": "x",
            "source": {"path": "/somewhere/else/song.mp3", "sha256": sha or hashlib.sha256(data).hexdigest(),
                       "duration_s": seconds},
            "beats": beats, "downbeats": list(downbeats), "bars": [], "meter": per_bar,
            "tempo": {"median_bpm": 96.77}, "sections": [], "lyrics": {"lines": lines or []}}


def len_for_bars(n, bar_s=2.4):
    """A slot `length` (frames) whose planned run is about n bars, as a person sizing shots by hand would."""
    return int(n * bar_s * 24) + 1


def regular_downbeats(first, bar_s, seconds):
    out, t = [], first
    while t < seconds - 0.01:
        out.append(round(t, 4))
        t += bar_s
    return out


class Rig:
    """Thin helpers around one in-process server module."""
    def __init__(self, m):
        self.m = m
        self.SING = {}
        for pack in engines.packs():
            if pack["cap"] == "video":
                self.SING.update(pack.get("sing_along") or {})
        self.CONT = next((k for k, s in self.SING.items() if s.get("carried_frames")), None)
        self.HEAD = next((k for k, s in self.SING.items() if not s.get("carried_frames")), None)
        self.JACK = next((f["id"] for f in engines.fields("video", self.CONT) if f.get("type") == "video"), None)
        self.n_jobs = 0

    def op(self, sid, name, **kw):
        m = self.m
        return m.seq_op(dict(kw, id=sid, rev=m.seq_get(sid)[0]["rev"], op=name))

    def ok(self, sid, name, **kw):
        body, code = self.op(sid, name, **kw)
        assert code == 200, (name, kw, body)
        return body

    def get(self, sid):
        return self.m.seq_get(sid)[0]

    def slot(self, sid, slot_id):
        return next(s for s in self.get(sid)["slots"] if s["id"] == slot_id)

    def stored(self, sid):
        with open(os.path.join(self.m.SEQ_DIR, sid + ".json")) as f:
            return json.load(f)

    def new_seq(self, title, canvas=True):
        seq, _ = self.m.seq_create({"title": title, "mode": "sequence"})
        if canvas:
            self.ok(seq["id"], "set_canvas", width=W, height=H)
        return seq["id"]

    def shot(self, sid, mode="fl2va", length=62, **values):
        v = {f["id"]: f["default"] for f in engines.fields("video", mode) if f.get("default") is not None}
        v.update(prompt="a shot", length=length, width=W, height=H)
        if any(f["id"] == "first_frame" for f in engines.fields("video", mode)):
            v["first_frame"] = "start.png"
        v.update(values)
        return self.ok(sid, "add_slot", lane="video", cap="video", mode=mode, values=v)["added_slot_id"]

    def import_master(self, sid, data, name="master.wav"):
        body, code = self.m.seq_master_import(sid, self.get(sid)["rev"], name, data)
        assert code == 200, body
        return body

    def done_job(self, job_id, fname, kind="video", mode="fl2va", media="video"):
        with self.m.JOBS_LOCK:
            self.m.JOBS[job_id] = {"id": job_id, "lane": "t", "kind": kind, "mode": mode, "status": "done",
                                   "prompt": "x", "outputs": [{"filename": fname, "subfolder": "", "type": "output",
                                                               "media": media}]}

    def control(self, outputs):
        req = urllib.request.Request("http://127.0.0.1:%d/_control/accept" % PORT_T,
                                     data=json.dumps({"outputs": outputs}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()

    def make(self, sid, v, fname, mode=None, pick=True):
        """seq_generate for real (so the take records what it was made against), then the lane 'renders' fname."""
        self.control([{"filename": fname, "subfolder": "", "type": "output"}])
        body, code = self.m.seq_generate({"id": sid, "slot_id": v})
        if code != 200:
            return body, code
        mode = mode or self.slot(sid, v)["mode"]
        self.done_job(body["job"]["id"], fname, "video", mode, "video")
        if pick:
            self.ok(sid, "pick_take", slot_id=v, job_id=body["job"]["id"])
        return body, code

    def last_prompt(self):
        with open(os.path.join(STORE, "last_prompt.json")) as f:
            return json.load(f)

    def cut(self, sid, timeout=300):
        body, code = self.m.seq_cut_start({"id": sid})
        if code != 200:
            return None, body, code
        cut_id = body["cut_id"]
        end = time.time() + timeout
        while time.time() < end:
            entry = next((c for c in self.get(sid).get("cuts") or [] if c["id"] == cut_id), None)
            if entry and entry.get("status") in ("done", "error", "interrupted"):
                return entry, body, code
            time.sleep(0.3)
        return None, {"error": "cut timed out"}, 0

    def cut_path(self, sid, entry):
        return os.path.join(self.m.SEQ_MEDIA_DIR, sid, entry["file"])

    def uploaded_wav(self):
        graph = self.last_prompt()
        load = [n for n in graph.values() if n.get("class_type") == "LoadAudio"]
        if len(load) != 1:
            return None, graph
        return os.path.join(STORE, "inputs", load[0]["inputs"]["audio"]), graph


def refusal(body, code, *needles):
    err = (body or {}).get("error", "") if isinstance(body, dict) else ""
    return code == 400 and all(n.lower() in err.lower() for n in needles), (code, err)


# =====================================================================================================================
# (a) off means byte-identical: the same scenario, run on the code before this slice, gave this golden
# =====================================================================================================================
GOLDEN_PATH = os.path.join(HERE, "fixtures", "bar_snap_off_golden.json")


def norm_dump(obj, replacements):
    def strip(o):
        if isinstance(o, dict):
            return {k: strip(v) for k, v in o.items() if k not in ("created", "updated", "made", "started")}
        if isinstance(o, list):
            return [strip(v) for v in o]
        return o
    text = json.dumps(strip(obj), sort_keys=True, indent=1)
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def scenario_off(m):
    """Everything an unsnapped sequence can do today, then its stored file and its GET body (volatile bits stripped)."""
    r = Rig(m)
    master = wav_bytes(30.0, bursts=(5.0,))
    m.SONG_SECONDS["golden_song"] = 70.0
    reps, out = [], {}
    # an audio-led sequence: LTX shots, a lip-sync opt-out, a trim, an imported master with a start, one real generate
    sid = r.new_seq("golden off")
    a = r.shot(sid, "ltx", length=97)
    b = r.shot(sid, "ltx", length=121)
    c = r.shot(sid, r.HEAD, length=124)
    r.ok(sid, "set_audio_led", on=True)
    r.import_master(sid, master)
    r.ok(sid, "set_master_start", start=1.5)
    r.ok(sid, "set_lipsync", slot_id=b, on=False)
    r.ok(sid, "set_trim", slot_id=c, trim={"in": 0.25, "len": 2.5})
    solid(os.path.join(OUT, "golden_a.mp4"), 97, (200, 0, 0))
    mk_body, mk_code = r.make(sid, a, "golden_a.mp4", mode="ltx", pick=False)
    assert mk_code == 200, mk_body
    take = r.slot(sid, a)["takes"][0]
    reps += [(sid, "SEQ"), (take["job_id"], "JOB_A")]
    out["audio_led_stored"] = norm_dump(r.stored(sid), reps)
    out["audio_led_get"] = norm_dump(r.get(sid), reps)
    out["audio_led_audio_inputs"] = (take.get("inputs") or {}).get("audio_window")
    # an E1 sequence: a song, a singing chain head and a cabled continue shot
    sid2 = r.new_seq("golden sing")
    s_id = r.ok(sid2, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "a song"})["added_slot_id"]
    r.done_job("golden_song", "song.flac", "audio", "sfx", "audio")
    m.seq_add_take(sid2, s_id, "golden_song")
    r.ok(sid2, "pick_take", slot_id=s_id, job_id="golden_song")
    v1 = r.shot(sid2, r.HEAD, length=124)
    v2 = r.shot(sid2, r.CONT, length=124)
    r.ok(sid2, "patch", **{"from": v1, "to": v2, "field": r.JACK})
    r.ok(sid2, "set_sing", slot_id=v1, on=True)
    r.ok(sid2, "set_sing", slot_id=v1, start=12.5)
    r.ok(sid2, "set_sing", slot_id=v2, on=True)
    reps2 = [(sid2, "SEQ2")]
    out["sing_stored"] = norm_dump(r.stored(sid2), reps2)
    out["sing_get"] = norm_dump(r.get(sid2), reps2)
    return out


if CAPTURE:
    out_path = sys.argv[sys.argv.index("--capture") + 1]
    mod = load_server("capture")
    solid(os.path.join(OUT, "golden_a.mp4"), 97, (200, 0, 0))
    json.dump(scenario_off(mod), open(out_path, "w"), indent=1, sort_keys=True)
    print("captured the golden from %s -> %s" % (ROOT, out_path))
    sys.exit(0)

mod = load_server("main")
R = Rig(mod)
CONT, HEAD, JACK = R.CONT, R.HEAD, R.JACK
check("sanity: a pack declares sing_along for a plain mode and a continue mode (with a video jack)",
      bool(CONT and HEAD and JACK), (CONT, HEAD, JACK))

print()
print("(a) a sequence that never uses beat snap is byte-identical to the code before it")
golden = json.load(open(GOLDEN_PATH))
solid(os.path.join(OUT, "golden_a.mp4"), 97, (200, 0, 0))
now = scenario_off(mod)
for key in sorted(golden):
    check("%s equals the golden captured from the pre-snap code" % key, now.get(key) == golden[key],
          "differs" if now.get(key) != golden[key] else "")
NEW_KEYS = ("bar_snap", "song_map", "song_map_job", "song_map_stale", "snap_report", "snap_error")
body = json.loads(now["audio_led_get"])
check("an unsnapped GET body carries none of the new keys",
      not any(k in body for k in NEW_KEYS) and all("follows_song" not in s for s in body["slots"]), [k for k in NEW_KEYS if k in body])
check("an unsnapped window is still just {start, len}",
      all(set(s.get("window") or {"start": 0, "len": 0}) == {"start", "len"} for s in body["slots"] if s["lane"] == "video"))

print()
print("frame_grid is pack data: H3 and LTX declare it, and a data pack may too")
G = {k: R.m._frame_grid("video", k) for k in ("fl2va", "continue", "ref2v", "ltx", "ltx_loop")}
check("H3 fl2va / continue / ref2v and LTX declare frame_grid per the spec",
      G["fl2va"] == {"step": 17, "offset": 5, "min": 124, "max": 362, "carried": 0}
      and G["continue"] == {"step": 17, "offset": 5, "min": 124, "max": 362, "carried": 22}
      and G["ref2v"] == G["fl2va"] and G["ltx"] == {"step": 8, "offset": 1, "min": 9, "max": 993, "carried": 0}, G)
check("a mode without one (ltx_loop) has none", G["ltx_loop"] is None)
from engines import _userpacks as up
PACK_DATA = os.path.join(SCRATCH, "packdata")


def write_pack(name, **extra):
    d = os.path.join(PACK_DATA, "packs", name)
    os.makedirs(d, exist_ok=True)
    man = {"schema": 1, "id": name, "cap": "video", "mode": "demograd", "label": "Demo", "graph": "g.json",
           "licence": {"name": "Demo"}}
    man.update(extra)
    json.dump(man, open(os.path.join(d, "manifest.json"), "w"))
    json.dump({"1": {"class_type": "X", "inputs": {}}}, open(os.path.join(d, "g.json"), "w"))


def load_packs():
    return up.load_user_packs(PACK_DATA, builtin=engines.packs())

write_pack("gridok", frame_grid={"demograd": {"step": 4, "offset": 1, "min": 5, "max": 65}})
good, problems = load_packs()
check("a data pack's frame_grid loads, carried defaulting to 0",
      [p.get("frame_grid") for p in good] == [{"demograd": {"step": 4, "offset": 1, "min": 5, "max": 65, "carried": 0}}], (good, problems))
shutil.rmtree(os.path.join(PACK_DATA, "packs", "gridok"))
for label, bad in (("a grid for a different mode", {"other": {"step": 4, "offset": 1, "min": 5, "max": 65}}),
                   ("a zero step", {"demograd": {"step": 0, "offset": 1, "min": 5, "max": 65}}),
                   ("min above max", {"demograd": {"step": 4, "offset": 1, "min": 70, "max": 65}}),
                   ("a float", {"demograd": {"step": 4.5, "offset": 1, "min": 5, "max": 65}}),
                   ("a boolean", {"demograd": {"step": True, "offset": 1, "min": 5, "max": 65}}),
                   ("an unknown key", {"demograd": {"step": 4, "offset": 1, "min": 5, "max": 65, "x": 1}}),
                   ("a missing key", {"demograd": {"step": 4, "offset": 1, "min": 5}})):
    write_pack("gridbad", frame_grid=bad)
    got, probs = load_packs()
    check("a data pack with %s is skipped with a reason" % label, not got and any("frame_grid" in p for p in probs), (got, probs))
    shutil.rmtree(os.path.join(PACK_DATA, "packs", "gridbad"))

# =====================================================================================================================
# (k) north-star import: the sha256 must match the master; the map is normalised
# =====================================================================================================================
print()
print("(k) set_song_map takes a north-star map only for the master the sequence has")
sidk = R.new_seq("import")
MASTER_K = wav_bytes(60.0, bursts=(20.0,))
DB_K = regular_downbeats(0.5, 2.4, 60.0)
LINES_K = [{"start": 3.0, "text": "first line"}, {"start": 12.5, "text": "second line"}]
body, code = R.op(sidk, "set_song_map", song_map=north_star_map(MASTER_K, 60.0, DB_K))
check("no master: refused with a sentence about the master", *refusal(body, code, "master"))
R.import_master(sidk, MASTER_K)
other = north_star_map(wav_bytes(60.0, bursts=(21.0,)), 60.0, DB_K)
body, code = R.op(sidk, "set_song_map", song_map=other)
check("a map for different bytes is refused: 'this song map is for a different song'", *refusal(body, code, "different song"))
check("and nothing was stored", "song_map" not in R.stored(sidk))
body, code = R.op(sidk, "set_song_map", song_map=dict(north_star_map(MASTER_K, 60.0, DB_K), schema="something/else"))
check("another schema is refused", *refusal(body, code, "north-star"))
bad_down = north_star_map(MASTER_K, 60.0, DB_K)
bad_down["downbeats"] = DB_K[:5] + [DB_K[4]] + DB_K[6:]
body, code = R.op(sidk, "set_song_map", song_map=bad_down)
check("downbeats that do not increase are refused", *refusal(body, code, "order"))
few = north_star_map(MASTER_K, 60.0, DB_K)
few["beats"] = few["beats"][:7]
body, code = R.op(sidk, "set_song_map", song_map=few)
check("fewer than 8 beats are refused", *refusal(body, code, "beats"))
nan_map = north_star_map(MASTER_K, 60.0, DB_K)
nan_map["beats"][3] = float("nan")
body, code = R.op(sidk, "set_song_map", song_map=nan_map)
check("a non-finite beat is refused", *refusal(body, code, "finite"))
good_map = north_star_map(MASTER_K, 60.0, DB_K, lines=LINES_K)
good_map["sections"] = [{"junk": 1}]
good_map["tools"] = {"x": 1}
body, code = R.op(sidk, "set_song_map", song_map=good_map)
check("a map with the master's sha256 is accepted", code == 200 and body.get("song_map"), (code, body.get("error")))
sm = R.stored(sidk).get("song_map") or {}
check("duration_s became source.seconds; kind and ref come from the master, path is informational only",
      sm.get("source") == {"kind": "file", "ref": "master.wav", "sha256": hashlib.sha256(MASTER_K).hexdigest(),
                           "seconds": 60.0, "import_path": "/somewhere/else/song.mp3"}, sm.get("source"))
check("downbeats, meter, tempo and the lyric lines were taken; sections and tools were dropped",
      sm.get("downbeats") == DB_K and sm.get("beats_per_bar") == 4 and sm.get("bpm_median") == 96.77
      and sm.get("lines") == LINES_K and sm.get("from") == "import" and sm.get("v") == 1
      and "sections" not in sm and "tools" not in sm, {k: v for k, v in sm.items() if k not in ("beats", "downbeats")})
check("a fresh map is not stale", R.get(sidk).get("song_map_stale") is False)
body = R.ok(sidk, "set_song_map", lines_text="[0:03.0] sung line\n[0:12.5] another\n")
check("pasted [m:ss.x] lines replace the lines", R.stored(sidk)["song_map"]["lines"] ==
      [{"start": 3.0, "text": "sung line"}, {"start": 12.5, "text": "another"}], R.stored(sidk)["song_map"].get("lines"))
body, code = R.op(sidk, "set_song_map", lines_text="not a line")
check("a malformed pasted line is refused with a sentence", *refusal(body, code, "line 1"))
R.import_master(sidk, wav_bytes(60.0, bursts=(22.0,)))
check("replacing the master makes the map stale in the GET body", R.get(sidk).get("song_map_stale") is True)
body, code = R.op(sidk, "set_audio_led", on=True)
body, code = R.op(sidk, "set_bar_snap", on=True)
check("a stale map blocks Snap to beats with a sentence", *refusal(body, code, "changed", "again"))

# =====================================================================================================================
# (n) rounding: half to even on frames; the nearest-downbeat tie goes earlier. Two separate rules.
# =====================================================================================================================
print()
print("(n) t*24 at exactly k+0.5 rounds to the even frame; a start midway between downbeats takes the earlier one")
G1 = {"x": {"step": 1, "offset": 0, "min": 1, "max": 10000, "carried": 0}}
d_half = [0.0, 0.1875, 0.3125, 0.4375, 1.0]       # x24 = 0, 4.5, 7.5, 10.5, 24
w = beatsnap.windows(d_half, 1.0, [{"id": "a", "bars": 1, "grid": "x"}, {"id": "b", "bars": 1, "grid": "x"},
                                   {"id": "c", "bars": 1, "grid": "x"}, {"id": "d", "to_end": True, "grid": "x"}], G1)
check("4.5 -> 4, 7.5 -> 8, 10.5 -> 10 (half to even, not half up)", [x["f1"] for x in w[:3]] == [4, 8, 10], [x["f1"] for x in w[:3]])
check("the shots tile around them", [(x["f0"], x["f1"]) for x in w] == [(0, 4), (4, 8), (8, 10), (10, 24)])
tie = beatsnap.windows([1.0, 2.0, 3.0], 3.0, [{"id": "a", "end_at": 1.5, "grid": "x"}, {"id": "b", "bars": 1, "grid": "x"}], G1)
check("a start exactly midway between D[0] and D[1] counts its bars from D[0] (the earlier one)",
      tie[1]["k"] == 0 and (tie[1]["f0"], tie[1]["f1"]) == (36, 48), tie[1])
# the same through the server: a bar line at exactly k+0.5 frames
sidn = R.new_seq("half frame")
dn = [0.0, 0.1875, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
MASTER_N = wav_bytes(10.0)
R.import_master(sidn, MASTER_N)
nm = north_star_map(MASTER_N, 10.0, dn)
R.ok(sidn, "set_song_map", song_map=nm)
vn = [R.shot(sidn, "ltx", length=97), R.shot(sidn, "ltx", length=97)]
R.ok(sidn, "set_audio_led", on=True)
R.ok(sidn, "set_bar_snap", on=True)
R.ok(sidn, "set_shot_bars", slot_id=vn[0], bars=1)
win = R.slot(sidn, vn[0]).get("window") or {}
check("the server rounds a 4.5-frame bar line to frame 4 (GET window f1)", win.get("f1") == 4 and win.get("f0") == 0, win)

# =====================================================================================================================
# (b)/(c) the Return: 34 segments through the real ops, generate and cut
# =====================================================================================================================
print()
print("(b) the Return's 34 segments: ops, generate on the frame grid, cut over a silent 177.6 s master")
sidb = R.new_seq("the return")
# each shot is planned near its real size (as a person would), so Snap to beats can fill and every state in between fits
vb = [R.shot(sidb, "fl2va", length=int(max(1, round((s["f1"] - s["f0"]) / 24 / 2.44)) * 2.44 * 24) + 1) for s in SEGS]
MASTER_B = wav_bytes(FX["end"])
R.import_master(sidb, MASTER_B)
R.ok(sidb, "set_song_map", song_map=north_star_map(MASTER_B, FX["end"], DOWN))
R.ok(sidb, "set_audio_led", on=True)
body = R.ok(sidb, "set_bar_snap", on=True)
check("Snap to beats turns on and fills the auto shots with explicit bars (all but the last)",
      R.stored(sidb).get("bar_snap") is True and all("bars" in s for s in R.stored(sidb)["slots"][:-1])
      and "bars" not in R.stored(sidb)["slots"][-1], [s.get("bars") for s in R.stored(sidb)["slots"]][:6])
for v, seg in zip(vb, SEGS):
    if "bars" in seg:
        R.ok(sidb, "set_shot_bars", slot_id=v, bars=seg["bars"])
    elif "end_at" in seg:
        R.ok(sidb, "set_shot_bars", slot_id=v, end_at=seg["end_at"])
    else:
        R.ok(sidb, "set_shot_bars", slot_id=v, to_end=True)
view = R.get(sidb)
wins = [view["slots"][i]["window"] for i in range(len(SEGS))]
check("every shot's derived window has the reference's f0 and f1 (0 frames of difference)",
      all((x["f0"], x["f1"]) == (s["f0"], s["f1"]) for x, s in zip(wins, SEGS)),
      [i for i, (x, s) in enumerate(zip(wins, SEGS)) if (x["f0"], x["f1"]) != (s["f0"], s["f1"])])
check("the windows sum to 4262 frames and the report says nothing is left over",
      sum(x["f1"] - x["f0"] for x in wins) == 4262 and view["snap_report"]["unplaced_bars"] == 0, view.get("snap_report"))
check("the length field is derived: every shot reports follows_song False for a non-singing H3 shot",
      all(s["follows_song"] is False for s in view["slots"]))
PAL = palette(len(SEGS))
for i, (v, seg) in enumerate(zip(vb, SEGS)):
    n = wins[i]["render_frames"] if seg["grid"] == "fl2va" else wins[i]["f1"] - wins[i]["f0"]
    solid(os.path.join(OUT, "ret_%02d.mp4" % i), n, PAL[i])
    body, code = R.make(sidb, v, "ret_%02d.mp4" % i)
    assert code == 200, body
    lp = R.last_prompt()
    if i == 0:
        node = next(n for n in lp.values() if n.get("class_type") == "MiniMaxH3ImageToVideo")
        check("generate sent the window's render_frames as the length (not the slot's own)",
              node["inputs"]["length"] == wins[0]["render_frames"] != 62, (node["inputs"]["length"], wins[0]["render_frames"]))
        rec = R.slot(sidb, v)["takes"][0]["inputs"].get("window")
        check("the take records its window {f0, f1, render_frames, keep, slice_start}",
              rec == {"f0": wins[0]["f0"], "f1": wins[0]["f1"], "render_frames": wins[0]["render_frames"],
                      "keep": "head", "slice_start": wins[0]["slice_start"]}, rec)
view = R.get(sidb)
check("after generating, no shot is stale", all(not s["stale"] for s in view["slots"]), [s["stale"] for s in view["slots"] if s["stale"]][:2])
entry, body, code = R.cut(sidb)
check("the snapped cut finishes", bool(entry and entry.get("status") == "done"), (code, body, entry and entry.get("log")))
cut_b = R.cut_path(sidb, entry) if entry else None
frames = counted_frames(cut_b) if cut_b else None
check("the cut has exactly 4262 frames", frames == 4262 == FX["total_frames"], frames)
check("the cut record carries bar_snap and the expected frame count", bool(entry and entry.get("bar_snap") and entry.get("frames") == 4262), entry)
cls = frame_colours(cut_b, PAL)
want = np.concatenate([np.full(s["f1"] - s["f0"], i) for i, s in enumerate(SEGS)])
check("every frame is the colour of the shot that owns it", len(cls) == len(want) and bool((cls == want).all()),
      "first mismatch at frame %s" % (int(np.argmax(cls[:len(want)] != want)) if len(cls) >= len(want) else "?"))
chain_snapped, _ = mod._clip_video_chain(0, {"in": 2.75, "len": 2.4, "frames": (66, 124), "w": W, "h": H}, W, H)
chain_plain, _ = mod._clip_video_chain(0, {"in": 2.75, "len": 2.4, "w": W, "h": H}, W, H)
check("a snapped clip is trimmed by frame index, an ordinary clip by float seconds as ever",
      "trim=start_frame=66:end_frame=124," in chain_snapped and "trim=start=2.750000:duration=2.400000," in chain_plain
      and "start_frame" not in chain_plain, (chain_snapped, chain_plain))
first = [int(i) for i in np.where(np.diff(cls) != 0)[0] + 1]
check("every cut frame equals the reference's f0 (33 cuts)", first == [s["f0"] for s in SEGS[1:]], first[:5])
# the per-boundary gate: each cut sits within half a frame of its intended downbeat and nearest to that downbeat
H3_GRID = {"step": 17, "offset": 5, "min": 124, "max": 362, "carried": 0}
BW = beatsnap.windows(DOWN, FX["end"], [dict({k: v for k, v in z.items() if k in ("bars", "end_at", "to_end", "grid")},
                                             id=str(j)) for j, z in enumerate(SEGS)],
                      {"fl2va": H3_GRID, "exact": {"step": 1, "offset": 0, "min": 1, "max": 10000, "carried": 0}})
bad_edge, n_checked = [], 0
for i in range(1, len(SEGS)):
    if BW[i - 1]["edge"] != "bars":
        continue                                      # a silence stop (end_at): no downbeat was intended
    n_checked += 1
    t_edge = DOWN[BW[i - 1]["k"] + BW[i - 1]["bars"]]
    t_cut = first[i - 1] / 24.0
    near = min(range(len(DOWN)), key=lambda j: (abs(DOWN[j] - t_cut), j))
    if abs(t_cut - round(t_edge * 24) / 24.0) > 0.5 / 24 or DOWN[near] != t_edge:
        bad_edge.append(i)
check("per boundary (%d on a downbeat): 0 out of tolerance, 0 misidentified" % n_checked, bad_edge == [] and n_checked >= 28, (bad_edge, n_checked))

NS = os.environ.get("BWF_NORTH_STAR", "/nonexistent/north-star")
RET = "/models/scratch/edrick/round10_the_return/video"
print()
print("(c) M4 (bench/north-star/m4_sync.py) on that cut, against the real song map")
if not (os.path.isfile(NS + "/m4_sync.py") and os.path.isfile(RET + "/songmap/master.song_map.json")):
    note("SKIP: the north-star scorer or the Return's song map is not on this machine")
else:
    planned = ",".join("%.4f" % (s["f0"] / 24.0) for s in SEGS[1:])
    out_json = os.path.join(SCRATCH, "m4.json")
    r = subprocess.run([sys.executable, NS + "/m4_sync.py", cut_b, RET + "/songmap/master.song_map.json",
                        "--planned", planned, "--out", out_json], capture_output=True, text=True, cwd=NS)
    if r.returncode != 0 or not os.path.isfile(out_json):
        check("m4_sync.py ran", False, r.stderr[-600:])
    else:
        m4 = json.load(open(out_json))
        note("M4: %d cuts, median %.1f ms, p90 %.1f ms, planned detected %s" % (
            m4["n_cuts"], m4["median_abs_ms"], m4["p90_abs_ms"], m4.get("planned")))
        check("median <= 13.8 ms (Claude's 10.8 + N=3)", m4["median_abs_ms"] <= 13.8, m4["median_abs_ms"])
        check("p90 <= 42 ms (one frame)", m4["p90_abs_ms"] <= 42.0, m4["p90_abs_ms"])
        check("every planned cut is detected", (m4.get("planned") or {}).get("detected") == 33, m4.get("planned"))

# =====================================================================================================================
# (d)/(j) H3 sings to an IMPORTED master; a window that starts before the file gets leading silence
# =====================================================================================================================
print()
print("(d) H3 fl2va follows an imported master: the uploaded slice starts at master_start + slice_start (tone markers)")
sidd = R.new_seq("sing master")
TONE_AT, START_D = 30.0, 28.0
MASTER_D = wav_bytes(40.0, bursts=(TONE_AT,))
DB_D = regular_downbeats(0.5, 2.4, 40.0)
R.import_master(sidd, MASTER_D)
R.ok(sidd, "set_master_start", start=START_D)
R.ok(sidd, "set_song_map", song_map=north_star_map(MASTER_D, 40.0, DB_D))
d1 = R.shot(sidd, HEAD, length=62)
d2 = R.shot(sidd, HEAD, length=62)
R.ok(sidd, "set_audio_led", on=True)
body, code = R.op(sidd, "set_sing", slot_id=d1, on=True)
check("with snap OFF, E1 still refuses an imported master: 'Pick a song in the sound lane first'",
      *refusal(body, code, "song in the sound lane"))
R.ok(sidd, "set_bar_snap", on=True)
body, code = R.op(sidd, "set_sing", slot_id=d1, on=True)
check("with snap ON, an imported master is a song H3 can sing to", code == 200, (code, body.get("error")))
R.ok(sidd, "set_shot_bars", slot_id=d1, bars=2)
R.ok(sidd, "set_shot_bars", slot_id=d2, bars=1)
v = R.get(sidd)
check("GET shows each shot's effective Follows the song: d1 sings (on), d2 does not (never switched on by snap)",
      [s["follows_song"] for s in v["slots"]] == [True, False], [s.get("follows_song") for s in v["slots"]])
check("and offers Sing along on an imported master", all(s["sing_offer"] for s in v["slots"]))
w1 = R.slot(sidd, d1)["window"]
solid(os.path.join(OUT, "d1.mp4"), w1["render_frames"], (10, 120, 200))
body, code = R.make(sidd, d1, "d1.mp4")
check("generate on the singing shot succeeds", code == 200, body)
wav_path, graph = R.uploaded_wav()
check("the graph has one LoadAudio and a TrimAudioDuration from 0 of render_frames/24 s",
      wav_path is not None and any(n.get("class_type") == "TrimAudioDuration" and n["inputs"]["start_index"] == 0.0
                                   and abs(n["inputs"]["duration"] - w1["render_frames"] / 24.0) < 1e-6
                                   for n in graph.values()), [n for n in graph.values() if n.get("class_type") == "TrimAudioDuration"])
x = decode_mono(wav_path) if wav_path and os.path.isfile(wav_path) else np.zeros(1)
onset = burst_onset(x)
expect = TONE_AT - (START_D + w1["slice_start"])
check("the uploaded wav is render_frames/24 + 1/24 long (%.3f s)" % (w1["render_frames"] / 24 + 1 / 24),
      abs(len(x) / 8000.0 - (w1["render_frames"] / 24 + 1 / 24)) < 0.06, len(x) / 8000.0)
check("the tone marker sits at %.3f s +-10 ms (master_start + slice_start)" % expect,
      onset is not None and abs(onset - expect) <= 0.010, (onset, expect))
tk = R.slot(sidd, d1)["takes"][0]["inputs"]
check("the take recorded no E1 'sing' (snap places it by the master), and records its window",
      "sing" not in tk and tk.get("window", {}).get("f0") == w1["f0"], tk)
body, code = R.make(sidd, d2, "d1.mp4")
lp = R.last_prompt()
check("the shot that does NOT follow gets no LoadAudio", not any(n.get("class_type") == "LoadAudio" for n in lp.values()))

print()
print("(j) a window that starts before the file: leading silence, the tone marker at abs(slice_start)")
sidj = R.new_seq("leading silence")
TONE_J = 0.5
MASTER_J = wav_bytes(16.0, bursts=(TONE_J,))
DB_J = regular_downbeats(0.0, 2.4, 16.0)
R.import_master(sidj, MASTER_J)
R.ok(sidj, "set_song_map", song_map=north_star_map(MASTER_J, 16.0, DB_J))
j1 = R.shot(sidj, HEAD, length=62)
j2 = R.shot(sidj, CONT, length=62)
R.ok(sidj, "patch", **{"from": j1, "to": j2, "field": JACK})
R.ok(sidj, "set_audio_led", on=True)
R.ok(sidj, "set_bar_snap", on=True)
R.ok(sidj, "set_sing", slot_id=j1, on=True)
wj = R.slot(sidj, j1)["window"]
check("the shot a continue is cabled from keeps its tail and its slice starts before 0",
      wj["keep"] == "tail" and wj["slice_start"] < 0, wj)
solid(os.path.join(OUT, "j1.mp4"), wj["render_frames"], (10, 120, 200))
body, code = R.make(sidj, j1, "j1.mp4")
wav_j, _ = R.uploaded_wav()
xj = decode_mono(wav_j) if wav_j else np.zeros(1)
onset_j = burst_onset(xj)
lead = -wj["slice_start"]
check("the tone marker sits at abs(slice_start) + its place in the master (%.3f s) +-10 ms" % (lead + TONE_J),
      onset_j is not None and abs(onset_j - (lead + TONE_J)) <= 0.010, (onset_j, lead + TONE_J))
check("and the lead-in before it is silent", float(np.abs(xj[:int(max(0.0, lead - 0.05) * 8000)]).max()) < 0.01 if len(xj) else False)
check("the slice runs render_frames/24 + 1/24", abs(len(xj) / 8000.0 - (wj["render_frames"] / 24 + 1 / 24)) < 0.06, len(xj) / 8000.0)

# =====================================================================================================================
# (e) a continue chain: the upstream keeps its last frames
# =====================================================================================================================
print()
print("(e) a continue chain: the shot a continue is cabled from is tail-anchored and the cut keeps its last frames")
sive = R.new_seq("continue chain")
MASTER_E = wav_bytes(16.0)
DB_E = regular_downbeats(0.0, 2.4, 16.0)
R.import_master(sive, MASTER_E)
R.ok(sive, "set_song_map", song_map=north_star_map(MASTER_E, 16.0, DB_E))
e1 = R.shot(sive, HEAD, length=62)
e2 = R.shot(sive, CONT, length=62)
e3 = R.shot(sive, CONT, length=62)
R.ok(sive, "patch", **{"from": e1, "to": e2, "field": JACK})
R.ok(sive, "set_audio_led", on=True)
R.ok(sive, "set_bar_snap", on=True)
R.ok(sive, "set_shot_bars", slot_id=e1, bars=1)
R.ok(sive, "set_shot_bars", slot_id=e2, bars=1)
v = R.get(sive)
we1, we2, we3 = [s["window"] for s in v["slots"]]
E1_ = we1["render_frames"] - (we1["f1"] - we1["f0"])
check("the upstream is tail-anchored with trim_in = E/24 and a slice pulled E/24 earlier",
      we1["keep"] == "tail" and E1_ > 0 and abs(we1["trim_in"] - E1_ / 24.0) < 1e-6
      and abs(we1["slice_start"] - (we1["start"] - E1_ / 24.0)) < 1e-6, (we1, E1_))
check("the cabled continue shot is head-anchored and its render is W + 22 frames, rounded up to 17n+5 (min 124)",
      we2["keep"] == "head" and we2["render_frames"] == max(124, 17 * -(-((we2["f1"] - we2["f0"] + 22) - 5) // 17) + 5), we2)
check("the uncabled shot after it carries nothing (a continue chain head needs no re-rendered context)",
      we3["render_frames"] >= 124 and we3["keep"] == "head")
RED, GREEN, BLUE = (200, 0, 0), (0, 200, 0), (0, 0, 200)
two_part(os.path.join(OUT, "e1.mp4"), E1_, RED, we1["f1"] - we1["f0"], GREEN)
solid(os.path.join(OUT, "e2.mp4"), we2["render_frames"] - 22, BLUE)
solid(os.path.join(OUT, "e3.mp4"), we3["render_frames"], (200, 200, 0))
R.make(sive, e1, "e1.mp4")
R.make(sive, e2, "e2.mp4")
R.make(sive, e3, "e3.mp4")
entry, body, code = R.cut(sive)
check("the chain cuts", bool(entry and entry.get("status") == "done"), (code, body, entry and entry.get("log")))
if entry and entry.get("status") == "done":
    palette_e = [RED, GREEN, BLUE, (200, 200, 0)]
    ce = frame_colours(R.cut_path(sive, entry), palette_e)
    n1 = we1["f1"] - we1["f0"]
    check("the cut keeps the LAST %d frames of the upstream (all green, none of its red head)" % n1,
          bool((ce[:n1] == 1).all()), np.bincount(ce[:n1], minlength=4).tolist())
    check("the next shot follows on (blue), and the whole cut is sum(W) frames",
          bool((ce[n1:n1 + (we2["f1"] - we2["f0"])] == 2).all()) and len(ce) == we3["f1"], len(ce))

# =====================================================================================================================
# (f)/(h)/(i) reflow, stale takes at cut time, CFR
# =====================================================================================================================
print()
print("(f) moving one shot's bars makes the later made takes stale, and the op says how many")
sidf = R.new_seq("reflow")
MASTER_F = wav_bytes(22.0)
DB_F = regular_downbeats(0.5, 2.4, 22.0)
R.import_master(sidf, MASTER_F)
R.ok(sidf, "set_song_map", song_map=north_star_map(MASTER_F, 22.0, DB_F))
f1, f2, f3, f4 = [R.shot(sidf, HEAD, length=62) for _ in range(4)]
R.ok(sidf, "set_audio_led", on=True)
R.ok(sidf, "set_bar_snap", on=True)
for v_ in (f1, f2, f3):
    R.ok(sidf, "set_shot_bars", slot_id=v_, bars=1)
for k, v_ in enumerate((f1, f2, f3, f4)):
    wk = R.slot(sidf, v_)["window"]
    solid(os.path.join(OUT, "f%d.mp4" % k), wk["render_frames"], palette(8)[k + 1])
    R.make(sidf, v_, "f%d.mp4" % k)
check("all four made shots start fresh", all(not s["stale"] for s in R.get(sidf)["slots"]))
body = R.ok(sidf, "set_shot_bars", slot_id=f2, bars=2)
st = {s["id"]: s["stale"] for s in body["slots"]}
check("the changed shot and every shot after it are stale: 'the beat grid moved this shot'",
      all(st[x] == ["the beat grid moved this shot"] for x in (f2, f3, f4)), st)
check("the shot before it is not", st[f1] == [], st)
check("the op returns the count (3) and the shot ids that just went stale",
      body.get("reflow_stale_count") == 3 and body.get("reflow_stale") == sorted([f2, f3, f4]),
      (body.get("reflow_stale_count"), body.get("reflow_stale")))
body = R.ok(sidf, "set_shot_bars", slot_id=f2, bars=1)
check("undoing the change makes them fresh again", all(not s["stale"] for s in body["slots"]) and body["reflow_stale_count"] == 0,
      [s["stale"] for s in body["slots"]])

print()
print("(h) a snapped cut refuses a picked take made for another window, naming the shot; adopted takes pass")
R.ok(sidf, "set_shot_bars", slot_id=f2, bars=2)
entry, body, code = R.cut(sidf)
ok_, det = refusal(body, code, f2, "beat grid now puts it at")
check("the cut is refused naming the stale shot and both windows (%s)" % det[1][:90], ok_ and "was made for" in det[1], det)
check("and the sentence tells the person what to do", "Make it again" in det[1] and "undo the bar change" in det[1], det[1])
for k, v_ in enumerate((f2, f3, f4)):
    wk = R.slot(sidf, v_)["window"]
    solid(os.path.join(OUT, "f%dr.mp4" % k), wk["render_frames"], palette(8)[k + 2])
    R.make(sidf, v_, "f%dr.mp4" % k)
entry, body, code = R.cut(sidf)
check("after re-generating the stale shots the cut goes through", bool(entry and entry.get("status") == "done"),
      (code, body, entry and entry.get("log")))
# a take made before snap (no window): refused; an adopted take with enough frames and no singing: accepted
sidh = R.new_seq("adopted")
MASTER_H = wav_bytes(30.0)
DB_H = regular_downbeats(0.5, 2.4, 30.0)
R.import_master(sidh, MASTER_H)
R.ok(sidh, "set_song_map", song_map=north_star_map(MASTER_H, 30.0, DB_H))
h1, h2 = R.shot(sidh, "ltx", length=97), R.shot(sidh, "ltx", length=97)
R.ok(sidh, "set_audio_led", on=True)
R.ok(sidh, "set_lipsync", slot_id=h2, on=False)               # an LTX shot follows the song unless unticked
solid(os.path.join(OUT, "h_pre.mp4"), 97, (200, 0, 0))
R.make(sidh, h1, "h_pre.mp4", mode="ltx")                  # made before snap: no window recorded
R.ok(sidh, "set_bar_snap", on=True)
R.ok(sidh, "set_shot_bars", slot_id=h1, bars=1)
check("a take made before snap shows as stale in GET", R.slot(sidh, h1)["stale"] == ["made before Snap to beats"], R.slot(sidh, h1)["stale"])
body, code = R.m.seq_cut_start({"id": sidh})
check("a snapped cut with an unpicked shot is refused, naming it (the later shots would slide off the beats)",
      *refusal(body, code, h2, "no picked take"))
wh = R.get(sidh)["slots"][0]["window"]
solid(os.path.join(OUT, "h_adopt.mp4"), wh["f1"] - wh["f0"] + 5, (0, 200, 0))
R.done_job("adopt_job_h2", "h_adopt.mp4", "video", "ltx", "video")
R.ok(sidh, "adopt_take", slot_id=h2, job_id="adopt_job_h2")
w2 = R.get(sidh)["slots"][1]["window"]
solid(os.path.join(OUT, "h_adopt2.mp4"), max(w2["f1"] - w2["f0"], 5), (0, 200, 0))
R.done_job("adopt_job_h2b", "h_adopt2.mp4", "video", "ltx", "video")
R.ok(sidh, "adopt_take", slot_id=h2, job_id="adopt_job_h2b")
entry, body, code = R.cut(sidh)
ok_, det = refusal(body, code, h1, "before Snap to beats")
check("the pre-snap take (shot %s) is refused: 'made before Snap to beats'" % h1, ok_, det)
solid(os.path.join(OUT, "h_re.mp4"), wh["render_frames"], (200, 0, 0))
R.make(sidh, h1, "h_re.mp4", mode="ltx")
entry, body, code = R.cut(sidh)
check("with shot 1 re-made, the adopted take (no window, enough frames, not singing) is accepted",
      bool(entry and entry.get("status") == "done"), (code, body, entry and entry.get("log")))
solid(os.path.join(OUT, "h_short.mp4"), 3, (0, 200, 0))
R.done_job("adopt_job_h2c", "h_short.mp4", "video", "ltx", "video")
R.ok(sidh, "adopt_take", slot_id=h2, job_id="adopt_job_h2c")
entry, body, code = R.cut(sidh)
check("an adopted take with fewer frames than its window is refused", *refusal(body, code, h2, "frames"))

print()
print("(i) CFR: a 25 fps take and a variable-rate take are refused when snapped, and accepted unsnapped")
sidi = R.new_seq("cfr")
MASTER_I = wav_bytes(14.0)
DB_I = regular_downbeats(0.5, 2.4, 14.0)
R.import_master(sidi, MASTER_I)
R.ok(sidi, "set_song_map", song_map=north_star_map(MASTER_I, 14.0, DB_I))
i1 = R.shot(sidi, HEAD, length=62)
R.ok(sidi, "set_audio_led", on=True)
R.ok(sidi, "set_bar_snap", on=True)
R.ok(sidi, "set_shot_bars", slot_id=i1, bars=1)
wi = R.slot(sidi, i1)["window"]
solid(os.path.join(OUT, "i25.mp4"), wi["render_frames"], (200, 0, 0), fps=25)
R.make(sidi, i1, "i25.mp4")
entry, body, code = R.cut(sidi)
check("a 25 fps take is refused in snapped mode naming the shot and 'steady 24 frames a second'",
      *refusal(body, code, i1, "steady 24 frames a second"))
solid(os.path.join(OUT, "i2398.mp4"), wi["render_frames"], (200, 0, 0), fps="24000/1001")
R.make(sidi, i1, "i2398.mp4")
entry, body, code = R.cut(sidi)
check("a 23.976 fps take (its frame count nearly matches 24 x duration) is refused on its frame rate alone",
      *refusal(body, code, i1, "steady 24 frames a second"))
# variable rate: 12 fps then 24 fps stitched without resampling
vfr = os.path.join(OUT, "ivfr.mp4")
for _vfr_flag in (["-fps_mode", "vfr"], ["-vsync", "vfr"]):      # ffmpeg 5.1+ has -fps_mode, 4.4 only -vsync
    try:
        ff(["-f", "lavfi", "-i", "color=c=red:size=%dx%d:rate=12:duration=4" % (W, H), "-f", "lavfi", "-i",
            "color=c=blue:size=%dx%d:rate=24:duration=4" % (W, H),
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]"] + _vfr_flag +
           ["-c:v", "libx264", "-pix_fmt", "yuv420p", vfr])
        break
    except RuntimeError:
        if _vfr_flag[0] == "-vsync":
            raise
pv = ffprobe_json(vfr, ["-show_streams"])["streams"][0]
note("synthetic VFR clip: r_frame_rate %s avg_frame_rate %s" % (pv.get("r_frame_rate"), pv.get("avg_frame_rate")))
R.make(sidi, i1, "ivfr.mp4")
entry, body, code = R.cut(sidi)
check("a variable-rate take is refused in snapped mode", *refusal(body, code, i1, "steady"))
R.ok(sidi, "set_bar_snap", on=False)
entry, body, code = R.cut(sidi)
check("unsnapped, the same take is accepted exactly as today", bool(entry and entry.get("status") == "done"), (code, body, entry and entry.get("log")))
R.make(sidi, i1, "i25.mp4")
entry, body, code = R.cut(sidi)
check("unsnapped, the 25 fps take is accepted too", bool(entry and entry.get("status") == "done"), (code, body, entry and entry.get("log")))

# =====================================================================================================================
# (m) E1 precedence
# =====================================================================================================================
print()
print("(m) E1 precedence: snap on leaves sing.on=false shots silent and keeps sing.start; snap off restores E1 byte for byte")
sidm = R.new_seq("e1 precedence")
song_slot = R.ok(sidm, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "a song"})["added_slot_id"]
SONG_BYTES = wav_bytes(16.0, bursts=(3.0,))
open(os.path.join(OUT, "m_song.wav"), "wb").write(SONG_BYTES)
R.done_job("m_song_job", "m_song.wav", "audio", "sfx", "audio")
mod.SONG_SECONDS["m_song_job"] = 16.0
mod.seq_add_take(sidm, song_slot, "m_song_job")
R.ok(sidm, "pick_take", slot_id=song_slot, job_id="m_song_job")
mod._cut_ensure_take_file(sidm, song_slot, "m_song_job")           # the take is local, so the master has bytes to hash
sing_on, sing_off = R.shot(sidm, HEAD, length=124), R.shot(sidm, HEAD, length=124)
R.ok(sidm, "set_sing", slot_id=sing_on, on=True)
R.ok(sidm, "set_sing", slot_id=sing_on, start=3.5)
R.ok(sidm, "set_sing", slot_id=sing_off, start=8.0)
R.ok(sidm, "set_audio_led", on=True)
span_before = [R.slot(sidm, v_).get("sing_span") for v_ in (sing_on, sing_off)]
solid(os.path.join(OUT, "m_a.mp4"), 124, (0, 100, 200))
R.make(sidm, sing_on, "m_a.mp4", pick=False)
lp0 = R.last_prompt()
trim0 = [n["inputs"] for n in lp0.values() if n.get("class_type") == "TrimAudioDuration"]
check("setup: with snap off, E1 sings the shot from its typed start (3.5 s)",
      span_before[0] and span_before[0]["start"] == 3.5 and trim0 and trim0[0]["start_index"] == 3.5, (span_before, trim0))
R.ok(sidm, "set_song_map", song_map=north_star_map(SONG_BYTES, 16.0, regular_downbeats(0.5, 2.4, 16.0),
                                                   sha=hashlib.sha256(SONG_BYTES).hexdigest()))
R.ok(sidm, "set_bar_snap", on=True)
v = R.get(sidm)
check("snap on: E1's computed spans are gone (the master slice places every following shot)",
      all(s.get("sing_span") is None for s in v["slots"] if s["lane"] == "video"), [s.get("sing_span") for s in v["slots"]])
check("sing.start and sing.on are kept as stored", R.slot(sidm, sing_on)["sing"] == {"on": True, "start": 3.5}
      and R.slot(sidm, sing_off)["sing"] == {"on": False, "start": 8.0}, (R.slot(sidm, sing_on).get("sing"), R.slot(sidm, sing_off).get("sing")))
check("follows_song: sing.on true -> on; sing.on false -> off (snap never switches singing on)",
      [R.slot(sidm, x)["follows_song"] for x in (sing_on, sing_off)] == [True, False])
R.ok(sidm, "set_shot_bars", slot_id=sing_on, bars=1)
wm = R.slot(sidm, sing_on)["window"]
solid(os.path.join(OUT, "m_b.mp4"), wm["render_frames"], (0, 100, 200))
R.make(sidm, sing_on, "m_b.mp4", pick=False)
lp1 = R.last_prompt()
trim1 = [n["inputs"] for n in lp1.values() if n.get("class_type") == "TrimAudioDuration"]
check("snap on: the singing shot's audio is its master slice from 0 (not E1's typed start)",
      trim1 and trim1[0]["start_index"] == 0.0 and abs(trim1[0]["duration"] - wm["render_frames"] / 24.0) < 1e-6, trim1)
solid(os.path.join(OUT, "m_c.mp4"), R.slot(sidm, sing_off)["window"]["render_frames"], (0, 100, 200))
R.make(sidm, sing_off, "m_c.mp4", pick=False)
check("snap on: the sing.on=false shot gets no sound clip at all",
      not any(n.get("class_type") == "LoadAudio" for n in R.last_prompt().values()))
R.ok(sidm, "set_bar_snap", on=False)
span_after = [R.slot(sidm, v_).get("sing_span") for v_ in (sing_on, sing_off)]
check("snap off: E1's spans are exactly what they were before", span_after == span_before, (span_after, span_before))
check("snap off: no bar_snap key is left", "bar_snap" not in R.stored(sidm))
R.make(sidm, sing_on, "m_a.mp4", pick=False)
trim2 = [n["inputs"] for n in R.last_prompt().values() if n.get("class_type") == "TrimAudioDuration"]
check("snap off: the next generate uses E1 timing byte for byte (same TrimAudioDuration as before)", trim2 == trim0, (trim2, trim0))
take_in = R.slot(sidm, sing_on)["takes"][-1]["inputs"]
check("snap off: the new take records inputs.sing and no window", "sing" in take_in and "window" not in take_in, take_in)

# =====================================================================================================================
# (g) refusal sentences
# =====================================================================================================================
print()
print("(g) refusal sentences")
sg = R.new_seq("refusals")
g1 = R.shot(sg, HEAD, length=len_for_bars(6))
body, code = R.op(sg, "set_bar_snap", on=True)
check("snap on without audio-led: tells you to turn audio-led on", *refusal(body, code, "audio-led"))
R.ok(sg, "set_audio_led", on=True)
body, code = R.op(sg, "set_bar_snap", on=True)
check("snap on without a song map: 'Find the beats of the master sound first'", *refusal(body, code, "find the beats"))
body, code = R.op(sg, "set_bar_snap", on="yes")
check("a non-bool is refused", *refusal(body, code, "on or off"))
body, code = R.op(sg, "set_shot_bars", slot_id=g1, bars=2)
check("set_shot_bars with snap off: 'Turn on Snap to beats first'", *refusal(body, code, "snap to beats"))
MASTER_G = wav_bytes(40.0)
DB_G = regular_downbeats(0.5, 2.4, 40.0)
R.import_master(sg, MASTER_G)
R.ok(sg, "set_song_map", song_map=north_star_map(MASTER_G, 40.0, DB_G))
g2 = R.shot(sg, HEAD, length=len_for_bars(6))
g3 = R.shot(sg, HEAD, length=62)
loop = R.shot(sg, "ltx_loop", length=97)
body, code = R.op(sg, "set_bar_snap", on=True)
check("a video mode with no frame grid cannot be snapped, and the sentence names the shot",
      *refusal(body, code, loop, "frame grid"))
R.ok(sg, "remove_slot", slot_id=loop)
R.ok(sg, "set_bar_snap", on=True)
snap_n = len(DB_G) - 1
body, code = R.op(sg, "set_shot_bars", slot_id=g1, bars=snap_n + 5)
check("more bars than the song has: 'the song has N bars left'", *refusal(body, code, "bars left"))
body, code = R.op(sg, "set_shot_bars", slot_id=g1, bars=8)
check("more bars than the model can render: names the shot and the most that fit",
      *refusal(body, code, g1, "at most", "bars fit"))
body, code = R.op(sg, "set_shot_bars", slot_id=g1, bars=0)
check("zero bars are refused", *refusal(body, code, "whole number"))
body, code = R.op(sg, "set_shot_bars", slot_id=g1, bars=2, to_end=True)
check("two settings at once are refused", *refusal(body, code, "exactly one"))
body, code = R.op(sg, "set_shot_bars", slot_id=g1, end_at=-3)
check("a negative end_at is refused", *refusal(body, code, "end_at"))
body, code = R.op(sg, "set_shot_bars", slot_id=g1, to_end=True)
check("to_end on a shot with another after it is refused", *refusal(body, code, g1))
body, code = R.op(sg, "set_shot_bars", slot_id="nope", bars=1)
check("an unknown shot is refused", code == 400)
snd = R.ok(sg, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "x"})["added_slot_id"]
body, code = R.op(sg, "set_shot_bars", slot_id=snd, bars=1)
check("a sound shot has no bars", *refusal(body, code, "video shot"))
# set_trim on a snapped shot: only the in-point may move
body, code = R.op(sg, "set_trim", slot_id=g2, trim={"in": 0.0, "len": 1.0})
check("a trim that changes a snapped shot's length is refused", *refusal(body, code, "only the in-point"))
wg = R.slot(sg, g2)["window"]
body, code = R.op(sg, "set_trim", slot_id=g2, trim={"in": 0.5, "len": wg["len"]})
check("a slip (same length, new in-point) on a shot that does not follow the song is accepted", code == 200, (code, body.get("error")))
body, code = R.op(sg, "set_trim", slot_id=g2, trim={"in": None, "len": wg["len"]})
check("a tail-keep trim is refused on a snapped shot", *refusal(body, code, "only the in-point"))

print()
print("too few shots for the song: the last shot is cut back to what its model can render, with a warning")
sidc = R.new_seq("too few shots")
MASTER_C = wav_bytes(40.0)
DB_C = regular_downbeats(0.5, 2.4, 40.0)
R.import_master(sidc, MASTER_C)
R.ok(sidc, "set_song_map", song_map=north_star_map(MASTER_C, 40.0, DB_C))
c1, c2 = R.shot(sidc, HEAD, length=62), R.shot(sidc, HEAD, length=62)
R.ok(sidc, "set_audio_led", on=True)
body, code = R.op(sidc, "set_bar_snap", on=True)
check("Snap to beats turns on although two 1-bar shots leave most of the 40 s song uncovered", code == 200, (code, body.get("error")))
v = R.get(sidc)
wc = [x["window"] for x in v["slots"]]
rep = v["snap_report"]
fit_c = beatsnap.fit_max_bars(DB_C, wc[1]["f0"], 1, H3_GRID)
check("the last shot is clamped to the most bars an H3 render holds (%d), not refused" % fit_c,
      wc[1]["bars"] == fit_c and wc[1]["render_frames"] <= 362 and wc[1]["bars"] > 1, wc[1])
check("unplaced_bars counts the song bars after the last window (%d of %d)" % (rep["unplaced_bars"], rep["song_bars"]),
      rep["song_bars"] == len(DB_C) - 1 and rep["unplaced_bars"] == sum(1 for d in DB_C[:-1] if d >= wc[1]["f1"] / 24.0 - 1e-6)
      and rep["unplaced_bars"] > 0, rep)
check("the warning names the shot, the bar count and where the cut ends",
      len(rep["warnings"]) == 1 and c2 in rep["warnings"][0] and ("%d bars" % rep["unplaced_bars"]) in rep["warnings"][0]
      and "the cut ends at" in rep["warnings"][0], rep["warnings"])
for k, x in enumerate((c1, c2)):
    solid(os.path.join(OUT, "c%d.mp4" % k), wc[k]["render_frames"], palette(4)[k + 1])
    R.make(sidc, x, "c%d.mp4" % k)
entry, body, code = R.cut(sidc)
check("the cut ends at the last shot: its length is the sum of the windows",
      bool(entry and entry.get("status") == "done") and counted_frames(R.cut_path(sidc, entry)) == wc[1]["f1"],
      (code, body, entry and entry.get("log")))
body, code = R.op(sidc, "set_shot_bars", slot_id=c2, bars=fit_c + 1)
check("an EXPLICIT bar count that cannot fit is still refused", *refusal(body, code, c2, "at most", "bars fit"))

# =====================================================================================================================
# (l) analyze identity (a stubbed process lane)
# =====================================================================================================================
print()
print("(l) analyze: the beats attach only to the song they were found for")
STUB = {"n": 0}
orig_generate, orig_lane, orig_seq_op = mod.generate, mod._grid_lane, mod.seq_op


def stub_generate(p):
    STUB["n"] += 1
    jid = "gridstub%02d" % STUB["n"]
    with mod.JOBS_LOCK:
        mod.JOBS[jid] = {"id": jid, "lane": "cpu", "kind": "producer", "mode": "grid", "status": "running", "outputs": []}
    STUB.setdefault("requests", []).append(dict(p))
    return {"ok": True, "job": {"id": jid}}, 200


def finish_grid(jid, downbeats, seconds, status="done", error=None):
    d = os.path.join(mod.LOCAL_OUTPUTS_DIR, jid)
    os.makedirs(d, exist_ok=True)
    beats = []
    for a, b in zip(downbeats, downbeats[1:]):
        beats += [a + (b - a) * k / 4 for k in range(4)]
    beats.append(downbeats[-1])
    json.dump({"beats": beats, "downbeats": downbeats, "bars": [], "meter": {"beats_per_bar": 4}, "bpm_median": 97.5,
               "source": {"duration_s": seconds}}, open(os.path.join(d, "grid.json"), "w"))
    with mod.JOBS_LOCK:
        mod.JOBS[jid]["status"] = status
        if error:
            mod.JOBS[jid]["error"] = error


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    return None


mod.generate, mod._grid_lane = stub_generate, lambda lane_id=None: {"id": "cpu", "name": "CPU lane", "kind": "process"}
mod.ANALYZE_POLL_S = 0.05
try:
    sl = R.new_seq("analyze")
    MASTER_L1 = wav_bytes(40.0, bursts=(5.0,))
    MASTER_L2 = wav_bytes(40.0, bursts=(6.0,))
    DB_L = regular_downbeats(0.4, 2.4, 40.0)
    body, code = mod.seq_analyze({"id": sl})
    check("analyze with no master is refused with a sentence", *refusal(body, code, "master"))
    R.import_master(sl, MASTER_L1)
    body, code = mod.seq_analyze({"id": sl, "beats_per_bar": 3, "first_downbeat": 0.4})
    check("analyze answers {ok, job_id}", code == 200 and body.get("ok") and body.get("job_id") == "gridstub01", (code, body))
    check("it asked the process lane for the producer's grid mode with the meter and the staged master",
          STUB["requests"][0].get("kind") == "producer" and STUB["requests"][0].get("mode") == "grid"
          and STUB["requests"][0].get("beats_per_bar") == 3 and STUB["requests"][0].get("first_downbeat") == 0.4
          and os.path.isfile(os.path.join(mod.UPLOADS_DIR, "cpu", STUB["requests"][0]["source_audio_name"])), STUB["requests"][0])
    job = R.stored(sl).get("song_map_job") or {}
    check("GET shows song_map_job with the source identity snapshotted at the start",
          R.get(sl)["song_map_job"]["job_id"] == "gridstub01" and job["status"] == "running"
          and job["source"]["sha256"] == hashlib.sha256(MASTER_L1).hexdigest() and job["source"]["kind"] == "file", job)
    check("a job whose attach thread is alive in this process reads 'running' (no restart sentence)",
          R.get(sl)["song_map_job"]["status"] == "running" and "error" not in R.get(sl)["song_map_job"], R.get(sl)["song_map_job"])
    R.import_master(sl, MASTER_L2)                      # the song changes while the (stubbed) job runs
    finish_grid("gridstub01", DB_L, 40.0)
    err = wait_for(lambda: (R.stored(sl).get("song_map_job") or {}).get("error"))
    check("the attach is dropped with 'the song changed while the beats were being found'",
          err == "the song changed while the beats were being found", err)
    check("and seq.song_map stays absent", "song_map" not in R.stored(sl))

    # a second analyze supersedes the first job's attach
    sl2 = R.new_seq("analyze twice")
    R.import_master(sl2, MASTER_L1)
    b1, _ = mod.seq_analyze({"id": sl2})
    b2, _ = mod.seq_analyze({"id": sl2})
    check("a second analyze replaces song_map_job", R.stored(sl2)["song_map_job"]["job_id"] == b2["job_id"] != b1["job_id"])
    finish_grid(b1["job_id"], regular_downbeats(0.9, 2.4, 40.0), 40.0)
    time.sleep(0.6)
    check("the first job finishing late attaches nothing", "song_map" not in R.stored(sl2)
          and (R.stored(sl2)["song_map_job"] or {}).get("job_id") == b2["job_id"] and "error" not in R.stored(sl2)["song_map_job"])
    finish_grid(b2["job_id"], DB_L, 40.0)
    got = wait_for(lambda: R.stored(sl2).get("song_map"))
    check("the current job attaches, from grid:<job id>, on the master's file clock",
          bool(got) and got["from"] == "grid:" + b2["job_id"] and got["downbeats"] == DB_L and got["beats_per_bar"] == 4
          and got["source"]["sha256"] == hashlib.sha256(MASTER_L1).hexdigest() and got["source"]["seconds"] == 40.0, got and got.get("from"))
    check("song_map_job is cleared once the map lands", "song_map_job" not in R.stored(sl2))

    # a 409 during the attach refreshes the rev and keeps the identity
    sl3 = R.new_seq("analyze 409")
    R.import_master(sl3, MASTER_L1)
    b3, _ = mod.seq_analyze({"id": sl3})
    calls = []

    def bumping_seq_op(p):
        calls.append(dict(p))
        if p.get("op") == "set_song_map" and len(calls) == 1:
            orig_seq_op(dict(id=p["id"], rev=p["rev"], op="set_title", title="moved under the attach"))
        return orig_seq_op(p)

    mod.seq_op = bumping_seq_op
    finish_grid(b3["job_id"], DB_L, 40.0)
    got = wait_for(lambda: R.stored(sl3).get("song_map"))
    mod.seq_op = orig_seq_op
    check("the attach got a 409, retried with a new rev, and landed",
          bool(got) and len(calls) >= 2 and calls[1]["rev"] > calls[0]["rev"], [c.get("rev") for c in calls])
    check("the retry carried the same job id (the identity is the snapshot's, not re-read)",
          all(c.get("grid_job") == b3["job_id"] and set(c) == {"id", "rev", "op", "grid_job"} for c in calls[1:]), calls[-1])

    # a failed job and a map that does not fit
    sl4 = R.new_seq("analyze fails")
    R.import_master(sl4, MASTER_L1)
    b4, _ = mod.seq_analyze({"id": sl4})
    finish_grid(b4["job_id"], DB_L, 40.0, status="error", error="beat_this is not installed")
    err = wait_for(lambda: (R.stored(sl4).get("song_map_job") or {}).get("error"))
    check("a failed job records its sentence on song_map_job", err == "beat_this is not installed", err)
    body, code = R.op(sl4, "set_song_map", grid_job="gridstub99")
    check("set_song_map {grid_job} for a job this sequence did not start is refused", *refusal(body, code, "not the one"))
finally:
    mod.generate, mod._grid_lane, mod.seq_op = orig_generate, orig_lane, orig_seq_op

# =====================================================================================================================
# (o) a job stored as running whose attach thread died with the last process reads as an error, in GET only
# =====================================================================================================================
print()
print("(o) a beat-finding run that a restart killed is shown as an error, never as spinning forever")
so = R.new_seq("restarted")
path_o = os.path.join(mod.SEQ_DIR, so + ".json")
doc = json.load(open(path_o))
doc["song_map_job"] = {"job_id": "gridgone01", "source": {"kind": "file", "ref": "x", "sha256": "0" * 64, "seconds": 40.0},
                       "master_start": 0.0, "lane": "cpu", "status": "running", "started": time.time()}
json.dump(doc, open(path_o, "w"))
got = R.get(so)["song_map_job"]
check("GET shows status error with the restart sentence", got["status"] == "error" and got["error"] == mod.ANALYZE_RESTARTED
      and got["error"] == "BWF restarted while the beats were being found. Find them again.", got)
check("the job id is still the stored one", got["job_id"] == "gridgone01", got)
check("storage is untouched (still running, no error key)",
      R.stored(so)["song_map_job"]["status"] == "running" and "error" not in R.stored(so)["song_map_job"], R.stored(so)["song_map_job"])
check("a sequence with no song_map_job has no such key", "song_map_job" not in R.get(R.new_seq("never analysed")))
doc["song_map_job"].update(status="error", error="beat_this is not installed")
json.dump(doc, open(path_o, "w"))
check("a stored error is passed through unchanged", R.get(so)["song_map_job"]["error"] == "beat_this is not installed",
      R.get(so)["song_map_job"])

# a run this process started never reads as "restarted"; a thread that cannot start, or dies oddly, ends as an error
print()
print("(o2) started here: never 'restarted'; failures to start or to attach end as errors")
import types
mod.generate, mod._grid_lane = stub_generate, lambda lane_id=None: {"id": "cpu", "name": "CPU lane", "kind": "process"}
orig_run, orig_threading = mod._analyze_attach_run, mod.threading
try:
    sp = R.new_seq("started here")
    R.import_master(sp, wav_bytes(40.0, bursts=(5.0,)))
    mod._analyze_attach_run = lambda sid, jid: None          # the thread ends at once; the stored job is still "running"
    b, code = mod.seq_analyze({"id": sp})
    time.sleep(0.3)
    got = R.get(sp)["song_map_job"]
    check("a job this process started, whose thread has ended, still reads running (not restarted)",
          code == 200 and got["status"] == "running" and "error" not in got, got)
    finish_grid(b["job_id"], regular_downbeats(0.4, 2.4, 40.0), 40.0)
    mod._analyze_attach_run = orig_run

    class NoThreads(object):
        def __getattr__(self, name):
            return getattr(orig_threading, name)

        @staticmethod
        def Thread(*a, **k):
            raise RuntimeError("can't start new thread")
    sq = R.new_seq("no thread")
    R.import_master(sq, wav_bytes(40.0, bursts=(5.0,)))
    mod.threading = NoThreads()
    try:
        body, code = mod.seq_analyze({"id": sq})
    finally:
        mod.threading = orig_threading
    check("a thread that cannot start answers 500 with the sentence",
          code == 500 and body.get("error") == "Finding the beats could not start.", (code, body))
    job = R.get(sq)["song_map_job"]
    check("and the job is stored as an error with that sentence, not left running",
          job["status"] == "error" and job["error"] == "Finding the beats could not start.", job)

    def boom(sid, jid):
        raise OSError("disk went away")
    mod._analyze_attach_run = boom
    sr = R.new_seq("attach raises")
    R.import_master(sr, wav_bytes(40.0, bursts=(5.0,)))
    mod.seq_analyze({"id": sr})
    err = wait_for(lambda: (R.stored(sr).get("song_map_job") or {}).get("error"))
    check("an attach that raises a non-ValueError ends the job as an error, not running",
          err == "finding the beats stopped with an error" and R.stored(sr)["song_map_job"]["status"] == "error", err)
finally:
    mod.generate, mod._grid_lane = orig_generate, orig_lane
    mod._analyze_attach_run, mod.threading = orig_run, orig_threading

# =====================================================================================================================
# (p) a data pack's frame_grid reaches the snapped window (S6, rule 8): the sage packs ship exactly this
# =====================================================================================================================
print()
print("(p) a data pack's frame_grid is the grid its snapped shots render on")
write_pack("gridsnap", frame_grid={"demograd": {"step": 17, "offset": 5, "min": 124, "max": 362, "carried": 22}},
           fields=[{"id": "length", "label": "Length", "type": "int", "tier": "primary", "group": "Length", "order": 1,
                    "bind": [{"node": "1", "input": "length"}], "default": 124, "range": [124, 362], "units": "frames"}])
json.dump({"1": {"class_type": "X", "inputs": {"length": 124}}}, open(os.path.join(PACK_DATA, "packs", "gridsnap", "g.json"), "w"))
loaded_p, problems_p = load_packs()
check("setup: the data pack loads", len(loaded_p) == 1 and not problems_p, (loaded_p, problems_p))
engines.packs().extend(loaded_p)                     # the server's discovery cache, as a restart would have filled it
try:
    check("the server's _frame_grid reads the data pack's grid for its mode (carried 22 as declared)",
          mod._frame_grid("video", "demograd") == {"step": 17, "offset": 5, "min": 124, "max": 362, "carried": 22},
          mod._frame_grid("video", "demograd"))
    sp = R.new_seq("data pack grid")
    MASTER_P = wav_bytes(40.0)
    DB_P = regular_downbeats(0.5, 2.4, 40.0)
    R.import_master(sp, MASTER_P)
    R.ok(sp, "set_audio_led", on=True)
    R.ok(sp, "set_song_map", song_map=north_star_map(MASTER_P, 40.0, DB_P))
    head = R.ok(sp, "add_slot", lane="video", cap="video", mode="demograd", values={"length": 124})["added_slot_id"]
    R.ok(sp, "add_slot", lane="video", cap="video", mode="demograd", values={"length": 124})
    R.ok(sp, "set_bar_snap", on=True)
    R.ok(sp, "set_shot_bars", slot_id=head, bars=2)
    wp = R.slot(sp, head)["window"]
    check("a snapped shot of the data pack renders on its 17n+5 grid, at least 124",
          (wp["render_frames"] - 5) % 17 == 0 and wp["render_frames"] >= 124, wp)
    check("an uncabled shot carries 0 (S2): the render is the smallest grid length covering the window alone",
          wp["render_frames"] == beatsnap.render_frames(wp["f1"] - wp["f0"], {"step": 17, "offset": 5, "min": 124, "max": 362, "carried": 0}), wp)
finally:
    for pk in loaded_p:
        engines.packs().remove(pk)
    shutil.rmtree(os.path.join(PACK_DATA, "packs", "gridsnap"), ignore_errors=True)

# =====================================================================================================================
# (q) Forge Master on_beats against the real analyze and the real ops (S6): find_beats, snap_shots, the one-planner guard
# =====================================================================================================================
print()
print("(q) Forge Master: find_beats and snap_shots on the real server")
import forge_master
import forge_run
orig_generate, orig_lane = mod.generate, mod._grid_lane
mod.generate, mod._grid_lane = stub_generate, lambda lane_id=None: {"id": "cpu", "name": "CPU lane", "kind": "process"}
mod.ANALYZE_POLL_S, mod.FORGE_BEATS_POLL_S = 0.05, 0.05
try:
    DB_Q = regular_downbeats(0.4, 2.4, 40.0)
    sq = R.new_seq("forge master beats")
    R.import_master(sq, wav_bytes(40.0, bursts=(5.0,)))
    bq = mod._ForgeBackend("run_q", mod.LANE_BY_ID["t"], W, H)
    nxt = "gridstub%02d" % (STUB["n"] + 1)
    threading.Timer(0.4, finish_grid, (nxt, DB_Q, 40.0)).start()
    got = bq.find_beats(sq)
    check("find_beats returns the stored song map once the analyze job lands",
          got["downbeats"] == DB_Q and got["source"]["seconds"] == 40.0 and got == R.stored(sq)["song_map"], list(got))
    wins = forge_master.beat_windows(got, 40.0)
    bq.set_audio_led(sq)
    slots_q = [bq.add_shot(sq, w, "a shot", "start.png", W, H) for w in wins]
    bq.snap_shots(sq, slots_q, wins)
    derived = [R.slot(sq, x)["window"] for x in slots_q]
    check("snap_shots: Snap to beats is on and every derived window equals the planned one (one planner)",
          R.stored(sq).get("bar_snap") is True
          and [(d["f0"], d["f1"], d["render_frames"]) for d in derived] == [(w["f0"], w["f1"], w["frames"]) for w in wins],
          [(d["f0"], d["f1"], d["render_frames"]) for d in derived][:3])
    check("snap_shots: all shots but the last carry the planned bars, the last runs to the end",
          [R.slot(sq, x).get("bars") for x in slots_q[:-1]] == [w["bars"] for w in wins[:-1]]
          and R.slot(sq, slots_q[-1]).get("to_end") is True, [R.slot(sq, x).get("bars") for x in slots_q])
    bad = [dict(w) for w in wins]
    bad[1]["frames"] += 8
    try:
        bq.snap_shots(sq, slots_q, bad)
        msg = None
    except forge_run.RunError as e:
        msg = str(e)
    check("snap_shots: a plan the Cutting Room does not derive is refused with the sentence naming the shot",
          msg == "The beat plan and the Cutting Room disagree on shot 2.", msg)

    # an errored grid job, a timeout, and the person's stop
    se = R.new_seq("forge master beats error")
    R.import_master(se, wav_bytes(40.0, bursts=(5.0,)))
    nxt = "gridstub%02d" % (STUB["n"] + 1)
    threading.Timer(0.3, finish_grid, (nxt, DB_Q, 40.0, "error", "beat_this is not installed.")).start()
    try:
        bq.find_beats(se)
        msg = None
    except forge_run.BeatsUnavailable as e:
        msg = str(e)
    check("an errored beat-finding job falls back with the generic sentence and the job's own reason",
          msg == "Finding the song's beats failed (beat_this is not installed), so the shots are spaced evenly instead.", msg)
    st = R.new_seq("forge master beats slow")
    R.import_master(st, wav_bytes(40.0, bursts=(5.0,)))
    mod.FORGE_BEATS_MAX_S = 0.3
    try:
        bq.find_beats(st)
        msg = None
    except forge_run.BeatsUnavailable as e:
        msg = str(e)
    finally:
        mod.FORGE_BEATS_MAX_S = 900
    check("a job that never lands times out with the generic sentence",
          msg == "Finding the song's beats failed (finding the beats took too long), so the shots are spaced evenly instead.", msg)
    ss = R.new_seq("forge master beats stop")
    R.import_master(ss, wav_bytes(40.0, bursts=(5.0,)))
    forge_run.FORGE_RUN_STOP["run_q"] = True
    try:
        bq.find_beats(ss)
        msg = None
    except forge_run.RunStopped:
        msg = "stopped"
    finally:
        forge_run.FORGE_RUN_STOP.pop("run_q", None)
    check("the person's stop ends the wait for the beats", msg == "stopped", msg)
finally:
    mod.generate, mod._grid_lane = orig_generate, orig_lane

print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
