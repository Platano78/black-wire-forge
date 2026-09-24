"""Regression gate for the C3.6 review fix (2026-09-23, live data): R5 no
longer requires a picked take to equal the SEQUENCE CANVAS's exact pixel
size. A pack may render at its declared width/height and DELIVER a
multiple of it (LTX delivers 2x -- engines/ltx.py: "delivered video is 2x
this"), so a live LTX sequence's canvas (1024x576) and its takes' real
pixel size (2048x1152, ffprobed with ffmpeg 4.4.2) legitimately differ while
sharing the same aspect ratio. The revised rule:

  - the cut's own OUTPUT size is the largest picked video take, by area
    (floored to even width/height -- yuv420p);
  - a take whose aspect ratio matches the SEQUENCE CANVAS within 1% is
    scaled (scale=W:H:flags=lanczos, setsar=1) to the output size, inside
    that clip's own filter chain, before its title;
  - a take whose aspect ratio does NOT match the canvas is still refused,
    naming the shot and both sizes -- tests/test_cut.py's own "wrong
    picture size" case (canvas+64 on each axis, a different aspect ratio)
    must keep refusing under this file too (checked below, not just
    tests/test_cut.py, since that scenario now shares code with the
    scaling path this file exercises).

Calibrated on ffmpeg 4.4.2 (the oldest version the cut pipeline supports),
same as tests/test_cut.py. Run it locally:

    python3 tests/test_cut_scale.py
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


def make_clip(path, width, height, duration=3.0, fps=24, sample_rate=48000):
    ffmpeg(["-f", "lavfi", "-i", "color=c=red:size=%dx%d:rate=%s:duration=%s" % (width, height, fps, duration),
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=%d:duration=%s" % (sample_rate, duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path], "make_clip")
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


def start_server(scratch, lane_port, name="main"):
    data_dir = os.path.join(scratch, "data_%s" % name)
    cfg_path = os.path.join(scratch, "config_%s.json" % name)
    port = free_port()
    cfg = {"title": "C3.6 scale test", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Target lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"]}],
           "timing": {"poll_seconds": 0.3, "job_poll_seconds": 0.3,
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


def add_video_slot(url, sid, rev, length=121):
    code, body = seq_op(url, sid, rev, "add_slot", lane="video", cap="video", mode="ltx",
                        values={"prompt": "a test shot", "length": length})
    if code != 200:
        raise RuntimeError("add_slot failed: %r" % (body,))
    return body


def generate(url, sid, slot_id, lane_port, clip_path):
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


def wait_cut_done(url, sid, cut_id, timeout=90):
    def get_entry():
        seq = seq_get(url, sid)
        entry = next((c for c in seq.get("cuts") or [] if c.get("id") == cut_id), None)
        if entry and entry.get("status") in ("done", "error", "interrupted"):
            return entry
        return None
    ok, entry = wait_true("cut %s reaches a terminal status" % cut_id, get_entry, timeout, interval=0.5)
    return entry if ok else None


def harvested(url, sid, slot_id, job_id):
    seq = seq_get(url, sid)
    slot = next(s for s in seq["slots"] if s["id"] == slot_id)
    take = next((t for t in slot["takes"] if t["job_id"] == job_id), None)
    return take if take and take.get("file") else None


SCRATCH = tempfile.mkdtemp(prefix="bwf_cutscale_")
print("scratch dir: %s (real data/ and the live ~/black-wire-forge are never touched)" % SCRATCH)

import atexit
def _cleanup():
    for p in PROCS:
        stop(p)
    import shutil
    shutil.rmtree(SCRATCH, ignore_errors=True)
atexit.register(_cleanup)

FAKE_PROC, FAKE_PORT, FAKE_STORE = start_fake_lane(SCRATCH, "t")
FAKE_OUT = os.path.join(FAKE_STORE, "outputs")
_srv_proc, URL, DATA_DIR = start_server(SCRATCH, FAKE_PORT, name="main")

_probe_seq = seq_create(URL, "canvas probe")
CANVAS_W, CANVAS_H = _probe_seq["canvas"]["width"], _probe_seq["canvas"]["height"]
note("sequence canvas is %dx%d" % (CANVAS_W, CANVAS_H))


# ---------------------------------------------------------------------------
print("a canvas-size take + a 2x-canvas take, same aspect -> cut done at the LARGER size (LTX's own "
      "'declared size vs 2x delivered' shape)")

seq1 = seq_create(URL, "scale mix")
rev = seq1["rev"]
after_a = add_video_slot(URL, seq1["id"], rev)
slot_a = after_a["slots"][0]["id"]
rev = after_a["rev"]
after_b = add_video_slot(URL, seq1["id"], rev)
slot_b = after_b["slots"][1]["id"]

clip_a = os.path.join(FAKE_OUT, "scale_canvas.mp4")
make_clip(clip_a, CANVAS_W, CANVAS_H, duration=2.0)
job_a = generate(URL, seq1["id"], slot_a, FAKE_PORT, clip_a)
ok, _ = wait_true("(setup) canvas-size take harvested", lambda: harvested(URL, seq1["id"], slot_a, job_a), 15)

clip_b = os.path.join(FAKE_OUT, "scale_2x.mp4")
make_clip(clip_b, CANVAS_W * 2, CANVAS_H * 2, duration=2.0)
job_b = generate(URL, seq1["id"], slot_b, FAKE_PORT, clip_b)
ok, _ = wait_true("(setup) 2x-canvas take harvested", lambda: harvested(URL, seq1["id"], slot_b, job_b), 15)

rev1 = seq_get(URL, seq1["id"])["rev"]
rev1 = pick(URL, seq1["id"], rev1, slot_a, job_a)["rev"]
rev1 = pick(URL, seq1["id"], rev1, slot_b, job_b)["rev"]

D_a = None
D_b = None
info_a = ffprobe_json(os.path.join(DATA_DIR, "seq", seq1["id"], "takes", job_a + ".mp4"), ["-show_format"])
info_b = ffprobe_json(os.path.join(DATA_DIR, "seq", seq1["id"], "takes", job_b + ".mp4"), ["-show_format"])
D_a = float(info_a.get("format", {}).get("duration") or 2.0)
D_b = float(info_b.get("format", {}).get("duration") or 2.0)
total_len = D_a + D_b

code, body = do_cut(URL, seq1["id"])
check("cut ACCEPTED for a canvas-size take + a 2x-canvas take, same aspect ratio (never refused for "
      "a size a pack legitimately delivers)", code == 200 and body.get("ok"), (code, body))
if code == 200 and body.get("ok"):
    entry = wait_cut_done(URL, seq1["id"], body["cut_id"], timeout=120)
    check("that cut reaches status done", entry is not None and entry.get("status") == "done", entry)
    if entry and entry.get("status") == "done":
        rel = entry.get("file") or ("cuts/%s.mp4" % body["cut_id"])
        out_path = os.path.join(DATA_DIR, "seq", seq1["id"], rel)
        check("the finished cut file exists", os.path.isfile(out_path), out_path)
        if os.path.isfile(out_path):
            info_out = ffprobe_json(out_path, ["-show_streams", "-show_format"])
            v_out = next((s for s in info_out.get("streams", []) if s.get("codec_type") == "video"), None)
            check("output size is the LARGEST picked take (2x canvas), not the canvas itself",
                  v_out is not None and v_out.get("width") == CANVAS_W * 2 and v_out.get("height") == CANVAS_H * 2,
                  v_out)
            check("output r_frame_rate == 24/1", v_out is not None and v_out.get("r_frame_rate") == "24/1", v_out)
            check("output avg_frame_rate == 24/1", v_out is not None and v_out.get("avg_frame_rate") == "24/1", v_out)
            rc, out, err = run(["ffprobe", "-v", "quiet", "-count_frames", "-select_streams", "v:0",
                                 "-show_entries", "stream=nb_read_frames", "-print_format", "json", out_path])
            nframes = None
            try:
                nframes = int(json.loads(out)["streams"][0]["nb_read_frames"])
            except Exception:
                pass
            expect_frames = round(24 * total_len)
            check("frame count == round(24 * sum(len)) +/-1 (scaling never touches timing) "
                  "(got %r, expected ~%d)" % (nframes, expect_frames),
                  nframes is not None and abs(nframes - expect_frames) <= 1, (nframes, expect_frames))


# ---------------------------------------------------------------------------
print("a take with a DIFFERENT aspect ratio from the canvas is still refused, even as the sole picked "
      "video slot (the largest-take rule never lets an odd shape become the output unchallenged)")

seq2 = seq_create(URL, "scale wrong aspect")
after2 = add_video_slot(URL, seq2["id"], seq2["rev"])
slot2 = after2["slots"][0]["id"]
clip2 = os.path.join(FAKE_OUT, "scale_wrongaspect.mp4")
make_clip(clip2, CANVAS_W + 64, CANVAS_H + 64, duration=2.0)   # same clip shape test_cut.py's R5 uses
job2 = generate(URL, seq2["id"], slot2, FAKE_PORT, clip2)
wait_true("(setup) wrong-aspect take harvested", lambda: harvested(URL, seq2["id"], slot2, job2), 15)
rev2 = seq_get(URL, seq2["id"])["rev"]
rev2 = pick(URL, seq2["id"], rev2, slot2, job2)["rev"]
code2, body2 = do_cut(URL, seq2["id"])
check("a take whose aspect ratio differs from the canvas is REFUSED, even as the only picked shot",
      400 <= code2 < 500 and isinstance(body2, dict) and isinstance(body2.get("error"), str) and body2["error"].strip(),
      (code2, body2))
if isinstance(body2, dict) and isinstance(body2.get("error"), str):
    check("the refusal names the shot", slot2 in body2["error"], body2["error"])
if code2 == 200 and isinstance(body2, dict) and body2.get("cut_id"):
    wait_cut_done(URL, seq2["id"], body2["cut_id"], timeout=30)


print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
