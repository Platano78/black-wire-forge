"""Audio-led sequences (opt-in): the sequence's master sound leads the cut and drives the shots.

Off by default. A sequence is audio-led only when it carries ``"audio_led": true`` (op ``set_audio_led``). With the flag
absent every API response, take and cut is what it has always been; the frozen cut tests and tests/test_sequences.py
guard that, and the classic-control checks below re-prove it next to the new behaviour.

What is covered: the op and its validation, the derived windows (planned lengths laid end to end), refusal without a
master, the slice that a shot generates against (uploaded wav, LoadAudio node, recorded window), the cut itself
(the master plays at full level, the clips' own audio is dropped), and the per-shot "Follows the song" opt-out
(set_lipsync), which makes one shot with no sound clip while its siblings are unchanged.

The helper block between the markers is copied verbatim from tests/test_cut.py (a frozen script that cannot be
imported); do not edit it here either. Run: python3 tests/test_audio_led.py
"""
import copy
import importlib.util
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Finding #21: ffmpeg/ffprobe are optional deps of the app itself -- their
# absence on a clean clone is a SKIP, not a build failure. This guard is
# purely environmental (added before any of the frozen checks below run);
# it does not touch the frozen assertions themselves ("DO NOT EDIT THIS
# FILE to make a build pass" above is about the checks, not this).
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg/ffprobe not on PATH -- this suite needs both")
    sys.exit(0)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

def note(msg):
    print("  ....  " + msg)


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe helpers for building synthetic media and reading it back.
# ---------------------------------------------------------------------------

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
LOUDNESS_DIFF_MIN = 8.0
BED_DIFF_TARGET = 18.0
BED_DIFF_TOL = 4.0
TITLE_DIFF_MIN = 0.25
TITLE_DIFF_MAX_OUTSIDE = 0.22


def run(cmd, timeout=60):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def ffmpeg(args, label="ffmpeg"):
    rc, out, err = run(["ffmpeg", "-y", "-hide_banner"] + args, timeout=120)
    if rc != 0:
        raise RuntimeError("%s failed (rc=%d): %s" % (label, rc, err[-2000:]))
    return err


def ffprobe_json(path, extra=()):
    rc, out, err = run(["ffprobe", "-v", "quiet", "-print_format", "json"] + list(extra) + [path])
    return json.loads(out) if out else {}


def video_stream(info):
    return next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)


def audio_stream(info):
    return next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)


def probed_duration(path):
    info = ffprobe_json(path, ["-show_format"])
    return float(info.get("format", {}).get("duration") or 0.0)


def all_tag_blob(info):
    """Every k/v from ffprobe's format.tags + each stream's tags, lowercased
    and joined -- scoped to metadata TAGS only, never the whole ffprobe JSON
    (which also carries structural keys like disposition.comment, a stream
    flag unrelated to metadata, that would otherwise false-positive)."""
    parts = []
    for k, v in (info.get("format", {}).get("tags") or {}).items():
        parts.append(str(k)); parts.append(str(v))
    for s in info.get("streams", []):
        for k, v in (s.get("tags") or {}).items():
            parts.append(str(k)); parts.append(str(v))
    return " ".join(parts).lower()


def counted_frames(path):
    info = ffprobe_json(path, ["-count_frames", "-select_streams", "v:0", "-show_entries",
                                "stream=nb_read_frames"])
    v = info.get("streams", [{}])[0].get("nb_read_frames")
    return int(v) if v not in (None, "N/A") else None


def mean_volume(path, trim=None, af="volumedetect"):
    args = []
    if trim:
        args += ["-ss", str(trim[0]), "-t", str(trim[1])]
    args += ["-i", path, "-af", af, "-f", "null", "-"]
    err = ffmpeg(args, "volumedetect")
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
    return float(m.group(1)) if m else None


def band_volume(path, freq, width):
    return mean_volume(path, af="bandpass=f=%d:w=%d,volumedetect" % (freq, width))


def true_peak_dbtp(path):
    """loudnorm's own measurement pass (print_format=json) reports input_tp:
    the true peak of the WHOLE file in dBTP -- exactly what R4 asks for."""
    err = ffmpeg(["-i", path, "-af", "loudnorm=I=-16:TP=-1.0:LRA=11:print_format=json",
                  "-f", "null", "-"], "loudnorm measure")
    m = re.search(r"\{[^{}]*\"input_tp\"[^{}]*\}", err, re.S)
    if not m:
        return None
    return float(json.loads(m.group(0))["input_tp"])


def extract_frame_png(path, t, out):
    ffmpeg(["-ss", str(t), "-i", path, "-frames:v", "1", out], "extract_frame")


def png_mean_abs_diff(a, b):
    # .tobytes() over .getdata() (removed in Pillow 14, 2027-10-15, per
    # Pillow's own DeprecationWarning) -- raw RGB bytes, 3 per pixel, same
    # sum-of-abs-differences this always computed, without the per-pixel
    # tuple round-trip.
    from PIL import Image
    ia = Image.open(a).convert("RGB")
    ib = Image.open(b).convert("RGB")
    if ia.size != ib.size:
        return 999.0
    ba, bb = ia.tobytes(), ib.tobytes()
    total = sum(abs(x - y) for x, y in zip(ba, bb))
    return total / (len(ba))


