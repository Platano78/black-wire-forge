"""Frozen acceptance gate for C3.6 -- the cut (the internal cut-feature commission doc,
the internal sequence/storyboard design spec Sections 1, 5, 6, 7, rules R1-R10).

Written BEFORE the feature exists, against an earlier version of server.py, which has
no can_cut/cut_reason/can_title/title_reason/trim_effective, no
/api/sequence/cut, and no /api/sequence/file route. Every check below is
therefore expected to FAIL (or error out) on the current tree -- that is
recorded as RED in the commission's report, not treated as a test bug. The
maker of C3.6 may NOT edit this file (commission "Do not grind").

Calibrated on ffmpeg 4.4.2 (the oldest version the cut pipeline supports);
it also passes on ffmpeg 8. 4.4.2 has no -fps_mode, so the CFR fix used
instead (`-vsync cfr -r 24`) works on both. Run it locally:

    python3 tests/test_cut.py

ISOLATION: every byte this test writes goes under a fresh tempfile.mkdtemp()
directory; GENCENTER_DATA/GENCENTER_CONFIG point there before server.py is
ever spawned, the same way tests/test_sequence_ui.py does it (a REAL
server.py subprocess + a REAL tests/fixtures/fake_comfy.py subprocess, both
on random free local ports, job_poll_seconds fast so job_poller's own
harvest runs for real). The owner's data/ tree, ~/black-wire-forge and its
port 3998 are never touched by this file.

CALIBRATION -- every numeric threshold below was measured against a real
ffmpeg 4.4.2 build with raw ffmpeg commands (scratch script, not committed),
BOTH DIRECTIONS (a correct-build number and a wrong-build number), so a
threshold that could never fail is not shipped here.

  T (loudness, R4) -- single loudnorm over the whole joined track must keep a
  loud shot audibly louder than a quiet shot; per-clip loudnorm (the WRONG
  build) collapses that gap to ~0.
    Pass case (single loudnorm over a loud+quiet concat, sine 1kHz 0dB then
    sine 1kHz -20dB, 3s each, 48kHz mono):
      loud segment mean_volume -15.90 dB, quiet segment -35.80 dB, diff 19.90 dB
    Fail case (each clip loudnorm'd independently to I=-16 THEN concatenated):
      loud segment -16.00 dB, quiet segment -16.00 dB, diff 0.00 dB
    ffmpeg -f lavfi -i sine=frequency=1000:duration=3:sample_rate=48000 \
        -af volume=0dB -ac 1 loud_raw.wav          (and volume=-20dB for quiet_raw.wav)
    ffmpeg -i loud_raw.wav -i quiet_raw.wav -filter_complex \
        "[0:a][1:a]concat=n=2:v=0:a=1" raw_concat.wav
    ffmpeg -i raw_concat.wav -af loudnorm=I=-16:TP=-1.0:LRA=11 single_norm.wav
    ffmpeg -i <clip> -af volumedetect -f null -   (mean_volume, per 3s segment via -ss/-t)
    LOUDNESS_DIFF_MIN = 8.0 dB (between the 0.0 fail and the 19.9 pass, wide margin)

  T2 (bed gain, R7) -- with the pipeline's mix-then-loudnorm-once order, a
  -18dB bed against a 0dB-source clip lands the bed's band ~18dB under the
  clip's band; a wrong build that never attenuates the bed (0dB bed) lands
  at ~0dB, off by ~18dB.
    Pass case (clip=1kHz sine, bed=440Hz sine, same source level, bed mixed
    at -18dB via `[1:a]volume=-18dB[bed];[0:a][bed]amix=inputs=2:duration=first:normalize=0`,
    then loudnorm=I=-16:TP=-1.0:LRA=11 over the mix, exactly the app's own order):
      1kHz band -19.10 dB, 440Hz band -37.10 dB, diff 18.00 dB
    Fail case (bed mixed at 0dB, i.e. never attenuated), same loudnorm after:
      1kHz band -21.70 dB, 440Hz band -21.80 dB, diff 0.10 dB
    ffmpeg -i <mix> -af "bandpass=f=1000:w=100,volumedetect" -f null -   (and f=440:w=80)
    BED_DIFF_TARGET = 18.0 dB, BED_DIFF_TOL = 4.0 dB (pass=18.0 -> |0.0|<=4;
    fail=0.10 -> |17.9|>4, wide margin both sides)

  Title frame diff (R6) -- "meaningfully differs, not encoder noise":
    Re-encode noise floor (same source, two INDEPENDENT x264 encodes, no
    title, same timestamp; mean per-channel abs diff over every pixel,
    computed with Pillow): 0.0000
    Titled vs untitled at the SAME timestamp, INSIDE the title's [1,2)s
    window (640x360 testsrc, drawtext "A Title" fontsize 36 centred): 0.5301
    Titled vs untitled OUTSIDE the window (t=2.5s, two different encode
    pipelines -- the more realistic noise floor a real build's outside-window
    frame is compared against): 0.1324
    ffmpeg -f lavfi -i testsrc=size=640x360:rate=24:duration=3 -c:v libx264 -pix_fmt yuv420p base.mp4
    ffmpeg -i base.mp4 -vf "drawtext=fontfile=.../DejaVuSans.ttf:text='A Title':
        x=(w-text_w)/2:y=(h-text_h)/2:fontsize=36:fontcolor=white:enable='between(t,1,2)'"
        -c:v libx264 -pix_fmt yuv420p titled.mp4
    ffmpeg -ss <t> -i <clip> -frames:v 1 frame.png
    TITLE_DIFF_MIN = 0.25 (between the 0.13 outside-window noise and the 0.53
    inside-window signal)
    TITLE_DIFF_MAX_OUTSIDE = 0.22 (below the 0.13 measured cross-encode noise
    at an untouched frame, with margin; a real build compares its OWN
    untitled-vs-titled cut, which should be quieter than two independent
    from-scratch encodes were)

  FPS (R4) -- deterministic, no tolerance needed: `-c copy` concat of a
  24fps clip + a 30fps clip reports r_frame_rate=24/1 but avg_frame_rate=108/5
  (NOT constant); `-vsync cfr -r 24` concat reports both r_frame_rate and
  avg_frame_rate as 24/1. The frozen check below asserts BOTH fields equal
  24/1, which a `-c copy` build cannot satisfy.

DO NOT EDIT THIS FILE to make a build pass. If a rule turns out impossible or
contradictory, that is the builder's finding to report, not this file's bug
to fix.

Every check() call is independent -- one failure never aborts the rest.
"""
import copy
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


# ---------------------------------------------------------------------------
# Bring-up: one fake lane, one real server.py subprocess, fast job polling
# so the server's own harvest runs for real inside each wait_true().
# ---------------------------------------------------------------------------

SCRATCH = tempfile.mkdtemp(prefix="bwf_cut_")
print("scratch dir: %s (real data/ and the live ~/black-wire-forge are never touched)" % SCRATCH)

