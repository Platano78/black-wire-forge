"""Import your own sound file as an audio-led sequence's master (opt-in).

POST /api/sequence/master (multipart: id, optional rev, file) keeps a sound file with the sequence; ops set_master_start
and clear_master manage it. The file only matters to an AUDIO-LED sequence, where it wins over a picked sound shot: shots
generate against their slices of it and the cut plays it from its start offset at full level.

Covered: refusals (extension, no sound, wrong content type, cross-site Origin, stale rev), the stored file and derived view,
the start offset reaching both the shot slices and the cut, file-over-slot precedence, clearing, and that a sequence with
no import (or an import but audio-led off) is untouched.

The helper block is copied verbatim from the frozen tests/test_cut.py. Run: python3 tests/test_master_import.py
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
import uuid


def add_sound_slot(url, sid, rev, seconds=6.0, at=None):
    kw = dict(lane="sound", cap="audio", mode="sfx", values={"prompt": "a hum", "seconds": seconds})
    if at is not None:
        kw["at"] = at
    code, body = seq_op(url, sid, rev, "add_slot", **kw)
    if code != 200:
        raise RuntimeError("add_slot(sound) failed: %r" % (body,))
    return body


def make_two_tone(path, f1, d1, f2, d2):
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=48000:duration=%s" % (f1, d1),
            "-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=48000:duration=%s" % (f2, d2),
            "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1", "-c:a", "aac", path], "make_two_tone")
    return path


def post_master(url, sid, filename, data, rev=None, ctype="audio/mp4", origin=None, content_type=None):
    b = uuid.uuid4().hex
    parts = ["--%s\r\nContent-Disposition: form-data; name=\"id\"\r\n\r\n%s\r\n" % (b, sid)]
    if rev is not None:
        parts.append("--%s\r\nContent-Disposition: form-data; name=\"rev\"\r\n\r\n%s\r\n" % (b, rev))
    head_b = "".join(parts) + ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % (b, filename, ctype))
    body = head_b.encode() + data + ("\r\n--%s--\r\n" % b).encode()
    headers = {"Content-Type": content_type or ("multipart/form-data; boundary=" + b)}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(url + "api/sequence/master", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as f:
            return f.status, json.loads(f.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def stored(data_dir, sid):
    with open(os.path.join(data_dir, "sequences", sid + ".json")) as f:
        return json.load(f)


SCRATCH = tempfile.mkdtemp(prefix="bwf_master_")
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

MASTER = os.path.join(SCRATCH, "song.m4a")
make_two_tone(MASTER, 440, 2.0, 880, 8.0)            # 2 s of 440 Hz, then 8 s of 880 Hz
MASTER_BYTES = open(MASTER, "rb").read()


print()
print("1. refusals")
s1 = seq_create(URL, "master refusals")
sid1 = s1["id"]
code, b = post_master(URL, sid1, "notes.txt", b"hello", ctype="text/plain")
check("a non-sound extension is refused (400, a plain sentence)", code == 400 and "sound file" in (b.get("error") or ""), (code, b))
code, b = post_master(URL, sid1, "fake.wav", b"this is not audio at all" * 50, ctype="audio/wav")
check("a .wav with no sound in it is refused", code == 400 and "no sound" in (b.get("error") or ""), (code, b))
code, b = post_master(URL, "s_nonexistent", "song.m4a", MASTER_BYTES)
check("an unknown sequence id is refused", code in (400, 404), (code, b))
code, b = post_master(URL, sid1, "song.m4a", MASTER_BYTES, content_type="application/json")
check("the route refuses anything but multipart (415)", code == 415, (code, b))
code, b = post_master(URL, sid1, "song.m4a", MASTER_BYTES, origin="http://evil.example")
check("the cross-site guard still applies to the new route (403)", code == 403, (code, b))
check("none of those left a master on the sequence", "master" not in stored(DATA_DIR, sid1))
code, b = post_master(URL, sid1, "song.m4a", MASTER_BYTES, rev=999)
check("a stale rev is refused with 409 and the current sequence", code == 409 and "sequence" in b, (code, list(b)))


print()
print("2. import: stored file, derived view, nothing else changes")
code, b = post_master(URL, sid1, "my song (final).m4a", MASTER_BYTES, rev=seq_get(URL, sid1)["rev"])
check("a real m4a is accepted", code == 200 and b.get("master", {}).get("name"), (code, b.get("error")))
mf = b.get("master") or {}
check("the derived view carries the cleaned name, seconds (about 10) and start 0",
      mf.get("name") == "my song _final_.m4a" and abs(float(mf.get("seconds") or 0) - 10.0) < 0.2 and mf.get("start") == 0.0, mf)
check("the file is kept under the sequence", os.path.isfile(os.path.join(DATA_DIR, "seq", sid1, "audio", "master.m4a")))
check("without audio-led there is no master_file / window / master_slot_id in the view",
      all(k not in b for k in ("audio_led", "master_file", "master_slot_id")) and all("window" not in s for s in b["slots"]))
other = seq_create(URL, "untouched")
check("another sequence has no master key", "master" not in seq_get(URL, other["id"]))


print()
print("3. set_master_start validation")
sid = sid1
rev = seq_get(URL, sid)["rev"]
for bad, why in ((-1, "negative"), (True, "a bool"), ("2", "a string"), (10.5, "past the end")):
    code, b = seq_op(URL, sid, rev, "set_master_start", start=bad)
    check("start %s is refused" % why, code == 400, (code, b.get("error")))
code, b = seq_op(URL, seq_create(URL, "nomaster")["id"], 1, "set_master_start", start=1)
check("set_master_start with no master is refused", code == 400 and "Add a sound file" in (b.get("error") or ""), (code, b))


print()
print("4. audio-led with the imported file: slices come from the file, honouring the start offset")
s4 = seq_create(URL, "led file")
sid4 = s4["id"]
code, b = post_master(URL, sid4, "song.m4a", MASTER_BYTES)
body = add_video_slot(URL, sid4, seq_get(URL, sid4)["rev"], length=97, prompt="f1")
body = add_video_slot(URL, sid4, body["rev"], length=97, prompt="f2")
va, vb = [s["id"] for s in body["slots"]]
code, body = seq_op(URL, sid4, body["rev"], "set_audio_led", on=True)
check("audio-led view names the master file and has no master slot", body.get("master_file", {}).get("name") == "song.m4a" and body.get("master_slot_id") is None, (body.get("master_file"), body.get("master_slot_id")))
check("windows are still 0 and 4", [s["window"]["start"] for s in body["slots"] if s["lane"] == "video"] == [0.0, 4.0])
clipf = os.path.join(FAKE_OUT, "file_clip.mp4")
make_clip(clipf, width=CW, height=CH, duration=2.0, audio_freq=1000)
slot_a, job_a, rev4 = generate_slot(URL, sid4, va, FAKE_PORT, clipf)
with open(os.path.join(FAKE_STORE, "last_prompt.json")) as f:
    ga = json.load(f)
name_a = [n for n in ga.values() if n.get("class_type") == "LoadAudio"][0]["inputs"]["audio"]
up_a = os.path.join(FAKE_STORE, "inputs", name_a)
v440 = mean_volume(up_a, trim=(0.0, 1.5), af="bandpass=f=440:w=60,volumedetect")
v880 = mean_volume(up_a, trim=(0.0, 1.5), af="bandpass=f=880:w=100,volumedetect")
check("no offset: the first 1.5 s of the slice is the file's 440 Hz opening", v440 is not None and v880 is not None and v440 - v880 >= 25, (v440, v880))
rev4 = seq_get(URL, sid4)["rev"]
code, body = seq_op(URL, sid4, rev4, "set_master_start", start=2.0)
check("set_master_start 2.0 is accepted and shows in the view", code == 200 and body["master_file"]["start"] == 2.0, (code, body.get("error")))
slot_b, job_b, rev4 = generate_slot(URL, sid4, vb, FAKE_PORT, clipf)
with open(os.path.join(FAKE_STORE, "last_prompt.json")) as f:
    gb = json.load(f)
name_b = [n for n in gb.values() if n.get("class_type") == "LoadAudio"][0]["inputs"]["audio"]
up_b = os.path.join(FAKE_STORE, "inputs", name_b)
w440 = mean_volume(up_b, trim=(0.0, 1.5), af="bandpass=f=440:w=60,volumedetect")
w880 = mean_volume(up_b, trim=(0.0, 1.5), af="bandpass=f=880:w=100,volumedetect")
check("offset 2.0: the slice for shot 2 (window 4) starts at 6 s of the file: 880 Hz, no 440", w440 is not None and w880 is not None and w880 - w440 >= 25, (w440, w880))


print()
print("5. the cut plays the imported file from its start offset; the clips' own audio is dropped")
rev4 = pick(URL, sid4, seq_get(URL, sid4)["rev"], va, job_a)["rev"]
rev4 = pick(URL, sid4, rev4, vb, job_b)["rev"]
code, b = do_cut(URL, sid4)
entry = wait_cut_done(URL, sid4, b.get("cut_id")) if code == 200 else None
check("the audio-led cut with an imported file finishes", bool(entry and entry.get("status") == "done"), (code, b, entry and entry.get("log")))
check("the cut record lists a master shot named after the file", bool(entry and any(s.get("role") == "master" and s.get("name") == "song.m4a" for s in entry.get("shots") or [])), entry and entry.get("shots"))
out = cut_file_path(DATA_DIR, sid4, entry, b.get("cut_id")) if entry else None
c440 = mean_volume(out, trim=(0.3, 1.5), af="bandpass=f=440:w=60,volumedetect") if out else None
c880 = mean_volume(out, trim=(0.3, 1.5), af="bandpass=f=880:w=100,volumedetect") if out else None
c1000 = mean_volume(out, trim=(0.3, 1.5), af="bandpass=f=1000:w=100,volumedetect") if out else None
check("offset 2.0: the cut's first seconds are the file's 880 Hz (not its 440 opening, not the clips' 1 kHz)",
      None not in (c440, c880, c1000) and c880 - c440 >= 25 and c880 - c1000 >= 25, (c440, c880, c1000))


print()
print("6. an imported file wins over a picked sound shot; clearing falls back")
snd = add_sound_slot(URL, sid4, seq_get(URL, sid4)["rev"], seconds=10.0)["slots"][-1]["id"]
slot_m = os.path.join(FAKE_OUT, "slot_master.m4a")
make_two_tone(slot_m, 440, 5.0, 440, 5.0)
_, job_s, rev4 = generate_slot(URL, sid4, snd, FAKE_PORT, slot_m)
rev4 = pick(URL, sid4, rev4, snd, job_s)["rev"]
code, b = do_cut(URL, sid4)
entry = wait_cut_done(URL, sid4, b.get("cut_id")) if code == 200 else None
out = cut_file_path(DATA_DIR, sid4, entry, b.get("cut_id")) if entry else None
d440 = mean_volume(out, trim=(0.3, 1.5), af="bandpass=f=440:w=60,volumedetect") if out else None
d880 = mean_volume(out, trim=(0.3, 1.5), af="bandpass=f=880:w=100,volumedetect") if out else None
check("with both present the imported file leads (880 Hz, not the slot's 440)", None not in (d440, d880) and d880 - d440 >= 25, (d440, d880))
code, body = seq_op(URL, sid4, seq_get(URL, sid4)["rev"], "clear_master")
check("clear_master removes the key", code == 200 and "master" not in body and "master" not in stored(DATA_DIR, sid4), (code, body.get("error")))
code, b = do_cut(URL, sid4)
entry = wait_cut_done(URL, sid4, b.get("cut_id")) if code == 200 else None
out = cut_file_path(DATA_DIR, sid4, entry, b.get("cut_id")) if entry else None
e440 = mean_volume(out, trim=(0.3, 1.5), af="bandpass=f=440:w=60,volumedetect") if out else None
e880 = mean_volume(out, trim=(0.3, 1.5), af="bandpass=f=880:w=100,volumedetect") if out else None
check("after clearing, the picked sound shot leads again (440 Hz)", None not in (e440, e880) and e440 - e880 >= 25, (e440, e880))


print()
print("7. audio-led off: an imported file changes nothing about a classic cut")
s7 = seq_create(URL, "off")
sid7 = s7["id"]
post_master(URL, sid7, "song.m4a", MASTER_BYTES)
body = add_video_slot(URL, sid7, seq_get(URL, sid7)["rev"], length=97, prompt="o1")
v7 = body["slots"][0]["id"]
slot7, job7, rev7 = generate_slot(URL, sid7, v7, FAKE_PORT, clipf)
rev7 = pick(URL, sid7, rev7, v7, job7)["rev"]
code, b = do_cut(URL, sid7)
entry7 = wait_cut_done(URL, sid7, b.get("cut_id")) if code == 200 else None
out7 = cut_file_path(DATA_DIR, sid7, entry7, b.get("cut_id")) if entry7 else None
o1000 = band_volume(out7, 1000, 100) if out7 and os.path.isfile(out7) else None
check("the classic cut keeps the clip's own 1 kHz audio", o1000 is not None and o1000 > -40, o1000)
check("and the record has no audio_led / master shot", bool(entry7) and "audio_led" not in entry7 and not any(s.get("role") == "master" for s in entry7.get("shots") or []), entry7 and entry7.get("shots"))


print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