def make_clip(path, width=640, height=360, fps=24, duration=3.0, color="red",
              audio=True, sample_rate=48000, channels=2, audio_freq=1000, volume_db=None,
              format_marker=None, stream_marker=None):
    """A real, small H.264+AAC mp4 built entirely with ffmpeg lavfi sources --
    no external fixture assets, so every property (size/fps/audio format/
    duration) is exactly under this test's control."""
    args = ["-f", "lavfi", "-i", "color=c=%s:size=%dx%d:rate=%s:duration=%s" % (color, width, height, fps, duration)]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=%d:duration=%s" % (audio_freq, sample_rate, duration)]
        args += ["-ac", str(channels)]
        if volume_db is not None:
            args += ["-af", "volume=%sdB" % volume_db]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac"]
    else:
        args += ["-an"]
    if format_marker:
        args += ["-metadata", "title=%s" % format_marker, "-metadata", "comment=%s" % format_marker,
                  "-metadata", "description=%s" % format_marker, "-metadata", "prompt=%s" % format_marker]
    if stream_marker:
        args += ["-metadata:s:v:0", "comment=%s" % stream_marker, "-metadata:s:v:0", "prompt=%s" % stream_marker]
        if audio:
            args += ["-metadata:s:a:0", "comment=%s" % stream_marker, "-metadata:s:a:0", "prompt=%s" % stream_marker]
    args += [path]
    ffmpeg(args, "make_clip")
    return path