import atexit
def _cleanup():
    for p in PROCS:
        stop(p)
    shutil.rmtree(SCRATCH, ignore_errors=True)
atexit.register(_cleanup)

FAKE_PROC, FAKE_PORT, FAKE_STORE = start_fake_lane(SCRATCH, "t")
FAKE_OUT = os.path.join(FAKE_STORE, "outputs")

FONT_CFG = {"cut": {"fontfile": FONT}}
_srv_proc, URL, DATA_DIR, _srv_log = start_server(SCRATCH, FAKE_PORT, name="main", cut_config=FONT_CFG)
capture_unknown_route_body(URL)

# Canvas: whatever default_canvas() picked (first video mode in pack order
# with integer width/height defaults) -- read it back rather than assume it.
_probe_seq = seq_create(URL, "canvas probe")
CANVAS_W, CANVAS_H = _probe_seq["canvas"]["width"], _probe_seq["canvas"]["height"]
note("sequence canvas is %dx%d" % (CANVAS_W, CANVAS_H))


def start_server_with_path(scratch, lane_port, name, path_value, job_poll_seconds=0.3):
    """Same as start_server, but with PATH overridden for the subprocess --
    R1's own check: ffmpeg/ffprobe made undiscoverable via shutil.which()
    while python itself is launched by its own absolute path (sys.executable),
    so PATH never has to include it."""
    data_dir = os.path.join(scratch, "data_%s" % name)
    cfg_path = os.path.join(scratch, "config_%s.json" % name)
    port = free_port()
    cfg = {"title": "C3.6 cut test (no ffmpeg)", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Target lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"]}],
           "timing": {"poll_seconds": 0.3, "job_poll_seconds": job_poll_seconds,
                      "http_timeout": 5.0, "free_settle_seconds": 1.0, "discover_seconds": 300.0}}
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    logf = open(os.path.join(scratch, "server_%s.log" % name), "w")
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data_dir, PATH=path_value)
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                             stdout=logf, stderr=subprocess.STDOUT)
    PROCS.append(proc)
    url = "http://127.0.0.1:%d/" % port
    ok, _ = wait_true("no-ffmpeg server %s is up" % name, lambda: http_json(url + "api/health")[0] == 200, 20)
    if not ok:
        with open(os.path.join(scratch, "server_%s.log" % name)) as f:
            note("no-ffmpeg server log tail: %s" % f.read()[-1500:])
    return proc, url, data_dir


# ---------------------------------------------------------------------------
print("R1: can_cut / cut_reason with ffmpeg absent from PATH; POST /cut refused")

EMPTY_BIN = os.path.join(SCRATCH, "empty_bin")
os.makedirs(EMPTY_BIN, exist_ok=True)   # a PATH dir with nothing in it -- no ffmpeg, no ffprobe
FAKE_PROC2, FAKE_PORT2, FAKE_STORE2 = start_fake_lane(SCRATCH, "noffmpeg")
_proc_nf, URL_NF, _data_nf = start_server_with_path(SCRATCH, FAKE_PORT2, "noffmpeg", EMPTY_BIN)
capture_unknown_route_body(URL_NF)

seq_nf = seq_create(URL_NF, "no ffmpeg")
check("GET /api/sequence carries can_cut: false when ffmpeg/ffprobe are not on PATH",
      seq_nf.get("can_cut") is False, seq_nf.get("can_cut"))
reason_nf = seq_nf.get("cut_reason")
check("cut_reason is a non-empty sentence naming what is missing",
      isinstance(reason_nf, str) and len(reason_nf.strip()) > 0, reason_nf)

code_nf, body_nf = do_cut(URL_NF, seq_nf["id"])
check("POST /api/sequence/cut refused (a REAL refusal, not the generic 404) while can_cut is false",
      refused(URL_NF, code_nf, body_nf), (code_nf, body_nf, UNKNOWN_ROUTE_BODY.get(URL_NF)))
check("the refusal carries the SAME sentence as cut_reason",
      isinstance(body_nf, dict) and body_nf.get("error") == reason_nf, (body_nf, reason_nf))


# ---------------------------------------------------------------------------
print("R2: local files only -- zero-pick refusal, unpicked exclusion+naming, cut-time harvest retry")

ZERO_PICKS_KEYWORDS = ("pick", "shot", "video", "slot")   # a loose, documented keyword set --
# the exact wording is the builder's, but a refusal for THIS reason should
# plausibly mention one of these nouns, not just be "some 4xx sentence".
seq_zero = seq_create(URL, "R2 zero picks")
add_video_slot(URL, seq_zero["id"], seq_zero["rev"])
code_zero, body_zero = do_cut(URL, seq_zero["id"])
check("zero picked video slots -> refused with a SPECIFIC sentence (mentions %s), not the generic 404"
      % (ZERO_PICKS_KEYWORDS,),
      refused(URL, code_zero, body_zero, keywords=ZERO_PICKS_KEYWORDS),
      (code_zero, body_zero))

seq_r2 = seq_create(URL, "R2 unpicked naming")
after_a = add_video_slot(URL, seq_r2["id"], seq_r2["rev"], prompt="shot A")
slot_a = after_a["slots"][0]["id"]
after_b = add_video_slot(URL, seq_r2["id"], after_a["rev"], prompt="shot B")
slot_b = after_b["slots"][1]["id"]

clip_a = make_clip(os.path.join(FAKE_OUT, "r2_a.mp4"), width=CANVAS_W, height=CANVAS_H, duration=2.0)
slot_a_after, job_a, rev = generate_slot(URL, seq_r2["id"], slot_a, FAKE_PORT, clip_a)
pick(URL, seq_r2["id"], rev, slot_a, job_a)
cur = seq_get(URL, seq_r2["id"])
code_r2, body_r2 = do_cut(URL, seq_r2["id"])
check("one picked video slot + one entirely-untouched slot -> cut is accepted",
      code_r2 == 200 and body_r2.get("ok") and body_r2.get("cut_id"), (code_r2, body_r2))
if code_r2 == 200 and body_r2.get("cut_id"):
    entry = wait_cut_done(URL, seq_r2["id"], body_r2["cut_id"], timeout=90)
    check("that cut reaches status done", entry is not None and entry.get("status") == "done", entry)
    check("the cut's own record NAMES the unpicked slot (its id appears in the cuts[] entry)",
          entry is not None and slot_b in json.dumps(entry), (slot_b, entry))
    check("the cut's record does NOT claim slot B's take (slot A's job id is used, slot B never rendered)",
          entry is not None and job_a in json.dumps(entry), entry)

# --- cut-time harvest retry: force a real "file: null" via timing on a
# SEPARATE slow-poll server, then restore the file before cutting. --------
FAKE_PROC3, FAKE_PORT3, FAKE_STORE3 = start_fake_lane(SCRATCH, "slowpoll")
FAKE_OUT3 = os.path.join(FAKE_STORE3, "outputs")
_proc_sp, URL_SP, _data_sp, _log_sp = start_server(SCRATCH, FAKE_PORT3, name="slowpoll", job_poll_seconds=6.0,
                                                     cut_config=FONT_CFG)
