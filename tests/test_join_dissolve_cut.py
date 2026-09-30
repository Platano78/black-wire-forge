"""Acceptance gate for E2 "join: dissolve", gates 3-5 of the build spec (R3/R4):

  3. two/three continue-linked shots whose later takes kept their overlap cut
     with a cross-dissolve over it: the output has exactly the hard join's
     frame count (the same shots made trimmed, cut today's way); a frame in the
     middle of each seam is a blend of the two sides' distinct colours; both
     sides' sound is present mid-seam (uncorrelated
     noise, so each side is measured apart: each at -6 dB mid-seam, equal
     GAIN). With the overlap's pictures and sound equal to the shot before's
     last frames (as the render's are: correlated), the cut plays as one
     unbroken take and the level holds through the seam (within 1 dB; equal
     power would swell ~+3 dB).
  4. Sing along: a singing head + a singing continue that kept its overlap ->
     the cut's audio is the song from the head's start (xcorr lag < 25 ms) and
     the second shot's first NEW frame lands where E1 put it.
  5. a user trim that leaves less than the whole overlap -> a straight cut at
     that seam, the overlap frames dropped, and a sentence in the cut record.

Every take is made through seq_generate (the take's inputs.join is the real
record); the fake lane 'renders' a synthetic mp4. Real ffmpeg.
Run: python3 tests/test_join_dissolve_cut.py
"""
import importlib.util, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg/ffprobe not on PATH -- this suite needs both")
    sys.exit(0)
try:
    import numpy as np
except ImportError:
    print("SKIP: numpy is not installed (requirements.txt) -- this suite's checks need it")
    sys.exit(0)

import engines

FAILED = []
def check(name, cond, detail=""):
    shown = "" if isinstance(detail, str) and detail == "" else "  " + str(detail)
    print(("  PASS  " if cond else "  FAIL  ") + name + (shown if not cond else ""))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port

def ff(args):
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-1500:])

SR = 8000
W, H = 320, 192
FW, FH = 32, 16   # analysis frame size


def pcm(path, start=0.0, dur=None, sr=SR):
    args = ["ffmpeg", "-v", "error", "-ss", "%.6f" % start, "-i", path] + (["-t", "%.6f" % dur] if dur else [])
    r = subprocess.run(args + ["-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64)


def frames(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=%d:%d" % (FW, FH), "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.uint8).reshape(-1, FH, FW, 3).astype(np.float64)


def rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12)


def lag_ms(cut, ref, max_s=0.5):
    n = min(len(cut), len(ref))
    a, b = cut[:n] - cut[:n].mean(), ref[:n] - ref[:n].mean()
    size = 1 << (2 * n - 1).bit_length()
    xc = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    m = int(max_s * SR)
    window = np.concatenate([xc[-m:], xc[:m + 1]])
    k = int(np.argmax(window)) - m
    return 1000.0 * k / SR, float(window.max() / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-20))


SCRATCH = tempfile.mkdtemp(prefix="bwf_join_cut_")
print("scratch dir: %s" % SCRATCH)
STORE = os.path.join(SCRATCH, "store_t")
OUT = os.path.join(STORE, "outputs")
os.makedirs(OUT)
PORT_T = free_port()
FAKE_T = subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"),
                           "--port", str(PORT_T), "--store", STORE],
                          cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % PORT_T, timeout=0.5)
        break
    except Exception:
        time.sleep(0.05)
else:
    raise SystemExit("fake lane did not come up")

CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "e2 cut",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 5.0},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": PORT_T,
                      "caps": ["image", "video", "audio"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
DATA = os.path.join(SCRATCH, "data")
os.environ["GENCENTER_DATA"] = DATA
MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
for _role in engines.model_keys():
    if not MODELS.get(_role):
        MODELS[_role] = "stub_%s.safetensors" % _role
spec = importlib.util.spec_from_file_location("srv_join_cut", os.path.join(ROOT, "server.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.JOBS_FILE = os.path.join(DATA, "jobs.json")
mod.SEQ_DIR = os.path.join(DATA, "sequences")
mod.SEQ_MEDIA_DIR = os.path.join(DATA, "seq")
mod.CHAIN_DIR = os.path.join(DATA, "chain")
mod.LOCAL_OUTPUTS_DIR = os.path.join(DATA, "outputs")
for d in (mod.SEQ_DIR, mod.SEQ_MEDIA_DIR, mod.CHAIN_DIR, mod.LOCAL_OUTPUTS_DIR):
    os.makedirs(d, exist_ok=True)
mod.LANE_BY_ID["t"]["models"] = dict(MODELS)
with mod.STATE_LOCK:
    mod.LANE_STATE["t"] = {"up": True, "checked": time.time(), "err": ""}

JOINS = {}
for pack in engines.packs():
    if pack["cap"] == "video":
        JOINS.update(pack.get("joins") or {})
MODE = next(iter(JOINS), None)
FIELD = (JOINS.get(MODE) or {}).get("field")
OV = (JOINS.get(MODE) or {}).get("overlap_frames", 0)
JACK = next((f["id"] for f in engines.fields("video", MODE) if f.get("type") == "video"), None) if MODE else None
check("sanity: a mode declares joins, with a video jack", bool(MODE and FIELD and OV and JACK), JOINS)

# -- media ----------------------------------------------------------------------
RED, BLUE, GREEN = (255, 0, 0), (0, 0, 255), (0, 255, 0)


def colour_clip(name, spans, seed):
    """spans: [(rgb, frames), ...] -> an mp4 with those solid colours in turn, and
    its own 32 kHz noise (seeded, so two clips are uncorrelated) as audio."""
    total = sum(n for _, n in spans)
    ins, fc = [], []
    for k, (rgb, n) in enumerate(spans):
        ins += ["-f", "lavfi", "-i", "color=c=0x%02x%02x%02x:size=%dx%d:rate=24:d=%.6f" % (rgb + (W, H, n / 24.0))]
    fc.append("".join("[%d:v]" % k for k in range(len(spans))) + "concat=n=%d:v=1:a=0[v]" % len(spans))
    ins += ["-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.25:seed=%d:sample_rate=32000:duration=%.6f"
            % (seed, total / 24.0)]
    ff(ins + ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "%d:a" % len(spans),
              "-frames:v", str(total), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "12",
              "-c:a", "aac", "-b:a", "192k", os.path.join(OUT, name)])
    return name


def pattern_clip(name, first, n):
    """testsrc2 frames [first, first+n) and the same span of one seeded noise --
    so a later clip whose first frames repeat an earlier one's last frames is the
    render's overlap, pictures AND (correlated) sound."""
    ff(["-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=24" % (W, H), "-f", "lavfi", "-i",
        "anoisesrc=color=white:amplitude=0.25:seed=11:sample_rate=32000",
        "-vf", "trim=start_frame=%d:end_frame=%d,setpts=PTS-STARTPTS" % (first, first + n),
        # band-limited, so a shot starting between two audio samples (26/24 s at
        # 32 kHz) still repeats the same sound (white noise would decorrelate)
        "-af", "lowpass=f=2000,lowpass=f=2000,atrim=start=%.6f:duration=%.6f,asetpts=PTS-STARTPTS"
        % (first / 24.0, n / 24.0),
        "-frames:v", str(n), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "10", "-c:a", "aac",
        os.path.join(OUT, name)])
    return name


def op(sid, name, **kw):
    b, c = mod.seq_op(dict(kw, id=sid, rev=mod.seq_get(sid)[0]["rev"], op=name))
    assert c == 200, (name, b)
    return b


def slot_of(sid, slot_id):
    return next(s for s in mod.seq_get(sid)[0]["slots"] if s["id"] == slot_id)


def control(outputs):
    req = urllib.request.Request("http://127.0.0.1:%d/_control/accept" % PORT_T,
                                 data=json.dumps({"outputs": outputs}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def shot(sid, fname, join=True, cable_from=None, length=124, sing=None):
    """A continue shot made through seq_generate (so its take records what its
    render did), whose render is `fname`."""
    values = {f["id"]: f["default"] for f in engines.fields("video", MODE) if f.get("default") is not None}
    values.update(prompt="it carries on", length=length, width=W, height=H)
    values[FIELD] = join
    v = op(sid, "add_slot", lane="video", cap="video", mode=MODE, values=values)["added_slot_id"]
    if cable_from:
        op(sid, "patch", **{"from": cable_from, "to": v, "field": JACK})
    if sing is not None:
        op(sid, "set_sing", slot_id=v, on=True)
        op(sid, "set_sing", slot_id=v, start=sing)
    control([{"filename": fname, "subfolder": "", "type": "output"}])
    body, code = mod.seq_generate({"id": sid, "slot_id": v})
    assert code == 200, body
    jid = body["job"]["id"]
    with mod.JOBS_LOCK:
        mod.JOBS[jid].update(status="done", outputs=[{"filename": fname, "subfolder": "", "type": "output",
                                                      "media": "video"}])
    op(sid, "pick_take", slot_id=v, job_id=jid)
    return v


def new_seq(title, song=None):
    seq, _ = mod.seq_create({"title": title, "mode": "sequence"})
    sid = seq["id"]
    op(sid, "set_canvas", width=W, height=H)
    if song:
        s_id = op(sid, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "a song"})["added_slot_id"]
        with mod.JOBS_LOCK:
            mod.JOBS["song_" + sid] = {"id": "song_" + sid, "lane": "t", "kind": "audio", "mode": "sfx",
                                       "status": "done", "prompt": "x", "outputs": [
                                           {"filename": song, "subfolder": "", "type": "output", "media": "audio"}]}
        mod.seq_add_take(sid, s_id, "song_" + sid)
        op(sid, "pick_take", slot_id=s_id, job_id="song_" + sid)
    return sid


def cut(sid):
    body, code = mod.seq_cut_start({"id": sid})
    assert code == 200, body
    for _ in range(900):
        entry = next(c for c in mod.seq_get(sid)[0]["cuts"] if c["id"] == body["cut_id"])
        if entry["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    return entry, os.path.join(mod.SEQ_MEDIA_DIR, sid, entry.get("file") or "missing")


def take_of(sid, v):
    s = slot_of(sid, v)
    return next(t for t in s["takes"] if t["job_id"] == s["pick"])


def mean_rgb(fr):
    return fr.reshape(-1, 3).mean(axis=0)


def near(rgb, want, tol=40):
    return all(abs(a - b) <= tol for a, b in zip(rgb, want))


if MODE and FIELD and JACK:
    A = colour_clip("a48_red.mp4", [(RED, 48)], 1)
    B = colour_clip("b70_blue.mp4", [(BLUE, 70)], 2)
    C = colour_clip("c70_green.mp4", [(GREEN, 70)], 3)
    Bt = colour_clip("b48_blue.mp4", [(BLUE, 70 - OV)], 2)
    Ct = colour_clip("c48_green.mp4", [(GREEN, 70 - OV)], 3)

    print("\ngate 3: three continue-linked shots whose later takes kept their %d overlap frames" % OV)
    sid = new_seq("dissolve")
    v1 = shot(sid, A, join=True)
    v2 = shot(sid, B, join=True, cable_from=v1)
    v3 = shot(sid, C, join=True, cable_from=v2)
    check("(setup) the two cabled takes record inputs.join; the head does not",
          [take_of(sid, v)["inputs"].get("join") for v in (v1, v2, v3)]
          == [None, {"dissolve": True, "overlap": OV}, {"dissolve": True, "overlap": OV}])
    entry, out = cut(sid)
    check("the cut finishes", entry["status"] == "done", entry.get("log"))
    check("its record: 2 dissolves and a line saying so",
          entry.get("dissolves") == 2 and "2 shot joins blended" in (entry.get("join_note") or ""), entry)

    print("\n...against the hard join: the same shots made trimmed (field off), cut today's way")
    sid_h = new_seq("hard")
    h1 = shot(sid_h, A, join=False)
    h2 = shot(sid_h, Bt, join=False, cable_from=h1)
    h3 = shot(sid_h, Ct, join=False, cable_from=h2)
    entry_h, out_h = cut(sid_h)
    check("(setup) the hard-join cut finishes, no join fields in its record",
          entry_h["status"] == "done" and "dissolves" not in entry_h and "join_note" not in entry_h, entry_h)
    if entry["status"] == "done" and entry_h["status"] == "done":
        fr, fr_h = frames(out), frames(out_h)
        print("     frames: dissolve %d, hard join %d (48 + 70 + 70 - 2x%d = %d)" % (len(fr), len(fr_h), OV,
                                                                                    48 + 140 - 2 * OV))
        check("output frame count == the hard join's frame count", len(fr) == len(fr_h) == 48 + 140 - 2 * OV,
              (len(fr), len(fr_h)))
        s1, s2 = 48 - OV, 48 + 70 - 2 * OV   # first frame of each seam
        mid1, mid2 = s1 + OV // 2, s2 + OV // 2
        m1, m2 = mean_rgb(fr[mid1]), mean_rgb(fr[mid2])
        print("     mid-seam frame %d mean RGB %s (red|blue), frame %d %s (blue|green)"
              % (mid1, np.round(m1).tolist(), mid2, np.round(m2).tolist()))
        blend = lambda m, a, b: all(min(x, y) + 30 < v < max(x, y) - 30 if x != y else abs(v - x) < 30
                                    for v, x, y in zip(m, a, b))
        check("a frame in the middle of seam 1 is a blend of red and blue", blend(m1, RED, BLUE), m1)
        check("a frame in the middle of seam 2 is a blend of blue and green", blend(m2, BLUE, GREEN), m2)
        check("before/after each seam: the sources' own colours (frames %d, %d, %d)" % (s1 - 2, s2 - 2, len(fr) - 1),
              near(mean_rgb(fr[s1 - 2]), RED) and near(mean_rgb(fr[s2 - 2]), BLUE) and near(mean_rgb(fr[-1]), GREEN))
        hb = [mean_rgb(f) for f in fr_h]
        check("(contrast) the hard join has no blended frame anywhere",
              all(near(m, RED) or near(m, BLUE) or near(m, GREEN) for m in hb))

        # Both sides really are IN the seam, each at -6 dB (equal gain), not a hard cut
        # at some point inside it: fit the cut's audio at the seam's midpoint as
        # ca*A + cb*B (the sources' own UNcorrelated noise, aligned, so the two
        # can be told apart), each against its own level outside the seam.
        # Each shot starts where the one before it ends, less the overlap. The
        # cut's own lag is found, not assumed.
        R = 48000
        o48, srcs = pcm(out, sr=R), [pcm(os.path.join(OUT, f), sr=R) for f in (A, B, C)]
        # a seam's earlier side ends at its pictures' own end (the video stream's duration)
        lens = [float(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                      "stream=duration", "-of", "csv=p=0", os.path.join(OUT, f)],
                                     capture_output=True, text=True).stdout) for f in (A, B, C)]
        starts = [0, int(round((lens[0] - OV / 24.0) * R)), int(round((lens[0] + lens[1] - 2 * OV / 24.0) * R))]
        o0, r0 = o48[:R // 2], srcs[0][:R // 2]
        xc = [float(np.dot(o0[k:k + R // 4], r0[:R // 4])) for k in range(0, R // 50)]
        L = int(np.argmax(xc))   # the cut's own delay (the limiter's lookahead), in samples

        def fit(t, which, n=R // 25):
            i = int(round(t * R))
            cols = [srcs[k][i - starts[k]:i - starts[k] + n] for k in which]
            return np.linalg.lstsq(np.stack(cols, axis=1), o48[i + L:i + L + n], rcond=None)[0]
        for lbl, s0, (x, y) in (("1", s1, (0, 1)), ("2", s2, (1, 2))):
            ca, cb = fit((s0 + OV / 2.0) / 24.0, [x, y])
            # each side against its OWN level just outside the seam
            solo_x, solo_y = fit(s0 / 24.0 - 0.3, [x])[0], fit((s0 + OV) / 24.0 + 0.3, [y])[0]
            ga, gb = 20 * np.log10(abs(ca / solo_x) + 1e-9), 20 * np.log10(abs(cb / solo_y) + 1e-9)
            print("     seam %s midpoint: outgoing side at %.2f dB, incoming side at %.2f dB (equal gain: -6.02 each)"
                  % (lbl, ga, gb))
            check("seam %s: both shots' own sound is in the seam's midpoint, each at -6 dB +/- 1.5 (equal gain)" % lbl,
                  abs(ga + 6.02) <= 1.5 and abs(gb + 6.02) <= 1.5, (ga, gb))

    print("\n...the overlap as the render makes it (the shot before's last frames): one unbroken take")
    sid_p = new_seq("unbroken")
    p1 = shot(sid_p, pattern_clip("p0_48.mp4", 0, 48), join=True)
    p2 = shot(sid_p, pattern_clip("p26_70.mp4", 48 - OV, 70), join=True, cable_from=p1)
    entry_p, out_p = cut(sid_p)
    check("(setup) the cut finishes with one dissolve", entry_p["status"] == "done" and entry_p.get("dissolves") == 1,
          entry_p)
    if entry_p["status"] == "done":
        ref = os.path.join(SCRATCH, "ref96.mp4")
        ff(["-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=24" % (W, H), "-frames:v", "96", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-crf", "10", ref])
        fp, fref = frames(out_p), frames(ref)
        err = [float(np.abs(x - y).mean()) for x, y in zip(fp, fref)]
        print("     frames %d; mean abs error vs the continuous source: max %.2f over the seam, max %.2f elsewhere"
              % (len(fp), max(err[48 - OV:48]), max(err[:48 - OV] + err[48:])))
        check("96 frames, every one (seam included) the continuous source's own frame (mean abs error < 6/255)",
              len(fp) == 96 and max(err) < 6, (len(fp), max(err) if err else None))
        # The overlap's sound is the shot before's own tail (correlated, as the
        # render's is): an equal-gain crossfade keeps the level; equal power
        # would swell ~+3 dB in the middle of the seam.
        a = pcm(out_p, sr=48000)
        t0, t1 = (48 - OV) / 24.0, 48 / 24.0
        lvl = lambda x, y: rms_db(a[int(x * 48000):int(y * 48000)])
        left, right = lvl(t0 - 0.45, t0 - 0.05), lvl(t1 + 0.05, t1 + 0.45)
        mid_t = (t0 + t1) / 2
        mid = lvl(mid_t - 0.04, mid_t + 0.04)
        side = (left + right) / 2
        print("     correlated seam audio: left %.2f dB, midpoint %.2f dB, right %.2f dB (midpoint %+.2f dB)"
              % (left, mid, right, mid - side))
        check("correlated overlap: the midpoint level within 1 dB of the sides (equal gain; equal power swells ~+3)",
              abs(mid - side) <= 1.0 and abs(left - right) <= 1.0, (left, mid, right))
        win = 960   # 20 ms
        wins = lambda x, y: [rms_db(a[k:k + win]) for k in range(int(x * 48000), int(y * 48000) - win + 1, win)]
        seg = wins(t0 - 0.1, t1 + 0.1)
        # The 20 ms windows of this noise-like audio spread naturally by a few dB, so the band is measured on the
        # same clip AWAY from the seam (equal-length stretches just before and after the seam's own windows, as
        # long as the overlap itself), not fixed at +/-2 dB.
        span = t1 - t0
        away = wins(t0 - 0.1 - span, t0 - 0.1) + wins(t1 + 0.1, t1 + 0.1 + span)
        lo, hi, am = min(away), max(away), sum(away) / len(away)
        sm = sum(seg) / len(seg)
        print("     seam windows %.2f..%.2f (mean %.2f dB); away windows %.2f..%.2f (mean %.2f dB)"
              % (min(seg), max(seg), sm, lo, hi, am))
        check("correlated overlap: no gap or click -- every 20 ms window across the seam within 1 dB of the away "
              "windows' range, and the seam's mean level within 1 dB of the away mean (%.2f..%.2f vs %.2f..%.2f)"
              % (min(seg), max(seg), lo, hi),
              len(away) > 0 and min(seg) >= lo - 1 and max(seg) <= hi + 1 and abs(sm - am) <= 1.0)

    print("\ngate 4: Sing along -- a singing head + a singing continue that kept its overlap")
    SONG = os.path.join(OUT, "song.flac")
    ff(["-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.5:duration=70:seed=7:sample_rate=48000",
        "-af", "highpass=f=150,lowpass=f=700,highpass=f=150,lowpass=f=700", "-ac", "2", SONG])
    song_full = pcm(SONG)
    SA = colour_clip("sa124_red.mp4", [(RED, 124)], 4)
    SB = colour_clip("sb124_red_green.mp4", [(RED, OV), (GREEN, 124 - OV)], 5)
    sid_s = new_seq("singing", song="song.flac")
    w1 = shot(sid_s, SA, join=True, sing=23.0)
    w2 = shot(sid_s, SB, join=True, cable_from=w1, sing=0.0)
    tk = take_of(sid_s, w2)
    check("(setup) the continued take sang from 27.25 (E1's start) and kept its overlap",
          (tk["inputs"].get("sing") or {}).get("start") == 27.25 and tk["inputs"].get("join"), tk["inputs"])
    entry_s, out_s = cut(sid_s)
    check("the cut finishes, the song its soundtrack, one dissolve",
          entry_s["status"] == "done" and entry_s.get("soundtrack") == "song" and entry_s.get("dissolves") == 1, entry_s)
    if entry_s["status"] == "done":
        total = (124 + 124 - OV) / 24.0
        got = pcm(out_s)
        lag, peak = lag_ms(got, song_full[int(23.0 * SR):int((23.0 + total) * SR)])
        print("     xcorr vs the song from 23.0 s: lag %.2f ms, normalised peak %.3f" % (lag, peak))
        check("aligned across both shots: lag < 25 ms, and it is the song (peak > 0.8)", abs(lag) < 25 and peak > 0.8,
              (lag, peak))
        n1 = int(124 / 24.0 * SR)
        lag2, _ = lag_ms(got[n1:], song_full[int((23.0 + 124 / 24.0) * SR):])
        check("the second shot's own span lines up too: lag < 25 ms", abs(lag2) < 25, lag2)
        fs = frames(out_s)
        first_new = next((k for k, f in enumerate(fs) if near(mean_rgb(f), GREEN)), None)
        print("     frames %d; first frame of the second shot's new material: %s (E1: 124)" % (len(fs), first_new))
        check("the second shot's first NEW frame lands at frame 124 = 124/24 s, as in E1",
              first_new == 124 and len(fs) == 248 - OV, (first_new, len(fs)))

    print("\ngate 5: a trim that leaves less than the whole overlap -> a straight cut, and a sentence")
    for label, trim_slot, trim in (("the earlier shot's tail trimmed by 5 frames", "first", {"in": 0, "len": 43 / 24.0}),
                                   ("the later shot's in-point moved 5 frames", "second",
                                    {"in": 5 / 24.0, "len": 65 / 24.0})):
        sid_t = new_seq("trim " + trim_slot)
        t1 = shot(sid_t, A, join=True)
        t2 = shot(sid_t, B, join=True, cable_from=t1)
        op(sid_t, "set_trim", slot_id=t1 if trim_slot == "first" else t2, trim=trim)
        entry_t, out_t = cut(sid_t)
        note = entry_t.get("join_note") or ""
        print("     %s -> join_note: %r" % (label, note))
        check("%s: the cut finishes with no dissolve and a sentence naming the seam" % label,
              entry_t["status"] == "done" and entry_t.get("dissolves") == 0 and "straight cut" in note
              and t2 in note and t1 in note, entry_t)
        if entry_t["status"] == "done":
            ft = frames(out_t)
            a_frames = 43 if trim_slot == "first" else 48
            b_new = 70 - OV if trim_slot == "first" else 65 - (OV - 5)
            check("%s: the overlap frames are dropped: %d + %d frames, no blend" % (label, a_frames, b_new),
                  len(ft) == a_frames + b_new and all(near(mean_rgb(f), RED) or near(mean_rgb(f), BLUE) for f in ft)
                  and near(mean_rgb(ft[a_frames - 1]), RED) and near(mean_rgb(ft[a_frames]), BLUE), len(ft))

    print("\na kept-overlap take that starts the cut (its source shot not picked): overlap dropped, and said")
    sid_f = new_seq("first")
    f1 = shot(sid_f, A, join=True)
    f2 = shot(sid_f, B, join=True, cable_from=f1)
    op(sid_f, "pick_take", slot_id=f1, job_id=None)
    entry_f, out_f = cut(sid_f)
    check("the cut finishes, %d frames, with the sentence" % (70 - OV),
          entry_f["status"] == "done" and len(frames(out_f)) == 70 - OV
          and "starts the cut" in (entry_f.get("join_note") or ""), entry_f)

FAKE_T.terminate()
shutil.rmtree(SCRATCH, ignore_errors=True)
print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