def make_color_change_clip(path, width=640, height=360, fps=24, duration=6.0,
                            colors=("red", "blue"), sample_rate=48000):
    """A clip whose picture colour changes once per second (red 0-1s, blue
    1-2s, red 2-3s, ...), so a trim's TAIL can be proven by content: if the
    cut kept the wrong end, the first frame's colour would be wrong."""
    parts = []
    for i in range(int(duration)):
        parts.append("color=c=%s:size=%dx%d:rate=%s:duration=1" % (colors[i % len(colors)], width, height, fps))
    filt = "".join("[%d:v]" % i for i in range(len(parts))) + ("concat=n=%d:v=1:a=0[v]" % len(parts))
    args = []
    for p in parts:
        args += ["-f", "lavfi", "-i", p]
    args += ["-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=%d:duration=%s" % (sample_rate, duration)]
    args += ["-filter_complex", filt, "-map", "[v]", "-map", "%d:a" % len(parts),
              "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path]
    ffmpeg(args, "make_color_change_clip")
    return path


def frame_dominant_color(path, t):
    """Sample one pixel from the frame at time t; returns (r,g,b)."""
    from PIL import Image
    tmp = path + ".sample_%s.png" % str(t).replace(".", "_")
    extract_frame_png(path, t, tmp)
    im = Image.open(tmp).convert("RGB")
    px = im.getpixel((im.width // 2, im.height // 2))
    os.remove(tmp)
    return px


# ---------------------------------------------------------------------------
# HTTP + process helpers (house pattern, tests/test_sequence_ui.py's shape)
# ---------------------------------------------------------------------------

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def http_json(url, data=None, method=None, timeout=10, headers=None):
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    if headers:
        hdrs.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null"), dict(e.headers)
        except Exception:
            return e.code, None, {}


def http_raw(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def wait_true(desc, fn, timeout, interval=0.2):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return True, last
        except Exception as e:
            last = e
        time.sleep(interval)
    check(desc, False, "still false after %.0fs (last=%r)" % (timeout, last))
    return False, last


def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


PROCS = []   # every subprocess this file starts, stopped at exit no matter what


def start_fake_lane(scratch, name):
    store = os.path.join(scratch, "fake_%s" % name)
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(port), "--store", store],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(proc)
    ok, _ = wait_true("fake lane %s answers /system_stats" % name,
                       lambda: http_json("http://127.0.0.1:%d/system_stats" % port)[0] == 200, 15)
    if not ok:
        raise SystemExit("fake lane %s did not come up" % name)
    return proc, port, store


def control_accept(port, outputs):
    http_json("http://127.0.0.1:%d/_control/accept" % port, data={"outputs": outputs}, method="POST")


def start_server(scratch, lane_port, name="main", extra_lanes=(), job_poll_seconds=0.3, cut_config=None):
    data_dir = os.path.join(scratch, "data_%s" % name)
    cfg_path = os.path.join(scratch, "config_%s.json" % name)
    port = free_port()
    lanes = [{"id": "t", "name": "Target lane", "host": "127.0.0.1", "port": lane_port,
              "caps": ["image", "video", "audio"]}]
    lanes += list(extra_lanes)
    cfg = {"title": "C3.6 cut test", "port": port, "bind": "127.0.0.1", "lanes": lanes,
           "timing": {"poll_seconds": 0.3, "job_poll_seconds": job_poll_seconds,
                      "http_timeout": 5.0, "free_settle_seconds": 1.0, "discover_seconds": 300.0}}
    if cut_config is not None:
        cfg["cut"] = cut_config
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    logf = open(os.path.join(scratch, "server_%s.log" % name), "w")
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data_dir)
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                             stdout=logf, stderr=subprocess.STDOUT)
    PROCS.append(proc)
    url = "http://127.0.0.1:%d/" % port
    ok, _ = wait_true("server %s is up" % name, lambda: http_json(url + "api/health")[0] == 200, 20)
    if not ok:
        logf.flush()
        with open(os.path.join(scratch, "server_%s.log" % name)) as f:
            note("server %s log tail: %s" % (name, f.read()[-1500:]))
    wait_lane_up(url, "t")
    return proc, url, data_dir, logf


def wait_lane_up(url, lane_id, timeout=60):
    def lane_up():
        code, body, _ = http_json(url + "api/lanes")
        lanes = body.get("lanes") if isinstance(body, dict) else body
        if code != 200 or not isinstance(lanes, list):
            return False
        l = next((x for x in lanes if x.get("id") == lane_id), None)
        return bool(l and l.get("up") and l.get("discovered"))  # discovered: generate before discovery is refused (503)
    ok, _ = wait_true("lane %r is up (server's own lane poller discovered it)" % lane_id, lane_up, timeout)
    return ok


# ---------------------------------------------------------------------------
# Sequence API helpers (thin wrappers over the real HTTP routes)
# ---------------------------------------------------------------------------

def seq_create(url, title):
    code, body, _ = http_json(url + "api/sequence", data={"title": title, "mode": "sequence"}, method="POST")
    if code != 200:
        raise RuntimeError("seq_create failed: %r" % (body,))
    return body


def seq_get(url, sid):
    code, body, _ = http_json(url + "api/sequence?id=" + sid)
    if code != 200:
        raise RuntimeError("seq_get failed: %r" % (body,))
    return body


def seq_op(url, sid, rev, op, **kw):
    payload = dict(kw, id=sid, rev=rev, op=op)
    code, body, _ = http_json(url + "api/sequence/op", data=payload, method="POST")
    return code, body


def add_video_slot(url, sid, rev, length=121, prompt="a test shot", at=None):
    kw = dict(lane="video", cap="video", mode="ltx", values={"prompt": prompt, "length": length})
    if at is not None:
        kw["at"] = at
    code, body = seq_op(url, sid, rev, "add_slot", **kw)
    if code != 200:
        raise RuntimeError("add_slot failed: %r" % (body,))
    return body


def generate_slot(url, sid, slot_id, lane_port, clip_path, timeout=20):
    """Arm the fake lane to hand back `clip_path` as this render's output,
    POST /api/sequence/generate for real, then wait for the take's `file`
    to be set by the SERVER'S OWN harvest (job_poller -> seq_harvest), never
    written by this test. Returns the finished slot dict."""
    fname = os.path.basename(clip_path)
    dest_dir = None  # caller must have already placed the file under the fake lane's outputs dir
    control_accept(lane_port, [{"filename": fname, "subfolder": "", "type": "output"}])
    code, body, _ = http_json(url + "api/sequence/generate", data={"id": sid, "slot_id": slot_id}, method="POST")
    if code != 200 or not body.get("ok"):
        raise RuntimeError("seq_generate failed: %r" % (body,))
    job_id = body["job"]["id"]

    def harvested():
        seq = seq_get(url, sid)
        slot = next(s for s in seq["slots"] if s["id"] == slot_id)
        take = next((t for t in slot["takes"] if t["job_id"] == job_id), None)
        return take if take and take.get("file") else None

    ok, take = wait_true("take %s harvested by the server's own poller" % job_id, harvested, timeout)
    control_accept(lane_port, None)
    seq = seq_get(url, sid)
    slot = next(s for s in seq["slots"] if s["id"] == slot_id)
    return slot, job_id, seq["rev"]


def pick(url, sid, rev, slot_id, job_id):
    code, body = seq_op(url, sid, rev, "pick_take", slot_id=slot_id, job_id=job_id)
    if code != 200:
        raise RuntimeError("pick_take failed: %r" % (body,))
    return body


def set_trim(url, sid, rev, slot_id, trim):
    return seq_op(url, sid, rev, "set_trim", slot_id=slot_id, trim=trim)


def set_title_card(url, sid, rev, slot_id, title):
    return seq_op(url, sid, rev, "set_title_card", slot_id=slot_id, title=title)


def do_cut(url, sid):
    code, body, _ = http_json(url + "api/sequence/cut", data={"id": sid}, method="POST")
    return code, body


UNKNOWN_ROUTE_BODY = {}   # url -> (code, body) the server's generic 404 catch-all returns


def capture_unknown_route_body(url):
    """POST to a nonsense /api/ path once per server instance and remember
    exactly what its generic 'not found' catch-all looks like (server.py's
    do_POST ends with `self.send_json({"error": "not found"}, 404)` for any
    unmatched path) -- a real refusal must be distinguishable from THIS, not
    merely "some 4xx", since a missing route also 404s and would otherwise
    let a check pass on a tree where the feature does not exist at all."""
    code, body, _ = http_json(url + "api/__nonsense_probe_%s__" % os.urandom(4).hex(), method="POST")
    UNKNOWN_ROUTE_BODY[url] = (code, body)
    return code, body


def refused(url, code, body, keywords=None):
    """A genuine, decision-bearing synchronous refusal: 4xx, NOT identical
    to this server's generic unknown-route response (captured per instance
    by capture_unknown_route_body), never literally 404 (the unknown-route
    status itself), carrying a non-empty `error` sentence, and -- when
    `keywords` is given -- that sentence containing at least one of them
    (case-insensitive), a loose guard against a 4xx that is real but for an
    unrelated reason."""
    if not (400 <= code < 500) or code == 404:
        return False
    if UNKNOWN_ROUTE_BODY.get(url) == (code, body):
        return False
    if not (isinstance(body, dict) and isinstance(body.get("error"), str) and body["error"].strip()):
        return False
    if keywords:
        text = body["error"].lower()
        if not any(k in text for k in keywords):
            return False
    return True


def wait_cut_done(url, sid, cut_id, timeout=60):
    def get_entry():
        seq = seq_get(url, sid)
        entry = next((c for c in seq.get("cuts") or [] if c.get("id") == cut_id), None)
        if entry and entry.get("status") in ("done", "error", "interrupted"):
            return entry
        return None
    ok, entry = wait_true("cut %s reaches a terminal status" % cut_id, get_entry, timeout, interval=0.5)
    return entry if ok else None


def cut_file_path(data_dir, sid, entry, cut_id):
    """Resolve a finished cut's output to a real path on disk. `entry["file"]`
    is expected relative to data/seq/<id>/ (the same convention as a take's
    own `file`, e.g. "takes/<job>.<ext>") -- fall back to the spec's literal
    "cuts/<cut_id>.mp4" if a build used a bare filename or left it out."""
    seq_dir = os.path.join(data_dir, "seq", sid)
    f = entry.get("file") if entry else None
    if f:
        p = os.path.join(seq_dir, f) if not os.path.isabs(f) else f
        if os.path.isfile(p):
            return p
        p2 = os.path.join(seq_dir, os.path.basename(f))
        if os.path.isfile(p2):
            return p2
    return os.path.join(seq_dir, "cuts", "%s.mp4" % cut_id)

# ---- end of the helper block copied from tests/test_cut.py ----


def add_sound_slot(url, sid, rev, seconds=6.0, at=None):
    kw = dict(lane="sound", cap="audio", mode="sfx", values={"prompt": "a hum", "seconds": seconds})
    if at is not None:
        kw["at"] = at
    code, body = seq_op(url, sid, rev, "add_slot", **kw)
    if code != 200:
        raise RuntimeError("add_slot(sound) failed: %r" % (body,))
    return body


def make_audio_only(path, freq, duration, sample_rate=48000):
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=%d:duration=%s" % (freq, sample_rate, duration),
            "-c:a", "aac", path], "make_audio_only")
    return path


