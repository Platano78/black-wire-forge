"""Regression gate for the C3.6 review fix: a picked SOUND-lane bed slot
must go through the SAME cut-time harvest retry (R2) as a picked video
shot, and a bed that is still missing after the retry must REFUSE the cut
synchronously, naming the bed's slot -- never silently drop the bed.
Orchestrator ruling, 2026-09-23 (this file did not exist before that
review; the fix it gates was stashed to prove this file RED first, per the
review's own instruction -- see the builder report for that transcript).

Calibrated on ffmpeg 4.4.2 (the oldest version the cut pipeline supports),
same as tests/test_cut.py. Run it locally:

    python3 tests/test_cut_bed.py

ISOLATION: same as test_cut.py -- a fresh tempfile.mkdtemp() scratch dir,
GENCENTER_DATA/GENCENTER_CONFIG pointed there, a real server.py subprocess
+ a real fake_comfy.py lane, never the owner's data/ or the live app.
"""
import json
import os
import re
import shutil
import socket
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
# absence on a clean clone is a SKIP, not a failure.
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


def band_volume(path, freq, width, trim=None):
    args = []
    if trim:
        args += ["-ss", str(trim[0]), "-t", str(trim[1])]
    args += ["-i", path, "-af", "bandpass=f=%d:w=%d,volumedetect" % (freq, width), "-f", "null", "-"]
    err = ffmpeg(args, "volumedetect")
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", err)
    return float(m.group(1)) if m else None


def audio_sample_rate(path):
    info = ffprobe_json(path, ["-show_streams", "-select_streams", "a:0"])
    streams = info.get("streams") or []
    return int(streams[0]["sample_rate"]) if streams and streams[0].get("sample_rate") else None


def measured_integrated_loudness(path):
    """loudnorm's own measurement pass (print_format=json) on a FINISHED
    file -- input_i is that file's actual integrated loudness in LUFS,
    independent of whatever the cut's own two-pass measurement computed."""
    err = ffmpeg(["-i", path, "-af", "loudnorm=I=-16:TP=-1.0:LRA=11:print_format=json",
                  "-f", "null", "-"], "loudnorm measure")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", err, re.S)
    if not m:
        return None
    return float(json.loads(m.group(0))["input_i"])


def true_peak_ebur128(path):
    """ffmpeg's ebur128=peak=true, measured on the FINISHED (encoded) file
    -- the true peak AFTER the AAC encode, which is what R4's <= -1.0 dBTP
    rule actually has to hold on (loudnorm's own TP target only controls
    the PRE-encode signal; AAC's own lossy transform can overshoot it by a
    few tenths of a dB on transient-heavy content -- review fix,
    2026-09-23)."""
    err = ffmpeg(["-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"], "ebur128 measure")
    m = re.search(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+) dBFS", err, re.S)
    return float(m.group(1)) if m else None