capture_unknown_route_body(URL_SP)
seq_retry = seq_create(URL_SP, "R2 harvest retry")
after_r = add_video_slot(URL_SP, seq_retry["id"], seq_retry["rev"])
slot_r = after_r["slots"][0]["id"]
clip_r = make_clip(os.path.join(FAKE_OUT3, "r2_retry.mp4"), width=CANVAS_W, height=CANVAS_H, duration=2.0)
saved_bytes = open(clip_r, "rb").read()

control_accept(FAKE_PORT3, [{"filename": "r2_retry.mp4", "subfolder": "", "type": "output"}])
code_g, body_g, _ = http_json(URL_SP + "api/sequence/generate", data={"id": seq_retry["id"], "slot_id": slot_r},
                               method="POST")
if code_g == 200 and body_g.get("ok"):
    job_r = body_g["job"]["id"]
    os.remove(clip_r)   # the fake lane's /view now 404s for this filename
    note("removed the fake lane's output file immediately after generate; job_poll_seconds=6, "
         "waiting past one poll tick so the FIRST (non-retried) harvest attempt fails naturally")
    time.sleep(7.5)

    def take_file_is_null():
        seq = seq_get(URL_SP, seq_retry["id"])
        slot = next(s for s in seq["slots"] if s["id"] == slot_r)
        take = next(t for t in slot["takes"] if t["job_id"] == job_r)
        return take.get("file") is None

    got_null, _ = wait_true("(setup) the take's file is null after the fake lane's output vanished pre-harvest",
                             take_file_is_null, 3, interval=0.5)
    if got_null:
        with open(clip_r, "wb") as f:
            f.write(saved_bytes)   # restore -- the SOURCE is back, only the cut can retry the copy now
        rev_r = seq_get(URL_SP, seq_retry["id"])["rev"]
        pick(URL_SP, seq_retry["id"], rev_r, slot_r, job_r)
        code_c, body_c = do_cut(URL_SP, seq_retry["id"])
        check("cut accepted with the take's file still null but its source restored",
              code_c == 200 and body_c.get("ok"), (code_c, body_c))
        if code_c == 200:
            entry_r = wait_cut_done(URL_SP, seq_retry["id"], body_c["cut_id"], timeout=90)
            check("R2 cut-time retry: the cut reaches status done (it re-copied the take)",
                  entry_r is not None and entry_r.get("status") == "done", entry_r)
            seq_after_retry = seq_get(URL_SP, seq_retry["id"])
            slot_after_retry = next(s for s in seq_after_retry["slots"] if s["id"] == slot_r)
            take_after_retry = next(t for t in slot_after_retry["takes"] if t["job_id"] == job_r)
            check("R2 cut-time retry: the take's file is set again after the cut ran",
                  take_after_retry.get("file") is not None, take_after_retry)
    else:
        check("(setup) could not force a null take.file via timing -- see report", False,
              "R2's cut-time-retry check could not be exercised without a timing race; "
              "see report section 2")
else:
    check("(setup) seq_generate for the harvest-retry check", False, (code_g, body_g))

# --- still missing at cut time (source never restored) -> refused, naming the shot ---
seq_stillmissing = seq_create(URL_SP, "R2 still missing")
after_sm = add_video_slot(URL_SP, seq_stillmissing["id"], seq_stillmissing["rev"])
slot_sm = after_sm["slots"][0]["id"]
clip_sm = make_clip(os.path.join(FAKE_OUT3, "r2_missing.mp4"), width=CANVAS_W, height=CANVAS_H, duration=2.0)
control_accept(FAKE_PORT3, [{"filename": "r2_missing.mp4", "subfolder": "", "type": "output"}])
code_gm, body_gm, _ = http_json(URL_SP + "api/sequence/generate",
                                 data={"id": seq_stillmissing["id"], "slot_id": slot_sm}, method="POST")
if code_gm == 200 and body_gm.get("ok"):
    job_sm = body_gm["job"]["id"]
    os.remove(clip_sm)
    time.sleep(7.5)
    seq_sm = seq_get(URL_SP, seq_stillmissing["id"])
    slot_sm_state = next(s for s in seq_sm["slots"] if s["id"] == slot_sm)
    take_sm = next(t for t in slot_sm_state["takes"] if t["job_id"] == job_sm)
    if take_sm.get("file") is None:
        rev_sm = seq_sm["rev"]
        pick(URL_SP, seq_stillmissing["id"], rev_sm, slot_sm, job_sm)
        code_stillm, body_stillm = do_cut(URL_SP, seq_stillmissing["id"])
        check("still missing at cut time (source never restored) -> refused, not accepted "
              "(a REAL refusal, not the generic 404)",
              refused(URL_SP, code_stillm, body_stillm), (code_stillm, body_stillm))
        check("the refusal names the shot (slot id appears in the error)",
              isinstance(body_stillm, dict) and slot_sm in json.dumps(body_stillm), body_stillm)
    else:
        check("(setup) still-missing scenario: take.file was unexpectedly set", False, take_sm)
else:
    check("(setup) seq_generate for the still-missing check", False, (code_gm, body_gm))
control_accept(FAKE_PORT3, None)


# ---------------------------------------------------------------------------
print("R3: trims -- null=whole take, tail-keep from PROBED duration, refusals never clamped, trim_effective")

COLORS4 = ("red", "blue", "green", "yellow")
seq_r3 = seq_create(URL, "R3 trims")
after_r3 = add_video_slot(URL, seq_r3["id"], seq_r3["rev"])
slot_r3 = after_r3["slots"][0]["id"]
clip_r3 = os.path.join(FAKE_OUT, "r3_colorchange.mp4")
make_color_change_clip(clip_r3, width=CANVAS_W, height=CANVAS_H, colors=COLORS4, duration=4.0)
slot_after_r3, job_r3, rev_r3 = generate_slot(URL, seq_r3["id"], slot_r3, FAKE_PORT, clip_r3)
D_r3 = probed_duration(os.path.join(DATA_DIR, "seq", seq_r3["id"], slot_after_r3["takes"][0]["file"]))
note("R3 clip probed duration D=%.3fs (4 one-second colour segments: %s)" % (D_r3, COLORS4))

rev_r3 = pick(URL, seq_r3["id"], rev_r3, slot_r3, job_r3)["rev"]

seq_check = seq_get(URL, seq_r3["id"])
slot_check = next(s for s in seq_check["slots"] if s["id"] == slot_r3)
eff_null = slot_check.get("trim_effective")
check("trim_effective for a null trim is the whole take (in=0, len=D, +/-0.1s)",
      isinstance(eff_null, dict) and abs(eff_null.get("in", -1)) < 0.1 and abs(eff_null.get("len", -1) - D_r3) < 0.1,
      (eff_null, D_r3))