def make_tone_then_silence(path, freq, tone_s, silence_s):
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=48000:duration=%s" % (freq, tone_s),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=%s" % silence_s,
            "-filter_complex", "[0:a]aformat=channel_layouts=mono[a];[1:a]aformat=channel_layouts=mono[b];[a][b]concat=n=2:v=0:a=1",
            "-c:a", "aac", path], "make_tone_then_silence")
    return path


def stored_sequence(data_dir, sid):
    with open(os.path.join(data_dir, "sequences", sid + ".json")) as f:
        return json.load(f)


def lane_log_count(store, needles=("/prompt", "/upload/image")):
    path = os.path.join(store, "requests.log")
    if not os.path.isfile(path):
        return 0
    with open(path) as f:
        return sum(1 for ln in f if any(n in ln for n in needles))


SCRATCH = tempfile.mkdtemp(prefix="bwf_audio_led_")
import atexit
def _cleanup():
    for p in PROCS:
        stop(p)
    shutil.rmtree(SCRATCH, ignore_errors=True)
atexit.register(_cleanup)

FAKE_PROC, FAKE_PORT, FAKE_STORE = start_fake_lane(SCRATCH, "t")
FAKE_OUT = os.path.join(FAKE_STORE, "outputs")
_srv_proc, URL, DATA_DIR, _srv_log = start_server(SCRATCH, FAKE_PORT, name="main")
_probe = seq_create(URL, "canvas probe")
CW, CH = _probe["canvas"]["width"], _probe["canvas"]["height"]
note("canvas %dx%d" % (CW, CH))