def make_hot_burst_clip(path, width, height, bed_db=-16, spike=0.05, duration=3.0, fps=24, seed=None):
    """A steady -16dB tone bed (NOT silence -- EBU R128 gating excludes
    near-silent blocks from the loudness measurement entirely, which made
    an earlier silence-bed version of this clip overshoot by several dB
    regardless of the TP target, not the few-tenths-of-a-dB AAC-only
    overshoot this is meant to test) with a short full-scale sine+noise
    spike appended. Measured directly against this repo's real ffmpeg
    pipeline (both directions -- TP=-1.0 fails, TP=-1.5 passes): the
    steady bed keeps loudnorm's own I=-16 gain small and predictable, so
    the spike lands close to the TP ceiling pre-encode, and AAC's own
    overshoot on top of that is what pushes it over -- a fully silent gap
    or a flat steady tone (this file's OTHER audio helpers) never get
    close, however loud. `seed` (F5) seeds anoisesrc so a sweep is
    repeatable -- anoisesrc's own default reseeds itself from the clock
    every run otherwise."""
    tail = duration - spike
    bed = path + ".bed.wav"
    spike_wav = path + ".spike.wav"
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=1000:duration=%s" % tail,
            "-af", "volume=%sdB" % bed_db, bed], "make_hot_burst_clip bed")
    noise = ("anoisesrc=color=white:amplitude=0.9:duration=%s" % spike
              + (":seed=%d" % seed if seed is not None else ""))
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=1000:duration=%s" % spike,
            "-f", "lavfi", "-i", noise,
            "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]",
            "-map", "[a]", spike_wav], "make_hot_burst_clip spike")
    ffmpeg(["-f", "lavfi", "-i", "color=c=red:size=%dx%d:rate=%s:duration=%s" % (width, height, fps, duration),
            "-i", bed, "-i", spike_wav,
            "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path], "make_hot_burst_clip mux")
    for tmp in (bed, spike_wav):
        try:
            os.remove(tmp)
        except OSError:
            pass
    return path


def make_burst_in_silence_clip(path, width, height, burst=0.05, duration=3.0, fps=24, seed=42):
    """A short full-scale sine+noise burst amid otherwise SILENCE (not the
    steady tone bed above) -- realistic as "quiet dialogue, then one loud
    moment" content. EBU R128 gating excludes the silent stretch from the
    loudness measurement, so loudnorm's own linear-mode gain overcorrects
    and can push the burst several dB past ANY fixed TP target -- this is
    what the post-loudnorm limiter (CUT_LIMITER_DB) exists to catch."""
    silence = duration - burst
    ffmpeg(["-f", "lavfi", "-i", "color=c=red:size=%dx%d:rate=%s:duration=%s" % (width, height, fps, duration),
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=%s" % burst,
            "-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.9:duration=%s:seed=%d" % (burst, seed),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=%s" % silence,
            "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first:normalize=0[b];"
                                "[b][3:a]concat=n=2:v=0:a=1[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path], "make_burst_in_silence_clip")
    return path


def make_clip(path, width, height, duration=3.0, fps=24, sample_rate=48000, freq=1000, volume_db=None,
              audio=True):
    args = ["-f", "lavfi", "-i", "color=c=red:size=%dx%d:rate=%s:duration=%s" % (width, height, fps, duration)]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=%d:duration=%s" % (freq, sample_rate, duration)]
        if volume_db is not None:
            args += ["-af", "volume=%sdB" % volume_db]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac"] if audio else ["-an"]
    args += [path]
    ffmpeg(args, "make_clip")
    return path


def make_audio_only(path, freq, duration, sample_rate=48000):
    ffmpeg(["-f", "lavfi", "-i", "sine=frequency=%d:sample_rate=%d:duration=%s" % (freq, sample_rate, duration),
            "-c:a", "aac", path], "make_audio_only")
    return path


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def http_json(url, data=None, method=None, timeout=10):
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None


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


PROCS = []


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


def start_server(scratch, lane_port, name="main", job_poll_seconds=6.0):
    data_dir = os.path.join(scratch, "data_%s" % name)
    cfg_path = os.path.join(scratch, "config_%s.json" % name)
    port = free_port()
    cfg = {"title": "C3.6 bed test", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Target lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"]}],
           "timing": {"poll_seconds": 0.3, "job_poll_seconds": job_poll_seconds,
                      "http_timeout": 5.0, "free_settle_seconds": 1.0, "discover_seconds": 300.0},
           "cut": {"fontfile": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"}}
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
        with open(os.path.join(scratch, "server_%s.log" % name)) as f:
            note("server %s log tail: %s" % (name, f.read()[-1500:]))
    wait_lane_up(url, "t")
    return proc, url, data_dir


def wait_lane_up(url, lane_id, timeout=60):
    def lane_up():
        code, body = http_json(url + "api/lanes")
        lanes = body.get("lanes") if isinstance(body, dict) else body
        if code != 200 or not isinstance(lanes, list):
            return False
        l = next((x for x in lanes if x.get("id") == lane_id), None)
        return bool(l and l.get("up") and l.get("discovered"))  # discovered: generate before discovery is refused (503)
    return wait_true("lane %r is up" % lane_id, lane_up, timeout)


def seq_create(url, title):
    code, body = http_json(url + "api/sequence", data={"title": title, "mode": "sequence"}, method="POST")
    if code != 200:
        raise RuntimeError("seq_create failed: %r" % (body,))
    return body


def seq_get(url, sid):
    code, body = http_json(url + "api/sequence?id=" + sid)
    if code != 200:
        raise RuntimeError("seq_get failed: %r" % (body,))
    return body


def seq_op(url, sid, rev, op, **kw):
    payload = dict(kw, id=sid, rev=rev, op=op)
    return http_json(url + "api/sequence/op", data=payload, method="POST")


def add_video_slot(url, sid, rev, length=121, at=None):
    kw = dict(lane="video", cap="video", mode="ltx", values={"prompt": "a test shot", "length": length})
    if at is not None:
        kw["at"] = at
    code, body = seq_op(url, sid, rev, "add_slot", **kw)
    if code != 200:
        raise RuntimeError("add_slot(video) failed: %r" % (body,))
    return body


def add_sound_slot(url, sid, rev, seconds=6.0):
    code, body = seq_op(url, sid, rev, "add_slot", lane="sound", cap="audio", mode="sfx",
                        values={"prompt": "a hum", "seconds": seconds})
    if code != 200:
        raise RuntimeError("add_slot(sound) failed: %r" % (body,))
    return body


def generate(url, sid, slot_id, lane_port, clip_path, timeout=20):
    fname = os.path.basename(clip_path)
    control_accept(lane_port, [{"filename": fname, "subfolder": "", "type": "output"}])
    code, body = http_json(url + "api/sequence/generate", data={"id": sid, "slot_id": slot_id}, method="POST")
    if code != 200 or not body.get("ok"):
        raise RuntimeError("seq_generate failed: %r" % (body,))
    job_id = body["job"]["id"]
    control_accept(lane_port, None)
    return job_id


def pick(url, sid, rev, slot_id, job_id):
    code, body = seq_op(url, sid, rev, "pick_take", slot_id=slot_id, job_id=job_id)
    if code != 200:
        raise RuntimeError("pick_take failed: %r" % (body,))
    return body


def do_cut(url, sid):
    code, body = http_json(url + "api/sequence/cut", data={"id": sid}, method="POST")
    return code, body


def wait_cut_done(url, sid, cut_id, timeout=60):
    def get_entry():
        seq = seq_get(url, sid)
        entry = next((c for c in seq.get("cuts") or [] if c.get("id") == cut_id), None)
        if entry and entry.get("status") in ("done", "error", "interrupted"):
            return entry
        return None
    ok, entry = wait_true("cut %s reaches a terminal status" % cut_id, get_entry, timeout, interval=0.5)
    return entry if ok else None


def harvested_take(url, sid, slot_id, job_id):
    seq = seq_get(url, sid)
    slot = next(s for s in seq["slots"] if s["id"] == slot_id)
    take = next((t for t in slot["takes"] if t["job_id"] == job_id), None)
    return bool(take and take.get("file"))


SCRATCH = tempfile.mkdtemp(prefix="bwf_cutbed_")
print("scratch dir: %s (real data/ and the live ~/black-wire-forge are never touched)" % SCRATCH)

import atexit
def _cleanup():
    for p in PROCS:
        stop(p)
    shutil.rmtree(SCRATCH, ignore_errors=True)
atexit.register(_cleanup)

# job_poll_seconds is slow (6s) so we can remove a fake lane's output file
# BEFORE the server's own harvest runs, forcing a real take.file: null --
# the same timing trick test_cut.py's own R2 section uses.
FAKE_PROC, FAKE_PORT, FAKE_STORE = start_fake_lane(SCRATCH, "t")
FAKE_OUT = os.path.join(FAKE_STORE, "outputs")
_srv_proc, URL, DATA_DIR = start_server(SCRATCH, FAKE_PORT, name="main", job_poll_seconds=6.0)

_probe_seq = seq_create(URL, "canvas probe")
CANVAS_W, CANVAS_H = _probe_seq["canvas"]["width"], _probe_seq["canvas"]["height"]
note("sequence canvas is %dx%d" % (CANVAS_W, CANVAS_H))


# ---------------------------------------------------------------------------
print("bed still missing at cut time (source never restored) -> refused, naming the bed's slot")

seq1 = seq_create(URL, "bed missing")
rev = seq1["rev"]
after_v = add_video_slot(URL, seq1["id"], rev)
video_slot = after_v["slots"][0]["id"]
rev = after_v["rev"]
after_s = add_sound_slot(URL, seq1["id"], rev)
sound_slot = after_s["slots"][1]["id"]

clip_v = os.path.join(FAKE_OUT, "bed1_video.mp4")
make_clip(clip_v, CANVAS_W, CANVAS_H, duration=2.0)
job_v = generate(URL, seq1["id"], video_slot, FAKE_PORT, clip_v)

def video_harvested():
    seq = seq_get(URL, seq1["id"])
    slot = next(s for s in seq["slots"] if s["id"] == video_slot)
    take = next((t for t in slot["takes"] if t["job_id"] == job_v), None)
    return take if take and take.get("file") else None
ok, _ = wait_true("(setup) video take harvested", video_harvested, 15, interval=0.5)

clip_bed = os.path.join(FAKE_OUT, "bed1_bed.m4a")
make_audio_only(clip_bed, 440, 4.0)
job_bed = generate(URL, seq1["id"], sound_slot, FAKE_PORT, clip_bed)
os.remove(clip_bed)   # the fake lane's /view now 404s for this filename
note("removed the fake lane's bed output immediately after generate; job_poll_seconds=6, "
     "waiting past one poll tick so the first (non-retried) harvest attempt fails naturally")
time.sleep(7.5)

def bed_take_file_is_null():
    seq = seq_get(URL, seq1["id"])
    slot = next(s for s in seq["slots"] if s["id"] == sound_slot)
    take = next(t for t in slot["takes"] if t["job_id"] == job_bed)
    return take.get("file") is None
got_null, _ = wait_true("(setup) the bed take's file is null after its output vanished pre-harvest",
                         bed_take_file_is_null, 3, interval=0.5)

if got_null:
    rev1 = seq_get(URL, seq1["id"])["rev"]
    rev1 = pick(URL, seq1["id"], rev1, video_slot, job_v)["rev"]
    rev1 = pick(URL, seq1["id"], rev1, sound_slot, job_bed)["rev"]
    code, body = do_cut(URL, seq1["id"])
    check("cut REFUSED (a real 4xx with a sentence, not accepted) when the picked bed's take is "
          "still missing at cut time (source never restored)",
          400 <= code < 500 and isinstance(body, dict) and isinstance(body.get("error"), str) and body["error"].strip(),
          (code, body))
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        check("the refusal names the bed's own slot id",
              sound_slot in body["error"], body["error"])
    if code == 200 and isinstance(body, dict) and body.get("cut_id"):
        # if a build wrongly accepted this, don't leave a stray running cut for later checks to trip on
        wait_cut_done(URL, seq1["id"], body["cut_id"], timeout=30)
else:
    check("(setup) could not force a null bed take.file via timing -- see report", False,
          "the bed-missing-file check could not be exercised without a timing race")


# ---------------------------------------------------------------------------
print("bed's source restored before cut -> the cut-time retry re-copies it, and the cut succeeds WITH the bed")

seq2 = seq_create(URL, "bed retried")
rev = seq2["rev"]
after_v2 = add_video_slot(URL, seq2["id"], rev)
video_slot2 = after_v2["slots"][0]["id"]
rev = after_v2["rev"]
after_s2 = add_sound_slot(URL, seq2["id"], rev, seconds=4.0)
sound_slot2 = after_s2["slots"][1]["id"]

clip_v2 = os.path.join(FAKE_OUT, "bed2_video.mp4")
make_clip(clip_v2, CANVAS_W, CANVAS_H, duration=3.0)
job_v2 = generate(URL, seq2["id"], video_slot2, FAKE_PORT, clip_v2)

def video2_harvested():
    seq = seq_get(URL, seq2["id"])
    slot = next(s for s in seq["slots"] if s["id"] == video_slot2)
    take = next((t for t in slot["takes"] if t["job_id"] == job_v2), None)
    return take if take and take.get("file") else None
wait_true("(setup) video take harvested", video2_harvested, 15, interval=0.5)

clip_bed2 = os.path.join(FAKE_OUT, "bed2_bed.m4a")
make_audio_only(clip_bed2, 440, 4.0)
saved_bytes = open(clip_bed2, "rb").read()
job_bed2 = generate(URL, seq2["id"], sound_slot2, FAKE_PORT, clip_bed2)
os.remove(clip_bed2)
time.sleep(7.5)

def bed2_take_file_is_null():
    seq = seq_get(URL, seq2["id"])
    slot = next(s for s in seq["slots"] if s["id"] == sound_slot2)
    take = next(t for t in slot["takes"] if t["job_id"] == job_bed2)
    return take.get("file") is None
got_null2, _ = wait_true("(setup) the bed take's file is null after its output vanished pre-harvest",
                          bed2_take_file_is_null, 3, interval=0.5)

if got_null2:
    with open(clip_bed2, "wb") as f:
        f.write(saved_bytes)   # restore -- the SOURCE is back, only the cut can retry the copy now
    rev2 = seq_get(URL, seq2["id"])["rev"]
    rev2 = pick(URL, seq2["id"], rev2, video_slot2, job_v2)["rev"]
    rev2 = pick(URL, seq2["id"], rev2, sound_slot2, job_bed2)["rev"]
    code2, body2 = do_cut(URL, seq2["id"])
    check("cut accepted with the bed's take.file still null but its source restored",
          code2 == 200 and body2.get("ok"), (code2, body2))
    if code2 == 200 and body2.get("ok"):
        entry2 = wait_cut_done(URL, seq2["id"], body2["cut_id"], timeout=90)
        check("the cut reaches status done (the bed's cut-time retry re-copied it)",
              entry2 is not None and entry2.get("status") == "done", entry2)
        if entry2 and entry2.get("status") == "done":
            seq_after = seq_get(URL, seq2["id"])
            slot_after = next(s for s in seq_after["slots"] if s["id"] == sound_slot2)
            take_after = next(t for t in slot_after["takes"] if t["job_id"] == job_bed2)
            check("the bed take's file is set again after the cut ran",
                  take_after.get("file") is not None, take_after)
            check("the cut's own record names the bed's slot",
                  sound_slot2 in json.dumps(entry2), entry2)
            out_path = os.path.join(DATA_DIR, "seq", seq2["id"], entry2.get("file") or ("cuts/%s.mp4" % body2["cut_id"]))
            if os.path.isfile(out_path):
                v440 = band_volume(out_path, 440, 80)
                check("the finished cut actually carries the bed's 440Hz energy (band mean_volume %r)" % v440,
                      v440 is not None and v440 > -50.0, v440)
            else:
                check("(setup) finished cut file exists on disk", False, out_path)
else:
    check("(setup) could not force a null bed take.file via timing for the retry-success case -- see report",
          False, "the bed cut-time-retry check could not be exercised without a timing race")


# ---------------------------------------------------------------------------
print("R4 review fix (2026-09-23): output audio is 48kHz, and integrated loudness lands within +/-1 LU "
      "of the -16 LUFS target on a real multi-shot programme (a single-pass loudnorm's own ESTIMATE "
      "drifted to -18.6 LUFS on this exact assembly; two-pass -- measure, then apply with linear=true -- "
      "is what actually holds the target, still ONE normalisation over the whole join per R4)")

seq3 = seq_create(URL, "R4 loudness accuracy")
rev3 = seq3["rev"]
seg_slots = []
# Three shots of varying volume/tone -- the dynamics a single-pass estimate
# gets wrong; a flat single-tone clip (as R7's own test above uses) does not
# reproduce the drift, measured directly against this exact repo's ffmpeg.
for i, (vol, freq) in enumerate([(0, 1000), (-14, 600), (-6, 1400)]):
    body = add_video_slot(URL, seq3["id"], rev3)
    rev3 = body["rev"]
    seg_slots.append(body["slots"][i]["id"])
job_ids = []
for slot_id, (vol, freq) in zip(seg_slots, [(0, 1000), (-14, 600), (-6, 1400)]):
    clip = os.path.join(FAKE_OUT, "loud_seg_%s.mp4" % slot_id)
    make_clip(clip, CANVAS_W, CANVAS_H, duration=4.0, freq=freq, volume_db=vol)
    job_ids.append(generate(URL, seq3["id"], slot_id, FAKE_PORT, clip))
    wait_true("(setup) segment %s harvested" % slot_id,
              lambda sid=slot_id, jid=job_ids[-1]: harvested_take(URL, seq3["id"], sid, jid), 15)
rev3 = seq_get(URL, seq3["id"])["rev"]
for slot_id, jid in zip(seg_slots, job_ids):
    rev3 = pick(URL, seq3["id"], rev3, slot_id, jid)["rev"]

code3, body3 = do_cut(URL, seq3["id"])
check("R4 loudness-accuracy cut accepted", code3 == 200 and body3.get("ok"), (code3, body3))
if code3 == 200 and body3.get("ok"):
    entry3 = wait_cut_done(URL, seq3["id"], body3["cut_id"], timeout=120)
    check("R4 loudness-accuracy cut reaches status done", entry3 is not None and entry3.get("status") == "done", entry3)
    if entry3 and entry3.get("status") == "done":
        out3 = os.path.join(DATA_DIR, "seq", seq3["id"], entry3.get("file") or ("cuts/%s.mp4" % body3["cut_id"]))
        check("R4 loudness-accuracy output file exists", os.path.isfile(out3), out3)
        if os.path.isfile(out3):
            sr3 = audio_sample_rate(out3)
            check("output audio sample rate is 48000 (loudnorm's own internal resample corrected by "
                  "aresample after it), got %r" % sr3, sr3 == 48000, sr3)
            measured_i3 = measured_integrated_loudness(out3)
            check("output integrated loudness is within +/-1 LU of the -16 LUFS target on a real "
                  "multi-shot programme (two-pass loudnorm, not a single-pass estimate), got %r" % measured_i3,
                  measured_i3 is not None and abs(measured_i3 - (-16.0)) <= 1.0, measured_i3)
            tp3 = true_peak_ebur128(out3)
            note("R4 normal multi-shot case: measured I=%r LUFS, TP=%r dBTP" % (measured_i3, tp3))
        check("the cut's own record marks this a two-pass loudness normalisation",
              entry3.get("loudness") == "two-pass", entry3.get("loudness"))


# ---------------------------------------------------------------------------
print("R4 review fix (2026-09-23), part 2: an ALL-SILENT assembly (every picked shot has no audio "
      "stream, no bed) -- loudnorm's own measurement reports input_i/input_tp as -inf on pure silence; "
      "feeding that straight into a linear loudnorm as measured_I would corrupt the output, so it must "
      "be skipped outright rather than applied blindly")

seq4 = seq_create(URL, "R4 silent assembly")
rev4 = seq4["rev"]
sil_slots = []
for i in range(2):
    body = add_video_slot(URL, seq4["id"], rev4)
    rev4 = body["rev"]
    sil_slots.append(body["slots"][i]["id"])
sil_jobs = []
for slot_id in sil_slots:
    clip = os.path.join(FAKE_OUT, "silent_%s.mp4" % slot_id)
    make_clip(clip, CANVAS_W, CANVAS_H, duration=2.0, audio=False)
    jid = generate(URL, seq4["id"], slot_id, FAKE_PORT, clip)
    sil_jobs.append(jid)
    wait_true("(setup) silent segment %s harvested" % slot_id,
              lambda sid=slot_id, j=jid: harvested_take(URL, seq4["id"], sid, j), 15)
rev4 = seq_get(URL, seq4["id"])["rev"]
for slot_id, jid in zip(sil_slots, sil_jobs):
    rev4 = pick(URL, seq4["id"], rev4, slot_id, jid)["rev"]

code4, body4 = do_cut(URL, seq4["id"])
check("an all-silent cut (no audio anywhere, no bed) is ACCEPTED", code4 == 200 and body4.get("ok"), (code4, body4))
if code4 == 200 and body4.get("ok"):
    entry4 = wait_cut_done(URL, seq4["id"], body4["cut_id"], timeout=90)
    check("an all-silent cut reaches status done (never corrupted by a -inf loudnorm target)",
          entry4 is not None and entry4.get("status") == "done", entry4)
    if entry4 and entry4.get("status") == "done":
        out4 = os.path.join(DATA_DIR, "seq", seq4["id"], entry4.get("file") or ("cuts/%s.mp4" % body4["cut_id"]))
        check("the all-silent output file exists", os.path.isfile(out4), out4)
        if os.path.isfile(out4):
            info4 = ffprobe_json(out4, ["-show_streams"])
            a4 = next((s for s in info4.get("streams", []) if s.get("codec_type") == "audio"), None)
            check("the all-silent output still carries a (silent) audio stream", a4 is not None, info4)
            sr4 = audio_sample_rate(out4)
            check("the all-silent output's audio is still 48000 Hz", sr4 == 48000, sr4)
        check("the cut's own record marks this a SILENT loudness (normalising was skipped, not applied "
              "to a -inf target)", entry4.get("loudness") == "silent", entry4.get("loudness"))


# ---------------------------------------------------------------------------
print("R4 review fix (2026-09-23), part 3: the ENCODED (post-AAC) true peak of a hot, transient-heavy "
      "shot must stay <= -1.0 dBTP -- loudnorm's own TP target only controls the pre-encode signal; "
      "the AAC step after it can overshoot by a few tenths of a dB on real content (measured live: "
      "-0.9 dBTP on an actual LTX cut)")

seq5 = seq_create(URL, "R4 encoded true peak")
after5 = add_video_slot(URL, seq5["id"], seq5["rev"])
slot5 = after5["slots"][0]["id"]
clip5 = os.path.join(FAKE_OUT, "hot_burst.mp4")
make_hot_burst_clip(clip5, CANVAS_W, CANVAS_H, bed_db=-16, spike=0.05, duration=3.0, seed=1)
job5 = generate(URL, seq5["id"], slot5, FAKE_PORT, clip5)
wait_true("(setup) hot-burst take harvested", lambda: harvested_take(URL, seq5["id"], slot5, job5), 15)
rev5 = seq_get(URL, seq5["id"])["rev"]
rev5 = pick(URL, seq5["id"], rev5, slot5, job5)["rev"]

code5, body5 = do_cut(URL, seq5["id"])
check("R4 encoded-true-peak cut accepted", code5 == 200 and body5.get("ok"), (code5, body5))
if code5 == 200 and body5.get("ok"):
    entry5 = wait_cut_done(URL, seq5["id"], body5["cut_id"], timeout=90)
    check("R4 encoded-true-peak cut reaches status done", entry5 is not None and entry5.get("status") == "done", entry5)
    if entry5 and entry5.get("status") == "done":
        out5 = os.path.join(DATA_DIR, "seq", seq5["id"], entry5.get("file") or ("cuts/%s.mp4" % body5["cut_id"]))
        check("R4 encoded-true-peak output file exists", os.path.isfile(out5), out5)
        if os.path.isfile(out5):
            tp5 = true_peak_ebur128(out5)
            check("the DELIVERED (post-AAC) file's true peak is <= -1.0 dBTP on hot, transient-heavy "
                  "content (got %r)" % tp5, tp5 is not None and tp5 <= -1.0, tp5)
            i5 = measured_integrated_loudness(out5)
            note("R4 encoded-true-peak (hot bed+spike) case: measured I=%r LUFS, TP=%r dBTP" % (i5, tp5))


# ---------------------------------------------------------------------------
print("R4 review fix (2026-09-23), part 4: a full-scale burst amid otherwise SILENCE (quiet dialogue, "
      "then one loud moment) must not push the DELIVERED file's true peak past -1.0 dBTP either -- "
      "loudnorm's own TP target alone cannot hold this (EBU R128 gating excludes the silent stretch from "
      "the loudness measurement, so its linear-mode gain overcorrects); the post-loudnorm limiter is "
      "the backstop that has to catch it")

seq6 = seq_create(URL, "R4 burst in silence")
after6 = add_video_slot(URL, seq6["id"], seq6["rev"])
slot6 = after6["slots"][0]["id"]
clip6 = os.path.join(FAKE_OUT, "burst_in_silence.mp4")
make_burst_in_silence_clip(clip6, CANVAS_W, CANVAS_H, burst=0.05, duration=3.0, seed=42)
job6 = generate(URL, seq6["id"], slot6, FAKE_PORT, clip6)
wait_true("(setup) burst-in-silence take harvested", lambda: harvested_take(URL, seq6["id"], slot6, job6), 15)
rev6 = seq_get(URL, seq6["id"])["rev"]
rev6 = pick(URL, seq6["id"], rev6, slot6, job6)["rev"]

code6, body6 = do_cut(URL, seq6["id"])
check("R4 burst-in-silence cut accepted", code6 == 200 and body6.get("ok"), (code6, body6))
if code6 == 200 and body6.get("ok"):
    entry6 = wait_cut_done(URL, seq6["id"], body6["cut_id"], timeout=90)
    check("R4 burst-in-silence cut reaches status done", entry6 is not None and entry6.get("status") == "done", entry6)
    if entry6 and entry6.get("status") == "done":
        out6 = os.path.join(DATA_DIR, "seq", seq6["id"], entry6.get("file") or ("cuts/%s.mp4" % body6["cut_id"]))
        check("R4 burst-in-silence output file exists", os.path.isfile(out6), out6)
        if os.path.isfile(out6):
            tp6 = true_peak_ebur128(out6)
            check("the DELIVERED (post-AAC) file's true peak is <= -1.0 dBTP even for a full-scale "
                  "burst amid silence (got %r)" % tp6, tp6 is not None and tp6 <= -1.0, tp6)
            i6 = measured_integrated_loudness(out6)
            note("R4 burst-in-silence case: measured I=%r LUFS, TP=%r dBTP" % (i6, tp6))


# ---------------------------------------------------------------------------
print("F5: the cut's true peak is guaranteed on ANY content, by measuring the "
      "DELIVERED (encoded) file and correcting once if it is over -1.0 dBTP -- swept over "
      "seeded hot-spot noise sources of two shapes: a steady bed with a brief hot spike "
      "(spike=0.05s, bed=-16dB, seeds 1-5 -- seeds 3 and 4 measure over -1.0 dBTP BEFORE "
      "correction on this repo's ffmpeg 8), and a full-scale burst amid otherwise silence "
      "(burst=0.08s seed=1, 0.06s seed=10, 0.04s seed=10 -- these measure over -1.0 dBTP "
      "BEFORE correction on ffmpeg 4.4.2, where the hot-bed+spike shape above "
      "never quite breaches -- the two AAC encoder builds overshoot on different content, so "
      "both shapes are swept to prove the path on whichever build is running; see the "
      "builder report for the parameter sweep both were picked from). Every DELIVERED "
      "output must be <= -1.0 dBTP, and at least one trial must have gone through the "
      "correction pass (peak_corrected: true), proving the path actually runs, not just "
      "that the content happened to already be under the ceiling.")

F5_TRIALS = (
    [("hot spike seed %d" % s, make_hot_burst_clip,
      dict(bed_db=-16, spike=0.05, duration=3.0, seed=s)) for s in (1, 2, 3, 4, 5)]
    + [("burst-in-silence %ss seed %d" % (b, s), make_burst_in_silence_clip, dict(burst=b, duration=3.0, seed=s))
       for b, s in ((0.08, 1), (0.06, 10), (0.04, 10))]
)
f5_any_corrected = False
for i, (label, maker, kwargs) in enumerate(F5_TRIALS):
    seqf5 = seq_create(URL, "F5 trial %d" % i)
    afterf5 = add_video_slot(URL, seqf5["id"], seqf5["rev"])
    slotf5 = afterf5["slots"][0]["id"]
    clipf5 = os.path.join(FAKE_OUT, "f5_%d.mp4" % i)
    maker(clipf5, CANVAS_W, CANVAS_H, **kwargs)
    jobf5 = generate(URL, seqf5["id"], slotf5, FAKE_PORT, clipf5)
    wait_true("(setup) F5 %s take harvested" % label,
              lambda: harvested_take(URL, seqf5["id"], slotf5, jobf5), 15)
    revf5 = seq_get(URL, seqf5["id"])["rev"]
    revf5 = pick(URL, seqf5["id"], revf5, slotf5, jobf5)["rev"]

    codef5, bodyf5 = do_cut(URL, seqf5["id"])
    check("F5 %s: cut accepted" % label, codef5 == 200 and bodyf5.get("ok"), (codef5, bodyf5))
    if not (codef5 == 200 and bodyf5.get("ok")):
        continue
    entryf5 = wait_cut_done(URL, seqf5["id"], bodyf5["cut_id"], timeout=90)
    check("F5 %s: cut reaches status done" % label,
          entryf5 is not None and entryf5.get("status") == "done", entryf5)
    if not (entryf5 and entryf5.get("status") == "done"):
        continue
    outf5 = os.path.join(DATA_DIR, "seq", seqf5["id"], entryf5.get("file") or ("cuts/%s.mp4" % bodyf5["cut_id"]))
    check("F5 %s: output file exists" % label, os.path.isfile(outf5), outf5)
    tp_server = entryf5.get("true_peak_db")
    check("F5 %s: cut record carries true_peak_db" % label, isinstance(tp_server, (int, float)), entryf5)
    if os.path.isfile(outf5):
        tp_measured = true_peak_ebur128(outf5)
        check("F5 %s: DELIVERED file's true peak is <= -1.0 dBTP (server said %r, "
              "measured independently %r)" % (label, tp_server, tp_measured),
              tp_measured is not None and tp_measured <= -1.0, tp_measured)
    if entryf5.get("peak_corrected"):
        f5_any_corrected = True
    note("F5 %s: true_peak_db=%r peak_corrected=%r" % (label, tp_server, entryf5.get("peak_corrected")))

check("F5: at least one of the %d trials went through the peak-correction pass "
      "(peak_corrected: true) -- proves the correction path actually runs" % len(F5_TRIALS),
      f5_any_corrected, f5_any_corrected)


# ---------------------------------------------------------------------------
print("F5 multi-pass convergence: one corrective pass does not always land under the ceiling "
      "-- a re-encode's own AAC overshoot differs from the first pass's (found live, "
      "2026-09-24: a single correction still measured -0.6 dBTP on unseeded content) -- so "
      "up to 3 CUMULATIVE passes must run, re-measuring the real encoded file each time. "
      "hot bed=-16dB, spike=0.07s, seed=6 is a known-hard case on this repo's ffmpeg 8 "
      "(this box): it needs exactly 2 passes to land under -1.0 dBTP (deterministic across "
      "3 reruns while tuning this check -- see the builder report's parameter sweep). On "
      "ffmpeg 4.4.2 the same content needs 0 passes -- it never breaches -1.0 in "
      "the first place, no seed swept there needed even 1 -- so the pass count is only noted, "
      "not asserted; the ceiling itself (<= -1.0 dBTP, delivered) is still asserted "
      "unconditionally, and 'the cut still reaches status done rather than erroring out after "
      "MAX_PASSES' is asserted unconditionally too, which is what the single-pass code (RED, "
      "see the builder report) actually failed on this exact seed.")

seqf5b = seq_create(URL, "F5 hard seed")
afterf5b = add_video_slot(URL, seqf5b["id"], seqf5b["rev"])
slotf5b = afterf5b["slots"][0]["id"]
clipf5b = os.path.join(FAKE_OUT, "f5_hard_seed.mp4")
make_hot_burst_clip(clipf5b, CANVAS_W, CANVAS_H, bed_db=-16, spike=0.07, duration=3.0, seed=6)
jobf5b = generate(URL, seqf5b["id"], slotf5b, FAKE_PORT, clipf5b)
wait_true("(setup) F5 hard-seed take harvested",
          lambda: harvested_take(URL, seqf5b["id"], slotf5b, jobf5b), 15)
revf5b = seq_get(URL, seqf5b["id"])["rev"]
revf5b = pick(URL, seqf5b["id"], revf5b, slotf5b, jobf5b)["rev"]

codef5b, bodyf5b = do_cut(URL, seqf5b["id"])
check("F5 hard seed: cut accepted", codef5b == 200 and bodyf5b.get("ok"), (codef5b, bodyf5b))
if codef5b == 200 and bodyf5b.get("ok"):
    entryf5b = wait_cut_done(URL, seqf5b["id"], bodyf5b["cut_id"], timeout=90)
    check("F5 hard seed: cut reaches status done (not stuck in error after MAX_PASSES)",
          entryf5b is not None and entryf5b.get("status") == "done", entryf5b)
    if entryf5b and entryf5b.get("status") == "done":
        outf5b = os.path.join(DATA_DIR, "seq", seqf5b["id"],
                              entryf5b.get("file") or ("cuts/%s.mp4" % bodyf5b["cut_id"]))
        check("F5 hard seed: output file exists", os.path.isfile(outf5b), outf5b)
        # Pass count is a build detail, not asserted here (this exact seed needs 0 correction
        # passes on ffmpeg 4.4.2 and 2 on this box's ffmpeg 8 -- see note below);
        # what's universal is the delivered ceiling, checked unconditionally just below.
        passes_f5b = entryf5b.get("peak_passes")
        if os.path.isfile(outf5b):
            tp_f5b = true_peak_ebur128(outf5b)
            check("F5 hard seed: DELIVERED file's true peak is <= -1.0 dBTP (server said %r, "
                  "measured independently %r, after %r pass(es))"
                  % (entryf5b.get("true_peak_db"), tp_f5b, passes_f5b),
                  tp_f5b is not None and tp_f5b <= -1.0, tp_f5b)
        if passes_f5b == 2:
            note("F5 hard seed: needed exactly 2 passes, matching this check's reference build (ffmpeg 8)")
        else:
            note("F5 hard seed: needed %r pass(es) on this build (reference build ffmpeg 8 needs 2 -- "
                 "AAC overshoot is encoder-build-dependent, see the builder report)" % passes_f5b)


print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