code_tk, body_tk = set_trim(URL, seq_r3["id"], rev_r3, slot_r3, {"in": None, "len": 2.0})
check("set_trim {in: null, len: 2.0} accepted", code_tk == 200, body_tk)
rev_r3 = body_tk.get("rev", rev_r3)
seq_check2 = seq_get(URL, seq_r3["id"])
slot_check2 = next(s for s in seq_check2["slots"] if s["id"] == slot_r3)
eff_tk = slot_check2.get("trim_effective")
expect_in = D_r3 - 2.0
check("trim_effective for tail-keep: in = D - len computed from the PROBED duration (+/-0.15s)",
      isinstance(eff_tk, dict) and abs(eff_tk.get("in", -999) - expect_in) < 0.15 and abs(eff_tk.get("len", -999) - 2.0) < 0.05,
      (eff_tk, expect_in))

# Cut with just this tail-keep slot picked; the FIRST frame of the result
# must show the tail's colour (green, seg index 2 at t=[2,4)), never the
# head's colour (red, seg index 0) -- proves the trim by CONTENT.
code_cut3, body_cut3 = do_cut(URL, seq_r3["id"])
if code_cut3 == 200 and body_cut3.get("ok"):
    entry3 = wait_cut_done(URL, seq_r3["id"], body_cut3["cut_id"], timeout=90)
    check("R3 tail-keep cut reaches status done", entry3 is not None and entry3.get("status") == "done", entry3)
    if entry3 and entry3.get("status") == "done":
        out_path3 = cut_file_path(DATA_DIR, seq_r3["id"], entry3, body_cut3["cut_id"])
        r, g, b = frame_dominant_color(out_path3, 0.05)
        check("the cut's first frame is the TAIL colour (green: g dominant), not the head (red)",
              g > r and g > b, (r, g, b))
else:
    check("R3 tail-keep cut accepted", False, (code_cut3, body_cut3))

# --- refusals: never clamped ------------------------------------------------
seq_r3b = seq_create(URL, "R3 refusals")
after_r3b = add_video_slot(URL, seq_r3b["id"], seq_r3b["rev"])
slot_r3b = after_r3b["slots"][0]["id"]
clip_r3b = os.path.join(FAKE_OUT, "r3_refuse.mp4")
make_clip(clip_r3b, width=CANVAS_W, height=CANVAS_H, duration=4.0)
slot_after_r3b, job_r3b, rev_r3b = generate_slot(URL, seq_r3b["id"], slot_r3b, FAKE_PORT, clip_r3b)
rev_r3b = pick(URL, seq_r3b["id"], rev_r3b, slot_r3b, job_r3b)["rev"]
D_r3b = probed_duration(os.path.join(DATA_DIR, "seq", seq_r3b["id"], slot_after_r3b["takes"][0]["file"]))
note("R3 refusal clip probed duration D=%.3fs" % D_r3b)


def _number_forms(value):
    """1 or 2 decimals, or a bare int -- whichever way a build chose to
    format the sentence's numbers, any of these counts as "the number
    appears"."""
    forms = {"%d" % value, "%.1f" % value, "%.2f" % value}
    if float(value).is_integer():
        forms.add(str(int(value)))
    return forms


def _sentence_names_shot_and_numbers(text, shot_id, *values):
    if shot_id not in text:
        return False
    for v in values:
        if not any(f in text for f in _number_forms(v)):
            return False
    return True


for label, trim in (
    ("explicit len > D", {"in": 1, "len": 10}),
    ("explicit in+len > D", {"in": 3, "len": 3}),
    ("tail-keep len > D (computed in < 0)", {"in": None, "len": 10}),
):
    rev_now = seq_get(URL, seq_r3b["id"])["rev"]
    code_t, body_t = set_trim(URL, seq_r3b["id"], rev_now, slot_r3b, trim)
    check("set_trim(%s) accepted (trim itself is only checked against D at CUT time)" % label,
          code_t == 200, body_t)
    rev_now = body_t.get("rev", rev_now)
    code_ct, body_ct = do_cut(URL, seq_r3b["id"])
    check("cut REFUSED for %s (never clamped, a REAL refusal not the generic 404)" % label,
          refused(URL, code_ct, body_ct), (label, code_ct, body_ct))
    if refused(URL, code_ct, body_ct):
        check("cut refusal for %s names the shot AND both numbers (requested length=%.1f, probed D=%.2f)"
              % (label, trim["len"], D_r3b),
              _sentence_names_shot_and_numbers(body_ct["error"], slot_r3b, trim["len"], D_r3b),
              body_ct["error"])
    if code_ct == 200:
        # If a build actually accepted this and queued a cut, it did not
        # refuse -- give the background thread no chance to leave a stray
        # cuts[] entry other checks might trip over.
        wait_cut_done(URL, seq_r3b["id"], body_ct.get("cut_id", ""), timeout=30)
    set_trim(URL, seq_r3b["id"], seq_get(URL, seq_r3b["id"])["rev"], slot_r3b, None)   # reset for the next case


# ---------------------------------------------------------------------------
print("R4: output file -- CFR 24, frame count, audio present+in-sync, no metadata survives, "
      "true peak, loudness normalised ONCE over the whole join")

MARKER = "SECRETRECIPEMARKER_do_not_leak"
seq_r4 = seq_create(URL, "R4 output file")
after_a4 = add_video_slot(URL, seq_r4["id"], seq_r4["rev"], prompt="loud shot")
slot_a4 = after_a4["slots"][0]["id"]
after_b4 = add_video_slot(URL, seq_r4["id"], after_a4["rev"], prompt="quiet shot")
slot_b4 = after_b4["slots"][1]["id"]

clip_a4 = os.path.join(FAKE_OUT, "r4_loud.mp4")
make_clip(clip_a4, width=CANVAS_W, height=CANVAS_H, duration=3.0, volume_db=0,
          format_marker=MARKER, stream_marker=MARKER)
clip_b4 = os.path.join(FAKE_OUT, "r4_quiet.mp4")
make_clip(clip_b4, width=CANVAS_W, height=CANVAS_H, duration=3.0, volume_db=-20,
          format_marker=MARKER, stream_marker=MARKER)

slot_a4_after, job_a4, rev4 = generate_slot(URL, seq_r4["id"], slot_a4, FAKE_PORT, clip_a4)
slot_b4_after, job_b4, rev4 = generate_slot(URL, seq_r4["id"], slot_b4, FAKE_PORT, clip_b4)
rev4 = pick(URL, seq_r4["id"], rev4, slot_a4, job_a4)["rev"]
rev4 = pick(URL, seq_r4["id"], rev4, slot_b4, job_b4)["rev"]

D_a4 = probed_duration(os.path.join(DATA_DIR, "seq", seq_r4["id"], slot_a4_after["takes"][0]["file"]))
D_b4 = probed_duration(os.path.join(DATA_DIR, "seq", seq_r4["id"], slot_b4_after["takes"][0]["file"]))
total_len_r4 = D_a4 + D_b4