print()
print("1. set_audio_led: an opt-in switch with validation; off removes the key")
s1 = seq_create(URL, "led switch")
sid1 = s1["id"]
code, body = seq_op(URL, sid1, s1["rev"], "set_audio_led", on=1)
check("a non-bool (1) is refused with a sentence", code == 400 and "on or off" in (body.get("error") or ""), (code, body))
code, body = seq_op(URL, sid1, s1["rev"], "set_audio_led", on="yes")
check("a string is refused", code == 400, (code, body))
code, body = seq_op(URL, sid1, s1["rev"], "set_audio_led", on=True)
check("on=true is accepted and shows in the API", code == 200 and body.get("audio_led") is True, (code, body.get("audio_led")))
check("on=true is stored in the sequence file", stored_sequence(DATA_DIR, sid1).get("audio_led") is True)
code, body = seq_op(URL, sid1, body["rev"], "set_audio_led", on=False)
check("on=false is accepted", code == 200, (code, body))
check("on=false removes the key from the API", "audio_led" not in body and "master_slot_id" not in body)
check("on=false removes the key from the stored file (byte-identical to a sequence that never had it)",
      "audio_led" not in stored_sequence(DATA_DIR, sid1))


print()
print("2. classic control: a sequence that never set the flag has none of the new keys and cuts as before")
s2 = seq_create(URL, "classic")
sid2 = s2["id"]
sl2 = add_video_slot(URL, sid2, s2["rev"], length=97, prompt="classic")["slots"][0]["id"]
view2 = seq_get(URL, sid2)
check("no audio_led / master_slot_id at the top level", "audio_led" not in view2 and "master_slot_id" not in view2)
check("no window on a slot", all("window" not in s for s in view2["slots"]))
clip2 = os.path.join(FAKE_OUT, "classic.mp4")
make_clip(clip2, width=CW, height=CH, duration=2.0, audio_freq=1000)
slot2, job2, rev2 = generate_slot(URL, sid2, sl2, FAKE_PORT, clip2)
check("a classic take has no audio_window", "audio_window" not in (slot2["takes"][0].get("inputs") or {}), slot2["takes"][0].get("inputs"))
rev2 = pick(URL, sid2, rev2, sl2, job2)["rev"]
code, b = do_cut(URL, sid2)
entry2 = wait_cut_done(URL, sid2, b.get("cut_id")) if code == 200 else None
check("the classic cut finishes", bool(entry2 and entry2.get("status") == "done"), (code, b, entry2))
check("a classic cut record has no audio_led key", entry2 is not None and "audio_led" not in entry2)
out2 = cut_file_path(DATA_DIR, sid2, entry2, b.get("cut_id")) if entry2 else None
v1000_classic = band_volume(out2, 1000, 100) if out2 and os.path.isfile(out2) else None
check("the classic cut carries the clip's own 1 kHz audio", v1000_classic is not None and v1000_classic > -40, v1000_classic)


print()
print("3. windows: planned lengths laid end to end (97 frames plan 4.0 s; a trim length replaces it)")
s3 = seq_create(URL, "windows")
sid3 = s3["id"]
body = add_video_slot(URL, sid3, s3["rev"], length=97, prompt="w1")
body = add_video_slot(URL, sid3, body["rev"], length=97, prompt="w2")
body = add_video_slot(URL, sid3, body["rev"], length=97, prompt="w3")
code, body = seq_op(URL, sid3, body["rev"], "set_audio_led", on=True)
wins = [(s["id"], s.get("window")) for s in body["slots"] if s["lane"] == "video"]
check("three windows start at 0, 4, 8 with len 4.0",
      [w[1] and (w[1]["start"], w[1]["len"]) for w in wins] == [(0.0, 4.0), (4.0, 4.0), (8.0, 4.0)], wins)
check("master_slot_id is null with no sound shot", body.get("master_slot_id") is None, body.get("master_slot_id"))
code, body = seq_op(URL, sid3, body["rev"], "set_trim", slot_id=wins[1][0], trim={"in": 0.0, "len": 2.5})
wins = [(s["id"], s.get("window")) for s in body["slots"] if s["lane"] == "video"] if code == 200 else []
check("a 2.5 s trim on shot 2 shortens its window and moves shot 3 to start 6.5",
      code == 200 and [w[1] and (w[1]["start"], w[1]["len"]) for w in wins] == [(0.0, 4.0), (4.0, 2.5), (6.5, 4.0)], (code, wins))


print()
print("4. generate refuses without a master sound and sends nothing to the lane")
s4 = seq_create(URL, "no master")
sid4 = s4["id"]
body = add_video_slot(URL, sid4, s4["rev"], length=97, prompt="n1")
sl4 = body["slots"][0]["id"]
code, body = seq_op(URL, sid4, body["rev"], "set_audio_led", on=True)
before = lane_log_count(FAKE_STORE)
control_accept(FAKE_PORT, [{"filename": "classic.mp4", "subfolder": "", "type": "output"}])
code, body, _ = http_json(URL + "api/sequence/generate", data={"id": sid4, "slot_id": sl4}, method="POST")
control_accept(FAKE_PORT, None)
check("generate on an audio-led video shot with no master is refused (400) with a plain sentence",
      code == 400 and "audio-led" in (body.get("error") or ""), (code, body))
check("nothing was uploaded or submitted to the lane", lane_log_count(FAKE_STORE) == before, (before, lane_log_count(FAKE_STORE)))


