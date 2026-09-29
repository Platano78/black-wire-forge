"""Acceptance gate for E1 "Sing along", gate 4 (R4): the cut of a sequence
with a song and Sing-along shots plays the SONG as its soundtrack, aligned so
each singing shot's first new frame is at song time = its start (+22/24 s for
a cabled continue shot, whose carried frames are trimmed off), with the
singing shots' own audio NOT mixed in (it is the same song: mixing doubles
it). Non-singing spans keep clip audio + the song at the bed's -18 dB. A
timeline that cannot hold one continuous track falls back to each shot's own
sound and says so in the cut's record.

Stand-ins: the song is band-limited noise (150-700 Hz, a sharp xcorr peak);
each clip's own audio is a quiet 3 kHz tone (quiet so AAC's masking does not
eat the -18 dB song under it), so "doubled" = 3 kHz energy in a singing span. Alignment = cross-correlation of the cut's audio against the
song from the head's start: lag < 25 ms.

In-process server.py, a fake ComfyUI lane, real ffmpeg. Run:
    python3 tests/test_sing_along_cut.py
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
    print("SKIP: numpy is not installed (requirements.txt) -- this suite's alignment check needs it")
    sys.exit(0)

import engines

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port

def ff(args):
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-1500:])

SR = 8000   # analysis rate


def pcm(path, start=0.0, dur=None):
    args = ["ffmpeg", "-v", "error", "-ss", "%.6f" % start, "-i", path] + (["-t", "%.6f" % dur] if dur else [])
    r = subprocess.run(args + ["-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True)
    return np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64)


def band_db(x, lo, hi):
    spec = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1.0 / SR)
    return 10 * np.log10(spec[(f >= lo) & (f <= hi)].sum() / len(x) ** 2 + 1e-20)


def lag_ms(cut, ref, max_s=0.5):
    """Cross-correlation lag of `cut` against `ref` (both from the same t0), ms, and the peak's normalised height."""
    n = min(len(cut), len(ref))
    a, b = cut[:n] - cut[:n].mean(), ref[:n] - ref[:n].mean()
    size = 1 << (2 * n - 1).bit_length()
    xc = np.fft.irfft(np.fft.rfft(a, size) * np.conj(np.fft.rfft(b, size)), size)
    m = int(max_s * SR)
    window = np.concatenate([xc[-m:], xc[:m + 1]])
    k = int(np.argmax(window)) - m
    return 1000.0 * k / SR, float(window.max() / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-20))


SCRATCH = tempfile.mkdtemp(prefix="bwf_sing_cut_")
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
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "e1 cut",
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
spec = importlib.util.spec_from_file_location("srv_sing_cut", os.path.join(ROOT, "server.py"))
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

SING = {}
for pack in engines.packs():
    if pack["cap"] == "video":
        SING.update(pack.get("sing_along") or {})
CONT = next((m for m, s in SING.items() if s.get("carried_frames")), None)
JACK = next((f["id"] for f in engines.fields("video", CONT) if f.get("type") == "video"), None) if CONT else None
CARRIED = SING[CONT]["carried_frames"] if CONT else 0
check("sanity: a continue mode that can sing, with a video jack", bool(CONT and JACK), (CONT, JACK))

# -- media --------------------------------------------------------------------
W, H = 320, 192
SONG = os.path.join(OUT, "song.flac")
ff(["-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.5:duration=70:seed=7:sample_rate=48000",
    "-af", "highpass=f=150,lowpass=f=700,highpass=f=150,lowpass=f=700", "-ac", "2", SONG])


def clip(name, frames):
    path = os.path.join(OUT, name)
    ff(["-f", "lavfi", "-i", "color=c=blue:size=%dx%d:rate=24" % (W, H),
        "-f", "lavfi", "-i", "sine=frequency=3000:sample_rate=32000",
        "-frames:v", str(frames), "-t", "%.6f" % (frames / 24.0), "-af", "volume=-24dB",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path])
    return name