code_r4, body_r4 = do_cut(URL, seq_r4["id"])
check("R4 cut accepted", code_r4 == 200 and body_r4.get("ok"), (code_r4, body_r4))
if code_r4 == 200 and body_r4.get("ok"):
    entry4 = wait_cut_done(URL, seq_r4["id"], body_r4["cut_id"], timeout=120)
    check("R4 cut reaches status done", entry4 is not None and entry4.get("status") == "done", entry4)
    if entry4 and entry4.get("status") == "done":
        out4 = cut_file_path(DATA_DIR, seq_r4["id"], entry4, body_r4["cut_id"])
        check("R4 output file exists on disk", os.path.isfile(out4), out4)
        if os.path.isfile(out4):
            info4 = ffprobe_json(out4, ["-show_streams", "-show_format"])
            v4 = video_stream(info4)
            a4 = audio_stream(info4)
            check("r_frame_rate == 24/1", v4 is not None and v4.get("r_frame_rate") == "24/1", v4 and v4.get("r_frame_rate"))
            check("avg_frame_rate == 24/1", v4 is not None and v4.get("avg_frame_rate") == "24/1", v4 and v4.get("avg_frame_rate"))
            nframes4 = counted_frames(out4)
            expect_frames4 = round(24 * total_len_r4)
            check("video frame count == round(24 * sum(len)) +/-1 (got %r, expected ~%d)" % (nframes4, expect_frames4),
                  nframes4 is not None and abs(nframes4 - expect_frames4) <= 1, (nframes4, expect_frames4))
            check("an audio stream is present", a4 is not None, info4.get("streams"))
            if a4:
                vdur = float(v4.get("duration") or info4["format"]["duration"])
                adur = float(a4.get("duration") or info4["format"]["duration"])
                check("audio duration within 0.1s of video duration", abs(vdur - adur) <= 0.1, (vdur, adur))
            blob = all_tag_blob(info4)
            check("no planted metadata marker survives anywhere in ffprobe's format+stream output",
                  MARKER.lower() not in blob, None)
            check("no tag key/value anywhere contains 'prompt' or 'comment'",
                  "prompt" not in blob and "comment" not in blob, None)
            tp4 = true_peak_dbtp(out4)
            check("true peak <= -1.0 dBTP", tp4 is not None and tp4 <= -1.0, tp4)
            v_loud = mean_volume(out4, trim=(0, min(D_a4, 2.5)))
            v_quiet = mean_volume(out4, trim=(D_a4 + 0.1, min(D_b4 - 0.2, 2.5)))
            diff4 = (v_loud - v_quiet) if (v_loud is not None and v_quiet is not None) else None
            check("loudness normalised ONCE over the whole join: the loud shot stays >= %.1fdB "
                  "louder than the quiet shot (got %r)" % (LOUDNESS_DIFF_MIN, diff4),
                  diff4 is not None and diff4 >= LOUDNESS_DIFF_MIN, (v_loud, v_quiet, diff4))


# ---------------------------------------------------------------------------
print("R5: mixed inputs -- no-audio gets silence, mixed fps/audio-format cut cleanly, wrong picture size refused")

seq_r5 = seq_create(URL, "R5 mixed inputs")
s_noaudio = add_video_slot(URL, seq_r5["id"], seq_r5["rev"], prompt="no audio shot")["slots"][0]["id"]
rev5 = seq_get(URL, seq_r5["id"])["rev"]
s_30fps = add_video_slot(URL, seq_r5["id"], rev5, prompt="30fps shot")["slots"][1]["id"]
rev5 = seq_get(URL, seq_r5["id"])["rev"]
s_44mono = add_video_slot(URL, seq_r5["id"], rev5, prompt="44.1kHz mono shot")["slots"][2]["id"]

clip_noaudio = os.path.join(FAKE_OUT, "r5_noaudio.mp4")
make_clip(clip_noaudio, width=CANVAS_W, height=CANVAS_H, duration=2.0, audio=False)
clip_30fps = os.path.join(FAKE_OUT, "r5_30fps.mp4")
make_clip(clip_30fps, width=CANVAS_W, height=CANVAS_H, fps=30, duration=2.0)
clip_44mono = os.path.join(FAKE_OUT, "r5_44mono.mp4")
make_clip(clip_44mono, width=CANVAS_W, height=CANVAS_H, duration=2.0, sample_rate=44100, channels=1)

slot_na, job_na, rev5 = generate_slot(URL, seq_r5["id"], s_noaudio, FAKE_PORT, clip_noaudio)
slot_30, job_30, rev5 = generate_slot(URL, seq_r5["id"], s_30fps, FAKE_PORT, clip_30fps)
slot_44, job_44, rev5 = generate_slot(URL, seq_r5["id"], s_44mono, FAKE_PORT, clip_44mono)
rev5 = pick(URL, seq_r5["id"], rev5, s_noaudio, job_na)["rev"]
rev5 = pick(URL, seq_r5["id"], rev5, s_30fps, job_30)["rev"]
rev5 = pick(URL, seq_r5["id"], rev5, s_44mono, job_44)["rev"]

code_r5, body_r5 = do_cut(URL, seq_r5["id"])
check("R5 mixed-input cut accepted", code_r5 == 200 and body_r5.get("ok"), (code_r5, body_r5))
if code_r5 == 200 and body_r5.get("ok"):
    entry5 = wait_cut_done(URL, seq_r5["id"], body_r5["cut_id"], timeout=120)
    check("R5 mixed-input cut reaches status done (not error)", entry5 is not None and entry5.get("status") == "done", entry5)
    if entry5 and entry5.get("status") == "done":
        out5 = cut_file_path(DATA_DIR, seq_r5["id"], entry5, body_r5["cut_id"])
        info5 = ffprobe_json(out5, ["-show_streams"])
        v5, a5 = video_stream(info5), audio_stream(info5)
        check("R5 output still r_frame_rate == avg_frame_rate == 24/1",
              v5 is not None and v5.get("r_frame_rate") == "24/1" and v5.get("avg_frame_rate") == "24/1", v5)
        check("R5 output still carries an audio stream (silence filled the no-audio clip)", a5 is not None, info5)

seq_r5b = seq_create(URL, "R5 wrong picture size")
s_wrongsize = add_video_slot(URL, seq_r5b["id"], seq_r5b["rev"], prompt="wrong size shot")["slots"][0]["id"]
clip_wrongsize = os.path.join(FAKE_OUT, "r5_wrongsize.mp4")
make_clip(clip_wrongsize, width=CANVAS_W + 64, height=CANVAS_H + 64, duration=2.0)
slot_ws, job_ws, rev5b = generate_slot(URL, seq_r5b["id"], s_wrongsize, FAKE_PORT, clip_wrongsize)
rev5b = pick(URL, seq_r5b["id"], rev5b, s_wrongsize, job_ws)["rev"]
code_ws, body_ws = do_cut(URL, seq_r5b["id"])
check("a picked take whose picture size differs from the canvas -> refused (not a background job, "
      "not the generic 404)",
      refused(URL, code_ws, body_ws), (code_ws, body_ws))
check("the refusal names the shot (slot id in the error)",
      isinstance(body_ws, dict) and s_wrongsize in json.dumps(body_ws), body_ws)