print()
print("5. generate with a master: the shot runs against its window of the master")
s5 = seq_create(URL, "with master")
sid5 = s5["id"]
body = add_video_slot(URL, sid5, s5["rev"], length=97, prompt="m1")
body = add_video_slot(URL, sid5, body["rev"], length=97, prompt="m2")
vid_a, vid_b = [s["id"] for s in body["slots"]]
body = add_sound_slot(URL, sid5, body["rev"], seconds=12.0)
snd5 = body["slots"][2]["id"]
master5 = os.path.join(FAKE_OUT, "master5.m4a")
make_audio_only(master5, 440, 12.0)
slot_s, job_s, rev5 = generate_slot(URL, sid5, snd5, FAKE_PORT, master5)
rev5 = pick(URL, sid5, rev5, snd5, job_s)["rev"]
code, body = seq_op(URL, sid5, rev5, "set_audio_led", on=True)
rev5 = body["rev"]
check("master_slot_id names the picked sound shot", body.get("master_slot_id") == snd5, body.get("master_slot_id"))
clip5 = os.path.join(FAKE_OUT, "shot5.mp4")
make_clip(clip5, width=CW, height=CH, duration=2.0, audio_freq=1000)
slot_a, job_a, rev5 = generate_slot(URL, sid5, vid_a, FAKE_PORT, clip5)
win_a = (slot_a["takes"][0].get("inputs") or {}).get("audio_window")
check("take A records its audio window (start 0, len 4.0)", win_a == {"start": 0.0, "len": 4.0}, win_a)
with open(os.path.join(FAKE_STORE, "last_prompt.json")) as f:
    graph_a = json.load(f)
load_a = [n for n in graph_a.values() if n.get("class_type") == "LoadAudio"]
check("the submitted graph has a LoadAudio node", len(load_a) == 1, list(graph_a)[:12])
name_a = load_a[0]["inputs"]["audio"] if load_a else None
up_a = os.path.join(FAKE_STORE, "inputs", name_a) if name_a else None
check("the lane holds the uploaded wav named in LoadAudio", bool(up_a and os.path.isfile(up_a)), name_a)
dur_a = probed_duration(up_a) if up_a and os.path.isfile(up_a) else 0.0
check("the uploaded slice is the window plus one frame (4.0417 s)", abs(dur_a - (4.0 + 1 / 24)) < 0.1, dur_a)
slot_b, job_b, rev5 = generate_slot(URL, sid5, vid_b, FAKE_PORT, clip5)
win_b = (slot_b["takes"][0].get("inputs") or {}).get("audio_window")
check("take B records its window (start 4.0, len 4.0)", win_b == {"start": 4.0, "len": 4.0}, win_b)


print()
print("6. the audio-led cut: master at full level, the clips' own audio dropped")
s6 = seq_create(URL, "led cut")
sid6 = s6["id"]
body = add_video_slot(URL, sid6, s6["rev"], length=97, prompt="c1")
body = add_video_slot(URL, sid6, body["rev"], length=97, prompt="c2")
v6a, v6b = [s["id"] for s in body["slots"]]
body = add_sound_slot(URL, sid6, body["rev"], seconds=4.0)
snd6 = body["slots"][2]["id"]
master6 = os.path.join(FAKE_OUT, "master6.m4a")
make_tone_then_silence(master6, 440, 2.0, 2.0)            # 2 s of 440 Hz, then 2 s of silence
clips6 = []
for k in (1, 2):
    c = os.path.join(FAKE_OUT, "led_clip%d.mp4" % k)
    make_clip(c, width=CW, height=CH, duration=2.0, audio_freq=1000, color=("red" if k == 1 else "blue"))
    clips6.append(c)
_, job_ca, rev6 = generate_slot(URL, sid6, v6a, FAKE_PORT, clips6[0])
_, job_cb, rev6 = generate_slot(URL, sid6, v6b, FAKE_PORT, clips6[1])
_, job_m, rev6 = generate_slot(URL, sid6, snd6, FAKE_PORT, master6)
for slot_id, job in ((v6a, job_ca), (v6b, job_cb), (snd6, job_m)):
    rev6 = pick(URL, sid6, rev6, slot_id, job)["rev"]
code, body = seq_op(URL, sid6, rev6, "set_audio_led", on=True)
code, b = do_cut(URL, sid6)
entry6 = wait_cut_done(URL, sid6, b.get("cut_id")) if code == 200 else None
check("the audio-led cut finishes", bool(entry6 and entry6.get("status") == "done"), (code, b, entry6 and entry6.get("log")))
check("the cut record says audio_led and lists the master", bool(entry6 and entry6.get("audio_led") is True
      and any(s.get("role") == "master" and s.get("slot_id") == snd6 for s in entry6.get("shots") or [])), entry6 and entry6.get("shots"))
