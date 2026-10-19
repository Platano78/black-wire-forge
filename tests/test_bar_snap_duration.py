"""Beat-snap: the master's length is its DECODED length, never the container header (spec S3 finding).

ffmpeg 4.4 reports an encoder-padded mp3's format.duration ~33 ms long (177.633 for a file that decodes to 177.600).
Analyze stored that as song_map.source.seconds, so the to_end last shot got a frame too many (258, not 257) and the
snapped cut refused. Here the header probe is made to lie by 32.7 ms and the snapped last window must not move;
_decoded_seconds is also checked on a real mp3 and wav. Run: python3 tests/test_bar_snap_duration.py
"""
import atexit, importlib.util, json, os, shutil, socket, struct, subprocess, sys, tempfile, time
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    print("SKIP: ffmpeg/ffprobe not on PATH"); sys.exit(0)
import engines

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

SCRATCH = tempfile.mkdtemp(prefix="bwf_bar_snap_dur_")
atexit.register(lambda: shutil.rmtree(SCRATCH, ignore_errors=True))
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
cfg = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "dur",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 5.0},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": free_port(), "caps": ["video"]}]},
          open(cfg, "w"))
data = os.path.join(SCRATCH, "data")
os.environ["GENCENTER_CONFIG"], os.environ["GENCENTER_DATA"] = cfg, data
spec = importlib.util.spec_from_file_location("srv_dur", os.path.join(ROOT, "server.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.SEQ_DIR, m.SEQ_MEDIA_DIR = os.path.join(data, "sequences"), os.path.join(data, "seq")
m.LOCAL_OUTPUTS_DIR, m.UPLOADS_DIR = os.path.join(data, "outputs"), os.path.join(data, "uploads")
for d in (m.SEQ_DIR, m.SEQ_MEDIA_DIR, m.LOCAL_OUTPUTS_DIR, m.UPLOADS_DIR):
    os.makedirs(d, exist_ok=True)
MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
for role in engines.model_keys():
    MODELS.setdefault(role, "stub_%s.safetensors" % role)
    MODELS[role] = MODELS[role] or "stub_%s.safetensors" % role
m.LANE_BY_ID["t"]["models"] = dict(MODELS)

SECONDS = 40.0
def wav(seconds, sr=8000):
    pcm = b"\0\0" * int(seconds * sr)
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)
MASTER = wav(SECONDS)

print("_decoded_seconds measures samples, not the header")
p_wav = os.path.join(SCRATCH, "a.wav"); open(p_wav, "wb").write(MASTER)
check("a 40.000 s wav decodes to 40.000 s", abs((m._decoded_seconds(p_wav) or 0) - SECONDS) < 1e-6, m._decoded_seconds(p_wav))
p_mp3 = os.path.join(SCRATCH, "a.mp3")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=12.6",
                "-c:a", "libmp3lame", p_mp3], check=True)
dec = m._decoded_seconds(p_mp3)
check("an encoder-padded mp3 (header says %.4f) decodes to 12.6 s within one mp3 frame" % (m._probe_duration(p_mp3) or 0),
      dec is not None and abs(dec - 12.6) < 0.03, dec)
check("a missing file gives None, not a crash", m._decoded_seconds(os.path.join(SCRATCH, "nope.mp3")) is None)

print("analyze keeps the decoded length when the header over-reports")
orig_probe = m._probe_duration
m._probe_duration = lambda path: (orig_probe(path) or 0) + 0.0327
jobs = []
def stub_generate(p):
    jid = "gridstub%02d" % (len(jobs) + 1); jobs.append(jid)
    with m.JOBS_LOCK:
        m.JOBS[jid] = {"id": jid, "lane": "cpu", "kind": "producer", "mode": "grid", "status": "running", "outputs": []}
    return {"ok": True, "job": {"id": jid}}, 200
m.generate = stub_generate
m._grid_lane = lambda lane_id=None: {"id": "cpu", "name": "CPU lane", "kind": "process"}
m.ANALYZE_POLL_S = 0.05

seq, _ = m.seq_create({"title": "dur", "mode": "sequence"})
sid = seq["id"]
def op(name, **kw):
    return m.seq_op(dict(kw, id=sid, rev=m.seq_get(sid)[0]["rev"], op=name))
def ok(name, **kw):
    body, code = op(name, **kw); assert code == 200, (name, kw, body); return body
ok("set_canvas", width=192, height=96)
shots = []
for _ in range(4):
    v = {f["id"]: f["default"] for f in engines.fields("video", "fl2va") if f.get("default") is not None}
    v.update(prompt="a", length=62, width=192, height=96, first_frame="start.png")
    shots.append(ok("add_slot", lane="video", cap="video", mode="fl2va", values=v)["added_slot_id"])
body, code = m.seq_master_import(sid, m.seq_get(sid)[0]["rev"], "master.wav", MASTER); assert code == 200, body
ok("set_audio_led", on=True)
body, code = m.seq_analyze({"id": sid})
check("analyze starts", code == 200 and body.get("ok"), (code, body))
jid = body["job_id"]
downs = [round(0.5 + 2.4 * k, 4) for k in range(16)]
beats = []
for a, b in zip(downs, downs[1:]):
    beats += [a + (b - a) * k / 4 for k in range(4)]
beats.append(downs[-1])
d = os.path.join(m.LOCAL_OUTPUTS_DIR, jid); os.makedirs(d)
json.dump({"beats": beats, "downbeats": downs, "bars": [], "meter": {"beats_per_bar": 4}, "bpm_median": 100.0,
           "source": {"duration_s": SECONDS}}, open(os.path.join(d, "grid.json"), "w"))
with m.JOBS_LOCK:
    m.JOBS[jid]["status"] = "done"
end = time.time() + 15
while time.time() < end and "song_map" not in m.seq_get(sid)[0]:
    time.sleep(0.05)
sm = (m.seq_get(sid)[0].get("song_map") or {})
check("the map attached with source.seconds = the decoded 40.0, not the header's 40.0327",
      abs((sm.get("source") or {}).get("seconds", 0) - SECONDS) < 1e-6, (sm.get("source"), m.seq_get(sid)[0].get("song_map_job")))
ok("set_bar_snap", on=True)
for v in shots[:3]:
    ok("set_shot_bars", slot_id=v, bars=4)
ok("set_shot_bars", slot_id=shots[3], to_end=True)
w = [s["window"] for s in m.seq_get(sid)[0]["slots"]]
check("the to_end shot ends on frame 960 (40.0 s x 24), not 961", w[-1]["f1"] == 960, w[-1])
check("nothing is left unplaced", m.seq_get(sid)[0]["snap_report"]["unplaced_bars"] == 0)
print()
print("ALL PASS" if not FAILED else "%d FAILED: %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