if code_ws == 200:
    wait_cut_done(URL, seq_r5b["id"], body_ws.get("cut_id", ""), timeout=30)


# ---------------------------------------------------------------------------
print("R6: titles -- burned in at cut time, missing fontfile -> can_title false + refusal, special chars render")

seq_r6 = seq_create(URL, "R6 titles")
s_title = add_video_slot(URL, seq_r6["id"], seq_r6["rev"], prompt="titled shot")["slots"][0]["id"]
clip_r6 = os.path.join(FAKE_OUT, "r6_title.mp4")
make_clip(clip_r6, width=CANVAS_W, height=CANVAS_H, duration=4.0)
slot_r6, job_r6, rev6 = generate_slot(URL, seq_r6["id"], s_title, FAKE_PORT, clip_r6)
rev6 = pick(URL, seq_r6["id"], rev6, s_title, job_r6)["rev"]

seq_r6_state = seq_get(URL, seq_r6["id"])
check("can_title is true (config.cut.fontfile is set to a real DejaVu Sans path)",
      seq_r6_state.get("can_title") is True, seq_r6_state.get("can_title"))

TITLE_TEXT = "It's ' : % \\ , a Title"
code_tc, body_tc = set_title_card(URL, seq_r6["id"], rev6, s_title, {"text": TITLE_TEXT, "at": 1.0, "dur": 1.0})
check("set_title_card with punctuation ' : %% \\\\ , accepted", code_tc == 200, body_tc)
rev6 = body_tc.get("rev", rev6)

code_ct6, body_ct6 = do_cut(URL, seq_r6["id"])
check("R6 titled cut accepted", code_ct6 == 200 and body_ct6.get("ok"), (code_ct6, body_ct6))
titled_out = None
if code_ct6 == 200 and body_ct6.get("ok"):
    entry6 = wait_cut_done(URL, seq_r6["id"], body_ct6["cut_id"], timeout=120)
    check("R6 titled cut reaches status done (special chars did not break/inject into the filter graph)",
          entry6 is not None and entry6.get("status") == "done", entry6)
    if entry6 and entry6.get("status") == "done":
        titled_out = cut_file_path(DATA_DIR, seq_r6["id"], entry6, body_ct6["cut_id"])
        check("titled cut output file exists", os.path.isfile(titled_out), titled_out)

# An UNTITLED cut of the identical shot, for a same-timestamp diff -----------
seq_r6u = seq_create(URL, "R6 untitled control")
s_untitled = add_video_slot(URL, seq_r6u["id"], seq_r6u["rev"], prompt="titled shot")["slots"][0]["id"]
slot_r6u, job_r6u, rev6u = generate_slot(URL, seq_r6u["id"], s_untitled, FAKE_PORT, clip_r6)
rev6u = pick(URL, seq_r6u["id"], rev6u, s_untitled, job_r6u)["rev"]
code_ctu, body_ctu = do_cut(URL, seq_r6u["id"])
untitled_out = None
if code_ctu == 200 and body_ctu.get("ok"):
    entry6u = wait_cut_done(URL, seq_r6u["id"], body_ctu["cut_id"], timeout=120)
    if entry6u and entry6u.get("status") == "done":
        untitled_out = cut_file_path(DATA_DIR, seq_r6u["id"], entry6u, body_ctu["cut_id"])

if titled_out and os.path.isfile(titled_out) and untitled_out and os.path.isfile(untitled_out):
    f_titled_mid = os.path.join(SCRATCH, "r6_titled_mid.png")
    f_untitled_mid = os.path.join(SCRATCH, "r6_untitled_mid.png")
    extract_frame_png(titled_out, 1.5, f_titled_mid)     # inside [1, 2)
    extract_frame_png(untitled_out, 1.5, f_untitled_mid)
    diff_mid = png_mean_abs_diff(f_titled_mid, f_untitled_mid)
    check("titled frame INSIDE the title window differs MEANINGFULLY from the untitled equivalent "
          "(diff=%.4f >= %.2f)" % (diff_mid, TITLE_DIFF_MIN), diff_mid >= TITLE_DIFF_MIN, diff_mid)

    f_titled_out = os.path.join(SCRATCH, "r6_titled_out.png")
    f_untitled_out = os.path.join(SCRATCH, "r6_untitled_out.png")
    extract_frame_png(titled_out, 3.0, f_titled_out)     # outside [1, 2)
    extract_frame_png(untitled_out, 3.0, f_untitled_out)
    diff_out = png_mean_abs_diff(f_titled_out, f_untitled_out)
    check("frame OUTSIDE the title window does not differ meaningfully (diff=%.4f < %.2f)"
          % (diff_out, TITLE_DIFF_MAX_OUTSIDE), diff_out < TITLE_DIFF_MAX_OUTSIDE, diff_out)
else:
    check("(setup) both titled and untitled cuts produced files to diff", False,
          (titled_out, untitled_out))

# --- missing fontfile -> can_title false, titled cut refused ----------------
FAKE_PROC4, FAKE_PORT4, FAKE_STORE4 = start_fake_lane(SCRATCH, "nofont")
FAKE_OUT4 = os.path.join(FAKE_STORE4, "outputs")
BAD_FONT_CFG = {"cut": {"fontfile": "/no/such/font/at/all.ttf"}}
_proc_nfont, URL_NFONT, _data_nfont, _log_nfont = start_server(SCRATCH, FAKE_PORT4, name="nofont",
                                                                  cut_config=BAD_FONT_CFG)
capture_unknown_route_body(URL_NFONT)
seq_nofont = seq_create(URL_NFONT, "R6 missing fontfile")
check("can_title is FALSE when config.cut.fontfile points at a missing path",
      seq_nofont.get("can_title") is False, seq_nofont.get("can_title"))
title_reason = seq_nofont.get("title_reason")
check("title_reason names the missing path",
      isinstance(title_reason, str) and "/no/such/font/at/all.ttf" in title_reason, title_reason)

s_nf = add_video_slot(URL_NFONT, seq_nofont["id"], seq_nofont["rev"], prompt="titled shot")["slots"][0]["id"]
clip_nf = os.path.join(FAKE_OUT4, "r6_nofont.mp4")
make_clip(clip_nf, width=CANVAS_W, height=CANVAS_H, duration=3.0)
slot_nf, job_nf, rev_nf = generate_slot(URL_NFONT, seq_nofont["id"], s_nf, FAKE_PORT4, clip_nf)
rev_nf = pick(URL_NFONT, seq_nofont["id"], rev_nf, s_nf, job_nf)["rev"]
code_nfc, body_nfc = set_title_card(URL_NFONT, seq_nofont["id"], rev_nf, s_nf, {"text": "Hi", "at": 0.5, "dur": 1.0})
rev_nf = body_nfc.get("rev", rev_nf)
code_cutnf, body_cutnf = do_cut(URL_NFONT, seq_nofont["id"])
check("a cut of a sequence carrying a title card is REFUSED when can_title is false "
      "(the title is never silently dropped, a REAL refusal not the generic 404)",
      refused(URL_NFONT, code_cutnf, body_cutnf) and body_cutnf.get("error") == title_reason,
      (code_cutnf, body_cutnf, title_reason))