out6 = cut_file_path(DATA_DIR, sid6, entry6, b.get("cut_id")) if entry6 else None
info6 = ffprobe_json(out6, ["-show_streams", "-show_format"]) if out6 and os.path.isfile(out6) else {}
check("exactly one audio stream", len([s for s in info6.get("streams", []) if s.get("codec_type") == "audio"]) == 1)
vdur = float((video_stream(info6) or {}).get("duration") or 0.0)
adur = float((audio_stream(info6) or {}).get("duration") or 0.0)
check("audio and video durations agree within 0.15 s (4 s picture)", vdur > 3.5 and abs(vdur - adur) < 0.15, (vdur, adur))
v440 = mean_volume(out6, trim=(0.2, 1.4), af="bandpass=f=440:w=60,volumedetect") if out6 else None
v1000 = mean_volume(out6, trim=(0.2, 1.4), af="bandpass=f=1000:w=100,volumedetect") if out6 else None
check("the first 1.6 s carries the master's 440 Hz", v440 is not None and v440 > -45, v440)
check("the clips' own 1 kHz is gone (>= 30 dB below the master)", v440 is not None and v1000 is not None and v440 - v1000 >= 30, (v440, v1000))
vtail = mean_volume(out6, trim=(2.4, 1.4)) if out6 else None
check("the last 1.6 s is the master's silence, not the clips' tone", vtail is not None and vtail < -50, vtail)


print()
print("7. audio-led with no master is refused; switching the flag off cuts the classic way")
s7 = seq_create(URL, "led no master cut")
sid7 = s7["id"]
body = add_video_slot(URL, sid7, s7["rev"], length=97, prompt="x1")
v7 = body["slots"][0]["id"]
slot7, job7, rev7 = generate_slot(URL, sid7, v7, FAKE_PORT, clips6[0])
rev7 = pick(URL, sid7, rev7, v7, job7)["rev"]
code, body = seq_op(URL, sid7, rev7, "set_audio_led", on=True)
code, b = do_cut(URL, sid7)
check("the cut is refused (400) naming audio-led and the missing sound pick",
      code == 400 and "audio-led" in (b.get("error") or ""), (code, b))
code, body = seq_op(URL, sid7, body["rev"], "set_audio_led", on=False)
code, b = do_cut(URL, sid7)
entry7 = wait_cut_done(URL, sid7, b.get("cut_id")) if code == 200 else None
check("with the flag off the same sequence cuts the classic way", bool(entry7 and entry7.get("status") == "done"
      and "audio_led" not in entry7), (code, b))
out7 = cut_file_path(DATA_DIR, sid7, entry7, b.get("cut_id")) if entry7 else None
vc7 = band_volume(out7, 1000, 100) if out7 and os.path.isfile(out7) else None
check("and keeps the clip's own audio", vc7 is not None and vc7 > -40, vc7)


print()
print("8. windows tile: a 97-frame take (4.0417 s) is trimmed to its planned 4.0 s window when audio-led")
s8 = seq_create(URL, "tiling")
sid8 = s8["id"]
body = add_video_slot(URL, sid8, s8["rev"], length=97, prompt="t1")
body = add_video_slot(URL, sid8, body["rev"], length=97, prompt="t2")
v8a, v8b = [s["id"] for s in body["slots"]]
body = add_sound_slot(URL, sid8, body["rev"], seconds=8.0)
snd8 = body["slots"][2]["id"]
master8 = os.path.join(FAKE_OUT, "master8.m4a")
make_audio_only(master8, 440, 9.0)
clips8 = []
for k in (1, 2):
    c = os.path.join(FAKE_OUT, "tile_clip%d.mp4" % k)
    make_clip(c, width=CW, height=CH, duration=97 / 24.0, audio_freq=1000, color=("red" if k == 1 else "blue"))
    clips8.append(c)
_, ja, rev8 = generate_slot(URL, sid8, v8a, FAKE_PORT, clips8[0])
_, jb, rev8 = generate_slot(URL, sid8, v8b, FAKE_PORT, clips8[1])
_, jm, rev8 = generate_slot(URL, sid8, snd8, FAKE_PORT, master8)
for slot_id, job in ((v8a, ja), (v8b, jb), (snd8, jm)):
    rev8 = pick(URL, sid8, rev8, slot_id, job)["rev"]
code, body = seq_op(URL, sid8, rev8, "set_audio_led", on=True)
code, b = do_cut(URL, sid8)
entry8 = wait_cut_done(URL, sid8, b.get("cut_id")) if code == 200 else None
out8 = cut_file_path(DATA_DIR, sid8, entry8, b.get("cut_id")) if entry8 else None
dur8 = probed_duration(out8) if out8 and os.path.isfile(out8) else 0.0
check("the audio-led cut of two 4.0417 s takes is 8.0 s (windows tile, no drift)", abs(dur8 - 8.0) < 0.05, dur8)
code, body = seq_op(URL, sid8, seq_get(URL, sid8)["rev"], "set_audio_led", on=False)
code, b = do_cut(URL, sid8)
entry8c = wait_cut_done(URL, sid8, b.get("cut_id")) if code == 200 else None
out8c = cut_file_path(DATA_DIR, sid8, entry8c, b.get("cut_id")) if entry8c else None
dur8c = probed_duration(out8c) if out8c and os.path.isfile(out8c) else 0.0
check("control: with the flag off the same takes cut at their full length (8.083 s)", abs(dur8c - 2 * 97 / 24.0) < 0.05, dur8c)