def op(sid, name, **kw):
    return mod.seq_op(dict(kw, id=sid, rev=mod.seq_get(sid)[0]["rev"], op=name))


def slot_of(sid, slot_id):
    return next(s for s in mod.seq_get(sid)[0]["slots"] if s["id"] == slot_id)


def done_job(job_id, fname, kind, mode, media):
    with mod.JOBS_LOCK:
        mod.JOBS[job_id] = {"id": job_id, "lane": "t", "kind": kind, "mode": mode, "status": "done", "prompt": "x",
                            "outputs": [{"filename": fname, "subfolder": "", "type": "output", "media": media}]}


def control(outputs):
    req = urllib.request.Request("http://127.0.0.1:%d/_control/accept" % PORT_T,
                                 data=json.dumps({"outputs": outputs}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def shot(sid, length, sing=None, cable_from=None):
    values = {f["id"]: f["default"] for f in engines.fields("video", CONT) if f.get("default") is not None}
    values.update(prompt="he sings", length=length, width=W, height=H)
    b, c = op(sid, "add_slot", lane="video", cap="video", mode=CONT, values=values)
    v = b["added_slot_id"]
    if cable_from:
        op(sid, "patch", **{"from": cable_from, "to": v, "field": JACK})
    if sing is not None:
        op(sid, "set_sing", slot_id=v, on=True)
        op(sid, "set_sing", slot_id=v, start=sing)
    return v


def make(sid, v, fname):
    """seq_generate for real (so the take records what it sang), then the lane 'renders' fname."""
    control([{"filename": fname, "subfolder": "", "type": "output"}])
    body, code = mod.seq_generate({"id": sid, "slot_id": v})
    assert code == 200, body
    done_job(body["job"]["id"], fname, "video", CONT, "video")
    op(sid, "pick_take", slot_id=v, job_id=body["job"]["id"])


def new_seq(title, with_song=True):
    seq, _ = mod.seq_create({"title": title, "mode": "sequence"})
    sid = seq["id"]
    op(sid, "set_canvas", width=W, height=H)
    if with_song:
        b, c = op(sid, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "a song"})
        s_id = b["added_slot_id"]
        done_job("song_" + sid, "song.flac", "audio", "sfx", "audio")
        mod.seq_add_take(sid, s_id, "song_" + sid)
        op(sid, "pick_take", slot_id=s_id, job_id="song_" + sid)
    return sid


def cut(sid):
    body, code = mod.seq_cut_start({"id": sid})
    assert code == 200, body
    for _ in range(600):
        entry = next(c for c in mod.seq_get(sid)[0]["cuts"] if c["id"] == body["cut_id"])
        if entry["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    path = os.path.join(mod.SEQ_MEDIA_DIR, sid, entry.get("file") or "missing")
    return entry, path


A124, B102, C72 = clip("a124.mp4", 124), clip("b102.mp4", 124 - CARRIED), clip("c72.mp4", 72)
song_full = pcm(SONG)

if CONT and JACK:
    print("\n2 Sing-along continue shots (head 23.0 + one cabled): the song IS the soundtrack")
    sid = new_seq("two singing")
    v1 = shot(sid, 124, sing=23.0)
    v2 = shot(sid, 124, sing=0.0, cable_from=v1)
    check("(setup) the cabled shot sings from 23 + (124-22)/24 = 27.25", (slot_of(sid, v2).get("sing_span") or {}).get("start") == 27.25)
    make(sid, v1, A124)
    make(sid, v2, B102)
    entry, out = cut(sid)
    check("the cut finishes", entry["status"] == "done", entry.get("log"))
    check("its record says the song is the soundtrack", entry.get("soundtrack") == "song", entry)
    if entry["status"] == "done":
        total = (124 + 124 - CARRIED) / 24.0
        got = pcm(out)
        ref = song_full[int(23.0 * SR):int((23.0 + total) * SR)]
        lag, peak = lag_ms(got, ref)
        print("     xcorr vs the song from 23.0 s: lag %.2f ms, normalised peak %.3f" % (lag, peak))
        check("aligned: xcorr lag of the cut's audio vs the song from the head's start < 25 ms", abs(lag) < 25, lag)
        check("...and it IS the song (normalised xcorr peak > 0.8)", peak > 0.8, peak)
        n1 = int(124 / 24.0 * SR)
        check("no doubled audio: no clip audio (3 kHz) under the singing spans -- the head's span",
              band_db(got[:n1], 2800, 3200) < band_db(got[:n1], 150, 700) - 40,
              (band_db(got[:n1], 2800, 3200), band_db(got[:n1], 150, 700)))
        check("...nor the cabled shot's span",
              band_db(got[n1:], 2800, 3200) < band_db(got[n1:], 150, 700) - 40,
              (band_db(got[n1:], 2800, 3200), band_db(got[n1:], 150, 700)))
        lag2, _ = lag_ms(got[n1:], song_full[int((23.0 + 124 / 24.0) * SR):])
        check("the second shot's own span lines up too (its first new frame = 27.25 + 22/24 s): lag < 25 ms",
              abs(lag2) < 25, lag2)

    print("\na singing head, then a shot that does not sing: clip audio + the song at the bed's -18 dB there")
    sid = new_seq("sing then not")
    v1 = shot(sid, 124, sing=23.0)
    v3 = shot(sid, 124)
    make(sid, v1, A124)
    make(sid, v3, C72)
    entry, out = cut(sid)
    check("the cut finishes with the song as its soundtrack", entry["status"] == "done" and entry.get("soundtrack") == "song",
          entry)
    if entry["status"] == "done":
        got = pcm(out)
        n1 = int(124 / 24.0 * SR)
        rest = got[n1 + SR // 20:]
        check("the non-singing span keeps its own clip audio (3 kHz present)",
              band_db(rest, 2800, 3200) > band_db(got[:n1], 2800, 3200) + 30)
        drop = band_db(got[SR // 20:n1 - SR // 20], 150, 700) - band_db(rest, 150, 700)
        print("     song level drop into the non-singing span: %.1f dB" % drop)
        check("...and the song carries on under it about 18 dB down (15-21 dB)", 15 <= drop <= 21, drop)
        lag3, _ = lag_ms(rest, song_full[int((23.0 + 124 / 24.0 + 0.05) * SR):])
        check("...continuing the same song timeline (lag < 25 ms)", abs(lag3) < 25, lag3)

    print("\ntwo chain heads the timeline cannot line up on one track: each shot keeps its own sound")
    sid = new_seq("broken timeline")
    v1 = shot(sid, 124, sing=23.0)
    v2 = shot(sid, 124, sing=50.0)
    make(sid, v1, A124)
    make(sid, v2, A124)
    entry, out = cut(sid)
    check("the cut still finishes", entry["status"] == "done", entry.get("log"))
    check("its record says so: soundtrack 'clips' with a sentence",
          entry.get("soundtrack") == "clips" and "own sound" in (entry.get("soundtrack_note") or ""), entry)
    check("the song is not in the cut's shots (no bed under per-clip audio)",
          not any(s.get("role") == "bed" for s in entry.get("shots") or []), entry.get("shots"))
    if entry["status"] == "done":
        got = pcm(out)
        check("the clips' own audio plays (3 kHz present)", band_db(got, 2800, 3200) > band_db(got, 150, 700) + 20)

    print("\na song but no Sing-along shot: today's bed, untouched")
    sid = new_seq("bed only")
    v1 = shot(sid, 124)
    make(sid, v1, A124)
    entry, out = cut(sid)
    check("the cut finishes with no soundtrack field (today's record)",
          entry["status"] == "done" and "soundtrack" not in entry and "soundtrack_note" not in entry, entry)

FAKE_T.terminate()
shutil.rmtree(SCRATCH, ignore_errors=True)
print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