if code_cutnf == 200:
    wait_cut_done(URL_NFONT, seq_nofont["id"], body_cutnf.get("cut_id", ""), timeout=30)


# ---------------------------------------------------------------------------
print("R7: music bed (owner ruling 3) -- first picked SOUND-lane take, mixed at -18dB, "
      "cut at the film's end, no pick -> no bed")

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


seq_r7 = seq_create(URL, "R7 music bed")
s_clip7 = add_video_slot(URL, seq_r7["id"], seq_r7["rev"], prompt="film")["slots"][0]["id"]
rev7 = seq_get(URL, seq_r7["id"])["rev"]
s_bed7 = add_sound_slot(URL, seq_r7["id"], rev7, seconds=6.0)["slots"][1]["id"]

clip7 = os.path.join(FAKE_OUT, "r7_film.mp4")
make_clip(clip7, width=CANVAS_W, height=CANVAS_H, duration=3.0, audio_freq=1000)
bed7 = os.path.join(FAKE_OUT, "r7_bed.m4a")
make_audio_only(bed7, 440, 6.0)   # bed longer than the 3s film

slot_c7, job_c7, rev7 = generate_slot(URL, seq_r7["id"], s_clip7, FAKE_PORT, clip7)
slot_b7, job_b7, rev7 = generate_slot(URL, seq_r7["id"], s_bed7, FAKE_PORT, bed7)
rev7 = pick(URL, seq_r7["id"], rev7, s_clip7, job_c7)["rev"]
rev7 = pick(URL, seq_r7["id"], rev7, s_bed7, job_b7)["rev"]
D_film7 = probed_duration(os.path.join(DATA_DIR, "seq", seq_r7["id"], slot_c7["takes"][0]["file"]))

code_r7, body_r7 = do_cut(URL, seq_r7["id"])
check("R7 cut (with a music bed) accepted", code_r7 == 200 and body_r7.get("ok"), (code_r7, body_r7))
if code_r7 == 200 and body_r7.get("ok"):
    entry7 = wait_cut_done(URL, seq_r7["id"], body_r7["cut_id"], timeout=120)
    check("R7 cut reaches status done", entry7 is not None and entry7.get("status") == "done", entry7)
    if entry7 and entry7.get("status") == "done":
        out7 = cut_file_path(DATA_DIR, seq_r7["id"], entry7, body_r7["cut_id"])
        outdur7 = probed_duration(out7)
        check("output duration matches the FILM's length, not the longer bed's (+/-0.15s)",
              abs(outdur7 - D_film7) <= 0.15, (outdur7, D_film7))
        v1k7 = band_volume(out7, 1000, 100)
        v440_7 = band_volume(out7, 440, 80)
        diff7 = (v1k7 - v440_7) if (v1k7 is not None and v440_7 is not None) else None
        check("bed sits ~%.1fdB under the clip audio (+/-%.1fdB): got %r"
              % (BED_DIFF_TARGET, BED_DIFF_TOL, diff7),
              diff7 is not None and abs(diff7 - BED_DIFF_TARGET) <= BED_DIFF_TOL, (v1k7, v440_7, diff7))

# --- no sound pick -> no bed -------------------------------------------------
seq_r7b = seq_create(URL, "R7 no sound pick")
s_clip7b = add_video_slot(URL, seq_r7b["id"], seq_r7b["rev"], prompt="film")["slots"][0]["id"]
rev7b = seq_get(URL, seq_r7b["id"])["rev"]
add_sound_slot(URL, seq_r7b["id"], rev7b, seconds=6.0)   # present, but never picked/generated
rev7b = seq_get(URL, seq_r7b["id"])["rev"]
clip7b = os.path.join(FAKE_OUT, "r7b_film.mp4")
make_clip(clip7b, width=CANVAS_W, height=CANVAS_H, duration=3.0, audio_freq=1000)
slot_c7b, job_c7b, rev7b = generate_slot(URL, seq_r7b["id"], s_clip7b, FAKE_PORT, clip7b)
rev7b = pick(URL, seq_r7b["id"], rev7b, s_clip7b, job_c7b)["rev"]
code_r7b, body_r7b = do_cut(URL, seq_r7b["id"])
check("R7 cut with no sound pick accepted", code_r7b == 200 and body_r7b.get("ok"), (code_r7b, body_r7b))
if code_r7b == 200 and body_r7b.get("ok"):
    entry7b = wait_cut_done(URL, seq_r7b["id"], body_r7b["cut_id"], timeout=120)
    if entry7b and entry7b.get("status") == "done":
        out7b = cut_file_path(DATA_DIR, seq_r7b["id"], entry7b, body_r7b["cut_id"])
        v440_none = band_volume(out7b, 440, 80)
        check("no 440Hz bed energy when no sound take is picked (band mean_volume very low: %r)" % v440_none,
              v440_none is not None and v440_none < -50.0, v440_none)


# ---------------------------------------------------------------------------
print("R8: the cut as a job -- returns immediately, 409 while one runs, restart mid-cut -> interrupted, "
      "error carries a sentence never a traceback")

FAKE_PROC5, FAKE_PORT5, FAKE_STORE5 = start_fake_lane(SCRATCH, "r8")
FAKE_OUT5 = os.path.join(FAKE_STORE5, "outputs")
_proc_r8, URL_R8, DATA_R8, _log_r8 = start_server(SCRATCH, FAKE_PORT5, name="r8", cut_config=FONT_CFG)
capture_unknown_route_body(URL_R8)

seq_r8 = seq_create(URL_R8, "R8 heavy sequence")
N_SLOTS_R8 = 10
rev_r8 = seq_r8["rev"]
slot_ids_r8 = []
for i in range(N_SLOTS_R8):
    body = add_video_slot(URL_R8, seq_r8["id"], rev_r8, prompt="heavy %d" % i)
    rev_r8 = body["rev"]
    slot_ids_r8.append(body["slots"][i]["id"])
for i, sid_slot in enumerate(slot_ids_r8):
    clip_i = os.path.join(FAKE_OUT5, "r8_%d.mp4" % i)
    make_clip(clip_i, width=CANVAS_W, height=CANVAS_H, duration=4.0)
    slot_i, job_i, rev_r8 = generate_slot(URL_R8, seq_r8["id"], sid_slot, FAKE_PORT5, clip_i)
    rev_r8 = pick(URL_R8, seq_r8["id"], rev_r8, sid_slot, job_i)["rev"]
    set_title_card(URL_R8, seq_r8["id"], rev_r8, sid_slot, {"text": "Shot %d" % i, "at": 0.2, "dur": 1.0})
    rev_r8 = seq_get(URL_R8, seq_r8["id"])["rev"]
note("R8 sequence built: %d slots x 4s = %ds of content, all titled" % (N_SLOTS_R8, N_SLOTS_R8 * 4))