print()
print("9. per-shot opt-out: set_lipsync turns the master sound off for one shot, and only for that shot")
s9 = seq_create(URL, "lipsync opt out")
sid9 = s9["id"]
body = add_video_slot(URL, sid9, s9["rev"], length=97, prompt="off")
body = add_video_slot(URL, sid9, body["rev"], length=97, prompt="on")
v9a, v9b = [s["id"] for s in body["slots"]]
body = add_sound_slot(URL, sid9, body["rev"], seconds=8.0)
snd9 = body["slots"][2]["id"]
r9 = body["rev"]
code, body = seq_op(URL, sid9, r9, "set_lipsync", slot_id=v9a, on="yes")
check("a non-bool is refused with a sentence", code == 400 and "on or off" in (body.get("error") or ""), (code, body))
code, body = seq_op(URL, sid9, r9, "set_lipsync", slot_id=snd9, on=False)
check("a sound shot is refused with a sentence", code == 400 and "video shot" in (body.get("error") or ""), (code, body))
code, body = seq_op(URL, sid9, r9, "set_lipsync", slot_id=v9a, on=False)
check("on=false is accepted", code == 200, (code, body))
check("on=false puts lipsync:false on the slot",
      next(s for s in body["slots"] if s["id"] == v9a).get("lipsync") is False)
check("on=false is stored in the sequence file",
      next(s for s in stored_sequence(DATA_DIR, sid9)["slots"] if s["id"] == v9a).get("lipsync") is False)
check("the sibling shot is untouched (no key at all)", "lipsync" not in next(s for s in body["slots"] if s["id"] == v9b))
code, body = seq_op(URL, sid9, body["rev"], "set_lipsync", slot_id=v9a, on=True)
check("on=true is accepted", code == 200, (code, body))
check("on=true removes the key again (byte-identical to a shot nobody touched)",
      "lipsync" not in next(s for s in body["slots"] if s["id"] == v9a))
check("on=true removes the key from the stored file too",
      "lipsync" not in next(s for s in stored_sequence(DATA_DIR, sid9)["slots"] if s["id"] == v9a))

# An audio-led sequence with a real master: the opted-out shot is made with no
# sound clip at all, its sibling still gets its window of the master.
master9 = os.path.join(FAKE_OUT, "master9.m4a")
make_audio_only(master9, 440, 8.0)
slot_s9, job_s9, rev9 = generate_slot(URL, sid9, snd9, FAKE_PORT, master9)
rev9 = pick(URL, sid9, rev9, snd9, job_s9)["rev"]
code, body = seq_op(URL, sid9, rev9, "set_audio_led", on=True)
rev9 = body["rev"]
code, body = seq_op(URL, sid9, rev9, "set_lipsync", slot_id=v9a, on=False)
rev9 = body["rev"]
check("set_lipsync on:false survives set_audio_led", code == 200
      and next(s for s in body["slots"] if s["id"] == v9a).get("lipsync") is False, (code, body))
clip9 = os.path.join(FAKE_OUT, "shot9.mp4")
make_clip(clip9, width=CW, height=CH, duration=2.0, audio_freq=1000)
uploads_before = lane_log_count(FAKE_STORE, ("/upload/image",))
slot_off, job_off, rev9 = generate_slot(URL, sid9, v9a, FAKE_PORT, clip9)
check("the opted-out shot uploads no sound clip to the lane",
      lane_log_count(FAKE_STORE, ("/upload/image",)) == uploads_before,
      (uploads_before, lane_log_count(FAKE_STORE, ("/upload/image",))))
with open(os.path.join(FAKE_STORE, "last_prompt.json")) as f:
    graph_off = json.load(f)
check("its generate payload carries no audio_slice (no LoadAudio node in the graph)",
      not any(n.get("class_type") == "LoadAudio" for n in graph_off.values()), list(graph_off)[:12])
check("no wav was cut for it under the sequence's own media",
      not os.path.exists(os.path.join(DATA_DIR, "seq", sid9, "audio", "%s.wav" % v9a)))
check("its take records no audio window", "audio_window" not in (slot_off["takes"][0].get("inputs") or {}),
      slot_off["takes"][0].get("inputs"))
slot_on, job_on, rev9 = generate_slot(URL, sid9, v9b, FAKE_PORT, clip9)
check("the sibling shot without the flag still gets its window of the master",
      (slot_on["takes"][0].get("inputs") or {}).get("audio_window") == {"start": 4.0, "len": 4.0},
      slot_on["takes"][0].get("inputs"))
check("and it did upload the clip to the lane",
      lane_log_count(FAKE_STORE, ("/upload/image",)) > uploads_before)
with open(os.path.join(FAKE_STORE, "last_prompt.json")) as f:
    graph_on = json.load(f)
check("whose graph has the LoadAudio node naming the uploaded wav",
      len([n for n in graph_on.values() if n.get("class_type") == "LoadAudio"]) == 1, list(graph_on)[:12])


print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