t0 = time.time()
code_r8a, body_r8a = do_cut(URL_R8, seq_r8["id"])
elapsed_r8a = time.time() - t0
check("POST /api/sequence/cut returns {ok, cut_id} immediately (< 2s), work runs in the background",
      code_r8a == 200 and body_r8a.get("ok") and body_r8a.get("cut_id") and elapsed_r8a < 2.0,
      (code_r8a, body_r8a, elapsed_r8a))
cut_id_r8 = body_r8a.get("cut_id") if code_r8a == 200 else None

if cut_id_r8:
    code_r8b, body_r8b = do_cut(URL_R8, seq_r8["id"])
    check("a second cut on the same sequence while one is running -> 409",
          code_r8b == 409 and isinstance(body_r8b, dict) and body_r8b.get("error"), (code_r8b, body_r8b))

    seq_running = seq_get(URL_R8, seq_r8["id"])
    entry_running = next((c for c in seq_running.get("cuts") or [] if c.get("id") == cut_id_r8), None)
    check("the cuts[] entry exists with status queued/running right after the POST",
          entry_running is not None and entry_running.get("status") in ("queued", "running"), entry_running)

    # --- restart mid-cut: kill the subprocess outright, start a fresh one
    # pointed at the SAME data dir, and confirm the cut reads interrupted. --
    _proc_r8.kill()
    _proc_r8.wait(timeout=10)
    cfg_r8_path = os.path.join(SCRATCH, "config_r8.json")
    port_r8_2 = free_port()
    cfg_r8 = json.load(open(cfg_r8_path))
    cfg_r8["port"] = port_r8_2
    with open(cfg_r8_path, "w") as f:
        json.dump(cfg_r8, f)
    logf_r8b = open(os.path.join(SCRATCH, "server_r8_restart.log"), "w")
    env_r8b = dict(os.environ, GENCENTER_CONFIG=cfg_r8_path, GENCENTER_DATA=DATA_R8)
    proc_r8_restart = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env_r8b,
                                        stdout=logf_r8b, stderr=subprocess.STDOUT)
    PROCS.append(proc_r8_restart)
    url_r8_restart = "http://127.0.0.1:%d/" % port_r8_2
    ok_restart, _ = wait_true("restarted R8 server is up", lambda: http_json(url_r8_restart + "api/health")[0] == 200, 20)
    if ok_restart:
        seq_after_restart = seq_get(url_r8_restart, seq_r8["id"])
        entry_after_restart = next((c for c in seq_after_restart.get("cuts") or [] if c.get("id") == cut_id_r8), None)
        check("after an app restart, a queued/running cut reads interrupted (never running forever)",
              entry_after_restart is not None and entry_after_restart.get("status") == "interrupted",
              entry_after_restart)
        code_after, body_after = do_cut(url_r8_restart, seq_r8["id"])
        check("a fresh cut is accepted after the app restart (the interrupted one no longer blocks 409)",
              code_after == 200 and body_after.get("ok"), (code_after, body_after))
        if code_after == 200:
            entry_final = wait_cut_done(url_r8_restart, seq_r8["id"], body_after["cut_id"], timeout=180)
            check("that fresh cut reaches a terminal status", entry_final is not None, entry_final)
            if entry_final:
                check("no status ever carries a Python traceback",
                      "Traceback" not in json.dumps(entry_final), entry_final)
    _proc_r8 = proc_r8_restart


# --- a corrupted take -> the background cut ends in status error with a plain sentence ---
seq_r8e = seq_create(URL, "R8 error status")
s_r8e = add_video_slot(URL, seq_r8e["id"], seq_r8e["rev"], prompt="corrupt me")["slots"][0]["id"]
clip_r8e = os.path.join(FAKE_OUT, "r8_corrupt.mp4")
make_clip(clip_r8e, width=CANVAS_W, height=CANVAS_H, duration=2.0)
slot_r8e, job_r8e, rev_r8e = generate_slot(URL, seq_r8e["id"], s_r8e, FAKE_PORT, clip_r8e)
rev_r8e = pick(URL, seq_r8e["id"], rev_r8e, s_r8e, job_r8e)["rev"]
harvested_path_r8e = os.path.join(DATA_DIR, "seq", seq_r8e["id"], slot_r8e["takes"][0]["file"])
with open(harvested_path_r8e, "wb") as f:
    f.write(b"not a real video file at all, ffprobe must choke on this")
code_r8e, body_r8e = do_cut(URL, seq_r8e["id"])
if code_r8e == 200 and body_r8e.get("ok"):
    entry_r8e = wait_cut_done(URL, seq_r8e["id"], body_r8e["cut_id"], timeout=60)
    check("a corrupted local take makes the background cut end in status error",
          entry_r8e is not None and entry_r8e.get("status") == "error", entry_r8e)
    if entry_r8e:
        log_text = json.dumps(entry_r8e)
        check("that error carries a plain sentence, not a Python traceback",
              entry_r8e.get("status") == "error" and "Traceback" not in log_text
              and isinstance(entry_r8e.get("log"), str) and entry_r8e["log"].strip(),
              entry_r8e)
else:
    # a synchronous refusal (probing the take before queuing) also satisfies
    # "never a traceback reaching the user" -- accepted as an alternate valid
    # shape, noted in the report's low-confidence rulings.
    check("a corrupted take is refused synchronously instead (alternate valid shape, "
          "a REAL refusal not the generic 404)",
          refused(URL, code_r8e, body_r8e) and "Traceback" not in json.dumps(body_r8e),
          (code_r8e, body_r8e))


# ---------------------------------------------------------------------------
print("R9: served -- GET /api/sequence/file with a Range header on a finished cut -> 206")

_r9_entry = globals().get("entry4")
_r9_cutid = globals().get("body_r4", {}).get("cut_id") if isinstance(globals().get("body_r4"), dict) else None
if _r9_entry and _r9_entry.get("status") == "done" and _r9_cutid:
    rel = _r9_entry.get("file") or ("cuts/%s.mp4" % _r9_cutid)
    file_url = URL + "api/sequence/file?id=%s&path=%s" % (seq_r4["id"], urllib.parse.quote(rel))
    status_full, body_full, headers_full = http_raw(file_url)
    check("GET /api/sequence/file (no Range) serves the cut, 200", status_full == 200, status_full)
    status_range, body_range, headers_range = http_raw(file_url, headers={"Range": "bytes=0-9"})
    check("GET /api/sequence/file with Range: bytes=0-9 -> 206", status_range == 206, status_range)
    check("the 206 body is exactly the 10 requested bytes",
          status_range == 206 and len(body_range) == 10, len(body_range) if status_range == 206 else None)
    check("the 206 body's first bytes equal the full file's first bytes",
          status_range == 206 and body_full[:10] == body_range, None)
else:
    check("(setup) R9 needed a finished cut from R4 to test Range serving -- see report", False,
          "R4's cut never reached status done, so R9 could not be exercised against a real file")


print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
