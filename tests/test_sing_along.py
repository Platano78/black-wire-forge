"""Acceptance gate for E1 "Sing along" (lip-sync chains in the Cutting Room),
gates 1-3 of the build spec:

  1. the fl2va/continue graphs WITH sing_audio/sing_start carry the probed
     chain -- LoadAudio -> TrimAudioDuration(start, length/24) ->
     VAEEncodeAudio(audio VAE) -> SetLatentNoiseMask(SolidMask 0, 32x32) ->
     LTXVConcatAVLatent(video = LTXVSeparateAVLatent(the empty AV latent)[0],
     audio = the masked song) -> the sampler's latent_image -- and WITHOUT them
     the graph equals the pre-E1 builders' own output
     (tests/golden/h3_sing_base.json, captured from the 5a25de6 builders);
  2. offsets are the server's: a 3-shot chain (head at 23.0 s, lengths
     124/124/243) sings from 23.0, 27.25 and 31.5; a reorder that drops a
     continue cable, an unpatch, a length change and a toggle all recompute;
  3. the song reaches the lane (one upload per lane per song take) and the
     prompt the fake ComfyUI captures carries it and the start; the take
     records what it was made against, so a later change shows it stale.

Wired by chain shape, never by node id. server.py in-process against a fake
ComfyUI lane and a fresh scratch data dir (same model as test_continue.py).

Run: python3 tests/test_sing_along.py
"""
import copy, importlib.util, json, os, socket, subprocess, sys, tempfile, time, urllib.request
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import engines

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


# ---------------------------------------------------------------------------
# 1. the graphs
# ---------------------------------------------------------------------------
print("gate 1: the sing chain in the fl2va/continue graphs, and nothing changes without it")

GOLDEN = json.load(open(os.path.join(HERE, "golden", "h3_sing_base.json")))


def ref_of(g, ref):
    return g.get(ref[0]) if isinstance(ref, list) and len(ref) == 2 else None


def chain_ok(g, args, audio, start):
    """The probed chain, followed by links from the sampler back. None = ok."""
    sampler = next((n for n in g.values() if n["class_type"] == "SamplerCustomAdvanced"), None)
    concat = ref_of(g, sampler["inputs"].get("latent_image")) if sampler else None
    if not concat or concat["class_type"] != "LTXVConcatAVLatent":
        return "sampler latent_image is not an LTXVConcatAVLatent"
    sep_ref = concat["inputs"].get("video_latent")
    sep = ref_of(g, sep_ref)
    if not sep or sep["class_type"] != "LTXVSeparateAVLatent" or sep_ref[1] != 0:
        return "video_latent is not LTXVSeparateAVLatent[0]"
    i2v_ref = sep["inputs"].get("av_latent")
    i2v = ref_of(g, i2v_ref)
    if not i2v or i2v["class_type"] != "MiniMaxH3ImageToVideo" or i2v_ref[1] != 1:
        return "the separated latent is not the empty AV latent (MiniMaxH3ImageToVideo[1])"
    mask_node = ref_of(g, concat["inputs"].get("audio_latent"))
    if not mask_node or mask_node["class_type"] != "SetLatentNoiseMask":
        return "audio_latent is not a SetLatentNoiseMask"
    solid = ref_of(g, mask_node["inputs"].get("mask"))
    if not solid or solid["class_type"] != "SolidMask" or solid["inputs"] != {"value": 0.0, "width": 32, "height": 32}:
        return "mask is not SolidMask(0, 32x32): %r" % (solid,)
    enc = ref_of(g, mask_node["inputs"].get("samples"))
    if not enc or enc["class_type"] != "VAEEncodeAudio":
        return "masked samples are not VAEEncodeAudio"
    vae = ref_of(g, enc["inputs"].get("vae"))
    if not vae or vae["class_type"] != "VAELoader" or "audio" not in vae["inputs"]["vae_name"]:
        return "VAEEncodeAudio's vae is not the audio VAE"
    trim = ref_of(g, enc["inputs"].get("audio"))
    if not trim or trim["class_type"] != "TrimAudioDuration":
        return "encoded audio is not TrimAudioDuration"
    if trim["inputs"].get("start_index") != start or trim["inputs"].get("duration") != args["length"] / 24.0:
        return "trim start/duration %r, want %r/%r" % (trim["inputs"], start, args["length"] / 24.0)
    load = ref_of(g, trim["inputs"].get("audio"))
    if not load or load["class_type"] != "LoadAudio" or load["inputs"] != {"audio": audio}:
        return "trim's audio is not LoadAudio(%r): %r" % (audio, load)
    return None


CHAIN = {"LoadAudio", "TrimAudioDuration", "VAEEncodeAudio", "SolidMask", "SetLatentNoiseMask",
         "LTXVSeparateAVLatent", "LTXVConcatAVLatent"}


def strip_sing(g):
    """The sing graph minus exactly the chain's seven nodes, sampler restored."""
    g = copy.deepcopy(g)
    sampler = next(n for n in g.values() if n["class_type"] == "SamplerCustomAdvanced")
    sep = next(n for n in g.values() if n["class_type"] == "LTXVSeparateAVLatent")
    sampler["inputs"]["latent_image"] = sep["inputs"]["av_latent"]
    return {k: v for k, v in g.items() if v["class_type"] not in CHAIN}


for case, rec in sorted(GOLDEN["cases"].items()):
    mode, args = rec["mode"], rec["args"]
    plain = engines.graph_for("video", mode, dict(args), GOLDEN["models"])
    check("%s: WITHOUT sing_* the graph equals the base builder's output" % case, plain == rec["graph"])
    for empty in ({"sing_audio": None, "sing_start": None}, {"sing_audio": ""}):
        check("%s: an empty sing_audio (%r) also leaves the graph as it was" % (case, empty),
              engines.graph_for("video", mode, dict(args, **empty), GOLDEN["models"]) == rec["graph"])
    for start in (23.0, 27.25, 0.0):
        g = engines.graph_for("video", mode, dict(args, sing_audio="song_take.flac", sing_start=start),
                              GOLDEN["models"])
        why = chain_ok(g, args, "song_take.flac", start)
        check("%s: WITH sing_audio/sing_start=%r the chain is wired exactly (start, duration, rewired sampler)"
              % (case, start), why is None, why)
        check("%s (start %r): it adds exactly seven nodes and changes nothing else" % (case, start),
              len(g) == len(rec["graph"]) + 7 and strip_sing(g) == rec["graph"])
_ref2v_args = dict(GOLDEN["cases"]["fl2va_t2v"]["args"], ref_images=["a.png"], ref_videos=[], keep_audio=True,
                   ref_image_size="match", sing_audio="s.flac", sing_start=1.0)
check("ref2v is out of scope: a sing_audio in its args adds nothing",
      not any(n["class_type"] in CHAIN for n in engines.graph_for(
          "video", "ref2v", _ref2v_args, dict(GOLDEN["models"], h3_unet_ref2va="minimax_h3_ref2v.safetensors")).values()))


# ---------------------------------------------------------------------------
# server in-process against a fake lane
# ---------------------------------------------------------------------------
SCRATCH = tempfile.mkdtemp(prefix="bwf_sing_")
print("\nscratch dir: %s (real data/ is never written)" % SCRATCH)
STORE = os.path.join(SCRATCH, "store_t")
os.makedirs(os.path.join(STORE, "outputs"))
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
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "e1 test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": PORT_T,
                      "caps": ["image", "video", "audio"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
DATA = os.path.join(SCRATCH, "data")
os.environ["GENCENTER_DATA"] = DATA
MODELS = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
for _role in engines.model_keys():
    if not MODELS.get(_role):
        MODELS[_role] = "stub_%s.safetensors" % _role

spec = importlib.util.spec_from_file_location("srv_sing", os.path.join(ROOT, "server.py"))
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

# F4: generate now probes the song's length. The song fakes here are random
# bytes, so _probe_duration is faked for THEM only (by song job id; anything else
# still goes to ffprobe). DUR maps a song job id to its length (None = unreadable).
DUR, PROBES = {}, []
_real_probe = mod._probe_duration
def _fake_probe(path):
    name = os.path.basename(path)
    for job_id, dur in DUR.items():   # a copied take is named <job id>.<ext>
        if os.path.splitext(name)[0] == job_id:
            if sys._getframe(1).f_code.co_name == "resolve_slot_sing":   # not the trim/derive probes
                PROBES.append(job_id)
            return dur
    return _real_probe(path)
mod._probe_duration = _fake_probe
DUR_LONG = 600.0

# The pack's own declarations, never a hardcoded mode name.
SING_MODES = {}
for pack in engines.packs():
    if pack["cap"] == "video":
        SING_MODES.update(pack.get("sing_along") or {})
HEAD_MODE = next((m for m, s in SING_MODES.items() if not s.get("carried_frames")), None)
CONT_MODE = next((m for m, s in SING_MODES.items() if s.get("carried_frames")
                  and any(f.get("type") == "video" for f in engines.fields("video", m))), None)
JACK = next((f["id"] for f in engines.fields("video", CONT_MODE) if f.get("type") == "video"), None) if CONT_MODE else None
OTHER_MODE = next((m for m in engines.modes_for("video") if m not in SING_MODES), None)
check("sanity: a pack declares sing_along for a plain mode and a continue mode (with a video jack)",
      bool(HEAD_MODE and CONT_MODE and JACK), (HEAD_MODE, CONT_MODE, JACK))


def op(sid, name, **kw):
    rev = mod.seq_get(sid)[0]["rev"]
    return mod.seq_op(dict(kw, id=sid, rev=rev, op=name))


def get(sid):
    return mod.seq_get(sid)[0]


def slot_of(sid, slot_id):
    return next(s for s in get(sid)["slots"] if s["id"] == slot_id)


def starts(sid, ids):
    return [(slot_of(sid, i).get("sing_span") or {}).get("start") for i in ids]


def control(outputs):
    req = urllib.request.Request("http://127.0.0.1:%d/_control/accept" % PORT_T,
                                 data=json.dumps({"outputs": outputs}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def last_prompt():
    with open(os.path.join(STORE, "last_prompt.json")) as f:
        return json.load(f)


def uploads():
    path = os.path.join(STORE, "requests.log")
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in open(path) if '"/upload/image"' in line]


def make_job(job_id, fname, data, kind, mode, media):
    with open(os.path.join(STORE, "outputs", fname), "wb") as f:
        f.write(data)
    with mod.JOBS_LOCK:
        mod.JOBS[job_id] = {"id": job_id, "lane": "t", "kind": kind, "mode": mode, "status": "done",
                            "prompt": "a test song",
                            "outputs": [{"filename": fname, "subfolder": "", "type": "output", "media": media}]}


def add_video(sid, mode, length):
    values = {f["id"]: f["default"] for f in engines.fields("video", mode) if f.get("default") is not None}
    values.update(prompt="he sings to camera", length=length)
    if any(f["id"] == "first_frame" for f in engines.fields("video", mode)):
        values["first_frame"] = "start.png"
    b, c = op(sid, "add_slot", lane="video", cap="video", mode=mode, values=values)
    assert c == 200, b
    return b["added_slot_id"]


def add_song(sid, job_id, fname):
    b, c = op(sid, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "a test song"})
    assert c == 200, b
    s_id = b["added_slot_id"]
    data = b"fLaC" + os.urandom(2048)
    DUR.setdefault(job_id, DUR_LONG)
    make_job(job_id, fname, data, "audio", "sfx", "audio")
    mod.seq_add_take(sid, s_id, job_id)
    b, c = op(sid, "pick_take", slot_id=s_id, job_id=job_id)
    assert c == 200, b
    return s_id, data


if HEAD_MODE and CONT_MODE and JACK:
    # -----------------------------------------------------------------------
    print("\ngate 2: offsets are computed by the server")
    seq, _ = mod.seq_create({"title": "chain", "mode": "sequence"})
    sid = seq["id"]
    v1 = add_video(sid, HEAD_MODE, 124)
    v2 = add_video(sid, CONT_MODE, 124)
    v3 = add_video(sid, CONT_MODE, 243)
    op(sid, "patch", **{"from": v1, "to": v2, "field": JACK})
    op(sid, "patch", **{"from": v2, "to": v3, "field": JACK})

    b, c = op(sid, "set_sing", slot_id=v1, on=True)
    check("without a song, Sing along is refused with a sentence", c == 400 and "song" in (b or {}).get("error", ""), b)
    check("without a song, no shot is offered Sing along",
          not any(s.get("sing_offer") for s in get(sid)["slots"]))
    add_song(sid, "song_job_1", "song_take.flac")
    check("with a song, the fl2va/continue shots are offered Sing along",
          all(slot_of(sid, v).get("sing_offer") for v in (v1, v2, v3)))
    for v in (v1, v2, v3):
        b, c = op(sid, "set_sing", slot_id=v, on=True)
        check("set_sing on %s accepted" % v, c == 200, b)
    b, c = op(sid, "set_sing", slot_id=v1, start=23.0)
    check("the chain head's Starts at is accepted", c == 200, b)
    check("3-shot chain (head 23.0, lengths 124/124/243) -> 23.0, 27.25, 31.5",
          starts(sid, [v1, v2, v3]) == [23.0, 27.25, 31.5], starts(sid, [v1, v2, v3]))
    spans = [slot_of(sid, v).get("sing_span") or {} for v in (v1, v2, v3)]
    check("only the chain head is a head; ends are start + length/24",
          [s.get("head") for s in spans] == [True, False, False]
          and abs(spans[0]["end"] - (23.0 + 124 / 24.0)) < 1e-6 and abs(spans[2]["end"] - (31.5 + 243 / 24.0)) < 1e-6,
          spans)
    check("F2: sing_span carries heard_start = start + lead_in: 23.0, 28.166667, 32.416667",
          [s.get("heard_start") for s in spans] == [23.0, 28.166667, 32.416667]
          and [s.get("start") for s in spans] == [23.0, 27.25, 31.5], spans)
    b, c = op(sid, "set_sing", slot_id=v2, start=99.0)
    check("a typed start on a chained shot is kept but never used (still 27.25)",
          c == 200 and starts(sid, [v2]) == [27.25], starts(sid, [v2]))
    for bad in (-1, "5", True, float("inf")):
        b, c = op(sid, "set_sing", slot_id=v1, start=bad)
        check("Starts at %r refused" % (bad,), c == 400, b)

    def set_start(val):
        try:
            return op(sid, "set_sing", slot_id=v1, start=val)
        except Exception as e:   # a typed field must never raise (the server turns it into a 500)
            return {"ok": False, "error": "RAISED %r" % (e,)}, 500
    for bad in (10 ** 19, 10 ** 400, 1e308, -1, 86400.5):
        b, c = set_start(bad)
        check("F1: Starts at %r is refused with a sentence, not a 500" % (bad,),
              c == 400 and isinstance(b.get("error"), str) and (bad < 0 or "24 hours" in b["error"]), (c, b))
    b, c = set_start(86400)
    check("F1: Starts at 86400 (24 h) is accepted", c == 200, (c, b))
    set_start(23.0)

    op(sid, "update_slot", slot_id=v1, values={"length": 141})
    check("a head length change recomputes: 23 + (141-22)/24, then + 102/24",
          starts(sid, [v1, v2, v3]) == [23.0, round(23 + 119 / 24.0, 6), round(round(23 + 119 / 24.0, 6) + 102 / 24.0, 6)],
          starts(sid, [v1, v2, v3]))
    op(sid, "update_slot", slot_id=v1, values={"length": 124})

    op(sid, "set_sing", slot_id=v2, on=False)
    check("toggling the middle shot off makes the next one a chain head at its own start (0)",
          starts(sid, [v1, v2, v3]) == [23.0, None, 0.0] and (slot_of(sid, v3).get("sing_span") or {}).get("head"),
          starts(sid, [v1, v2, v3]))
    op(sid, "set_sing", slot_id=v2, on=True)
    check("toggling it back on restores 27.25 / 31.5", starts(sid, [v1, v2, v3]) == [23.0, 27.25, 31.5])

    cable12 = next(c for c in get(sid)["cables"] if c["from"] == v1 and c["to"] == v2)
    op(sid, "unpatch", cable_id=cable12["id"])
    check("unpatching v1->v2 makes v2 a head at its own start (99.0 typed earlier), v3 follows it",
          starts(sid, [v1, v2, v3]) == [23.0, 99.0, 103.25], starts(sid, [v1, v2, v3]))
    op(sid, "patch", **{"from": v1, "to": v2, "field": JACK})
    check("re-patching restores the chain", starts(sid, [v1, v2, v3]) == [23.0, 27.25, 31.5])

    order = [s["id"] for s in get(sid)["slots"]]
    b, c = op(sid, "move_slot", slot_id=v3, to=order.index(v2))
    check("(setup) moving v3 before v2 drops the v2->v3 cable (existing behaviour)",
          c == 200 and {"from": v2, "to": v3} in (b.get("removed_cables") or []), b)
    check("the reorder recomputes: v3 is now a head at its own start (0), v2 still 27.25",
          starts(sid, [v1, v2, v3]) == [23.0, 27.25, 0.0], starts(sid, [v1, v2, v3]))
    check("a shot whose mode cannot sing refuses set_sing",
          OTHER_MODE is None or op(sid, "set_sing", slot_id=add_video(sid, OTHER_MODE, 124), on=True)[1] == 400)

    # -----------------------------------------------------------------------
    print("\ngate 3: the song reaches the lane and the prompt carries it")
    seq, _ = mod.seq_create({"title": "made", "mode": "sequence"})
    sid = seq["id"]
    w1 = add_video(sid, HEAD_MODE, 124)
    w2 = add_video(sid, CONT_MODE, 124)
    w3 = add_video(sid, CONT_MODE, 124)   # sings nothing: its graph must stay plain
    op(sid, "patch", **{"from": w1, "to": w2, "field": JACK})
    _song, SONG_BYTES = add_song(sid, "song_job_2", "song_take_2.flac")
    for v in (w1, w2):
        op(sid, "set_sing", slot_id=v, on=True)
    op(sid, "set_sing", slot_id=w1, start=23.0)

    before = len(uploads())
    control([{"filename": "w1_out.mp4", "subfolder": "", "type": "output"}])
    body, code = mod.seq_generate({"id": sid, "slot_id": w1})
    check("generate the chain head", code == 200 and body.get("ok"), body)
    g1 = last_prompt()
    song_up = [u for u in uploads()[before:] if u.get("length") == len(SONG_BYTES)]
    song_name = song_up[0]["filename"] if len(song_up) == 1 else None
    check("the song was uploaded to the lane once, byte-for-byte the song take",
          song_name is not None and open(os.path.join(STORE, "inputs", song_name), "rb").read() == SONG_BYTES,
          uploads()[before:])
    head_args = dict(GOLDEN["cases"]["fl2va_t2v"]["args"], length=124)
    why = chain_ok(g1, head_args, song_name, 23.0)
    check("the head's captured prompt carries sing_audio (the uploaded song) and sing_start 23.0", why is None, why)
    t1 = slot_of(sid, w1)["takes"][-1] if code == 200 else {"inputs": {}}
    check("the head's take records {song, start 23.0, lead_in 0}",
          t1["inputs"].get("sing") == {"song": "song_job_2", "start": 23.0, "lead_in": 0.0}, t1["inputs"])
    if code == 200:
        job1 = body["job"]["id"]
        make_job(job1, "w1_out.mp4", os.urandom(512), "video", HEAD_MODE, "video")
        op(sid, "pick_take", slot_id=w1, job_id=job1)

    before = len(uploads())
    control([{"filename": "w2_out.mp4", "subfolder": "", "type": "output"}])
    body, code = mod.seq_generate({"id": sid, "slot_id": w2})
    check("generate the continued shot", code == 200 and body.get("ok"), body)
    g2 = last_prompt()
    new = uploads()[before:]
    check("the song is NOT uploaded again for the next shot on the same lane (only the cabled clip is)",
          not [u for u in new if u.get("length") == len(SONG_BYTES)] and len(new) == 1, new)
    why = chain_ok(g2, head_args, song_name, 27.25)
    check("the continued shot's captured prompt carries the same song and sing_start 27.25", why is None, why)
    check("...and it still goes through the Motion-Context continue wiring",
          any(n["class_type"] == "MiniMaxH3MotionContext" for n in g2.values()))
    t2 = slot_of(sid, w2)["takes"][-1] if code == 200 else {"inputs": {}}
    check("the continued take records {song, start 27.25, lead_in 22/24}",
          t2["inputs"].get("sing") == {"song": "song_job_2", "start": 27.25, "lead_in": round(22 / 24.0, 6)},
          t2["inputs"])
    if code == 200:
        job2 = body["job"]["id"]
        make_job(job2, "w2_out.mp4", os.urandom(512), "video", CONT_MODE, "video")
        op(sid, "pick_take", slot_id=w2, job_id=job2)
    check("fresh takes are not stale", not slot_of(sid, w1)["stale"] and not slot_of(sid, w2)["stale"],
          (slot_of(sid, w1)["stale"], slot_of(sid, w2)["stale"]))

    control([{"filename": "w3_out.mp4", "subfolder": "", "type": "output"}])
    body, code = mod.seq_generate({"id": sid, "slot_id": w3})
    g3 = last_prompt()
    check("a shot with Sing along off in the same sequence renders with no song nodes",
          code == 200 and not any(n["class_type"] in CHAIN for n in g3.values()), body)
    check("...and its take records no sing", "sing" not in slot_of(sid, w3)["takes"][-1]["inputs"])

    op(sid, "update_slot", slot_id=w1, values={"length": 141})
    check("changing the head's length marks the continued shot stale: 'song timing changed'",
          "song timing changed" in slot_of(sid, w2)["stale"], slot_of(sid, w2)["stale"])
    op(sid, "update_slot", slot_id=w1, values={"length": 124})
    check("changing it back clears it", not slot_of(sid, w2)["stale"], slot_of(sid, w2)["stale"])
    op(sid, "set_sing", slot_id=w1, start=10.0)
    check("moving the head's start marks the head AND the chained shot stale",
          "song timing changed" in slot_of(sid, w1)["stale"] and "song timing changed" in slot_of(sid, w2)["stale"])
    op(sid, "set_sing", slot_id=w1, start=23.0)
    make_job("rerolled_head", "w1_reroll.mp4", os.urandom(512), "video", HEAD_MODE, "video")
    mod.seq_add_take(sid, w1, "rerolled_head", inputs={"refs": [], "cables": {}, "sing": t1["inputs"].get("sing")})
    op(sid, "pick_take", slot_id=w1, job_id="rerolled_head")
    check("re-rolling shot 1 shows shot 2 stale (it was made against the old take)",
          slot_of(sid, w2)["stale"] != [] and not slot_of(sid, w1)["stale"], slot_of(sid, w2)["stale"])

    before = len(uploads())
    mod.seq_generate({"id": sid, "slot_id": w2})
    check("a second generate on the same lane still does not re-upload the song",
          not [u for u in uploads()[before:] if u.get("length") == len(SONG_BYTES)])

    body, code = mod.generate(dict(prompt="x", lane="t", kind="video", mode=CONT_MODE, length=124,
                                   sing_audio="s.flac", sing_start=-2))
    check("a negative sing_start is refused with a sentence", code == 400 and "start" in body.get("error", ""), body)

    # -----------------------------------------------------------------------
    print("\nF3: staleness along a SINGING chain is transitive; nothing else changes")

    def made_chain(title, song, sing):
        seq, _ = mod.seq_create({"title": title, "mode": "sequence"})
        sid = seq["id"]
        ids = [add_video(sid, HEAD_MODE, 124), add_video(sid, CONT_MODE, 124), add_video(sid, CONT_MODE, 124)]
        op(sid, "patch", **{"from": ids[0], "to": ids[1], "field": JACK})
        op(sid, "patch", **{"from": ids[1], "to": ids[2], "field": JACK})
        if song:
            add_song(sid, "song_" + title, title + ".flac")
        for v, on in zip(ids, sing):
            if on:
                op(sid, "set_sing", slot_id=v, on=True)
        for n, v in enumerate(ids):
            control([{"filename": "%s_%d.mp4" % (title, n), "subfolder": "", "type": "output"}])
            body, code = mod.seq_generate({"id": sid, "slot_id": v})
            assert code == 200, body
            job = body["job"]["id"]
            make_job(job, "%s_%d.mp4" % (title, n), os.urandom(512), "video", HEAD_MODE if n == 0 else CONT_MODE, "video")
            op(sid, "pick_take", slot_id=v, job_id=job)
        return sid, ids

    def reroll_head(sid, head, tag):
        inputs = slot_of(sid, head)["takes"][-1]["inputs"]
        make_job(tag, tag + ".mp4", os.urandom(512), "video", HEAD_MODE, "video")
        mod.seq_add_take(sid, head, tag, inputs=copy.deepcopy(inputs))
        op(sid, "pick_take", slot_id=head, job_id=tag)

    sid, (c1, c2, c3) = made_chain("chainA", True, (True, True, True))
    check("(setup) a fresh singing chain is not stale", not any(slot_of(sid, x)["stale"] for x in (c1, c2, c3)),
          [slot_of(sid, x)["stale"] for x in (c1, c2, c3)])
    reroll_head(sid, c1, "chainA_re")
    st = [slot_of(sid, x)["stale"] for x in (c1, c2, c3)]
    check("F3: re-rolling shot 1 flags shot 2 (existing) AND shot 3 of a singing chain", st[0] == [] and st[1] and st[2], st)
    check("F3: shot 3's reasons include 'an earlier shot changed', exactly once",
          st[2].count("an earlier shot changed") == 1, st)
    check("F3: shot 2 is flagged by its own cable, not the new reason", "an earlier shot changed" not in st[1], st)

    sid, (d1, d2, d3) = made_chain("chainB", False, (False, False, False))
    reroll_head(sid, d1, "chainB_re")
    st = [slot_of(sid, x)["stale"] for x in (d1, d2, d3)]
    check("F3: the same chain with NO song: shot 2 stale (one hop), shot 3 NOT", st[1] and st[2] == [], st)

    sid, (e1, e2, e3) = made_chain("chainC", True, (True, True, False))
    reroll_head(sid, e1, "chainC_re")
    st = [slot_of(sid, x)["stale"] for x in (e1, e2, e3)]
    check("F3: song present but shot 3 not singing: shot 2 stale, shot 3 NOT", st[1] and st[2] == [], st)

    # -----------------------------------------------------------------------
    print("\nF4: a shot that would sing past the end of the song is refused at generate")
    seq, _ = mod.seq_create({"title": "short", "mode": "sequence"})
    sid = seq["id"]
    DUR["song_short"] = 10.0
    add_song(sid, "song_short", "short_song.flac")
    late = add_video(sid, HEAD_MODE, 243)
    ok1 = add_video(sid, HEAD_MODE, 124)
    ok2 = add_video(sid, HEAD_MODE, 124)
    for v in (late, ok1, ok2):
        op(sid, "set_sing", slot_id=v, on=True)
    op(sid, "set_sing", slot_id=late, start=5.0)
    del PROBES[:]
    body, code = mod.seq_generate({"id": sid, "slot_id": late})
    check("F4: start 5 + 243 frames on a 10 s song -> 400 naming both numbers",
          code == 400 and "0:15.1" in body.get("error", "") and "0:10.0" in body.get("error", ""), (code, body))
    check("F4: the refusal says what to do", "Start it earlier or make it shorter." in body.get("error", ""), body)
    control([{"filename": "ok1.mp4", "subfolder": "", "type": "output"}])
    body, code = mod.seq_generate({"id": sid, "slot_id": ok1})
    check("F4: start 0, 124 frames on the same song proceeds", code == 200 and body.get("ok"), (code, body))
    control([{"filename": "ok2.mp4", "subfolder": "", "type": "output"}])
    body, code = mod.seq_generate({"id": sid, "slot_id": ok2})
    check("F4: ...and so does the next shot", code == 200 and body.get("ok"), (code, body))
    check("F4: the song was probed once across all three shots", PROBES.count("song_short") == 1, PROBES)

    seq, _ = mod.seq_create({"title": "unreadable", "mode": "sequence"})
    sid = seq["id"]
    DUR["song_odd"] = None
    add_song(sid, "song_odd", "odd_song.flac")
    odd = add_video(sid, HEAD_MODE, 124)
    op(sid, "set_sing", slot_id=odd, on=True)
    body, code = mod.seq_generate({"id": sid, "slot_id": odd})
    check("F4: a song whose length cannot be read is refused with a sentence",
          code == 400 and body.get("error") == "Couldn't read the song's length.", (code, body))

    # -----------------------------------------------------------------------
    print("\nF5: adopt_take -- a finished job the person already has becomes a shot's take")
    seq, _ = mod.seq_create({"title": "adopt", "mode": "sequence"})
    sid = seq["id"]
    b, c = op(sid, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "the song"})
    a_slot = b["added_slot_id"]
    A_BYTES = b"fLaC" + os.urandom(2048)
    DUR["made_earlier"] = DUR_LONG
    make_job("made_earlier", "made_earlier.flac", A_BYTES, "audio", "sfx", "audio")
    make_job("a_video_job", "a_video_job.mp4", os.urandom(256), "video", HEAD_MODE, "video")
    with mod.JOBS_LOCK:
        mod.JOBS["still_running"] = {"id": "still_running", "lane": "t", "kind": "audio", "mode": "sfx",
                                     "status": "running", "outputs": []}
    for what, jid in (("a video job into a sound slot", "a_video_job"), ("an unfinished job", "still_running"),
                      ("an unknown id", "no_such_job"), ("no id", None)):
        b, c = op(sid, "adopt_take", slot_id=a_slot, job_id=jid)
        check("F5: adopting %s -> 400 with a sentence" % what, c == 400 and isinstance(b.get("error"), str) and b["error"], (c, b))
    check("F5: none of the refusals touched the slot", slot_of(sid, a_slot)["takes"] == [] and slot_of(sid, a_slot)["pick"] is None)
    b, c = op(sid, "adopt_take", slot_id=a_slot, job_id="made_earlier")
    check("F5: adopting a finished sound job into a sound slot -> 200", c == 200, b)
    sl = slot_of(sid, a_slot)
    check("F5: the pick is the job and seq.song.job_id is it",
          sl["pick"] == "made_earlier" and get(sid)["song"] == {"slot_id": a_slot, "job_id": "made_earlier"}, (sl["pick"], get(sid).get("song")))
    check("F5: the take is shaped like a generated one (beat_rev None, adopted inputs)",
          sl["takes"][-1]["beat_rev"] is None and sl["takes"][-1]["inputs"] == {"refs": [], "cables": {}, "adopted": True}, sl["takes"])
    check("F5: adopting it again is refused", op(sid, "adopt_take", slot_id=a_slot, job_id="made_earlier")[1] == 400)
    check("F5: the adopted take is not stale", sl["stale"] == [], sl["stale"])
    with mod.JOBS_LOCK:
        mod.JOBS["made_earlier"]["prompt"] = "the song"
    check("F5: adopt_take pins the job like pick_take", "adopt_take" in mod.SEQ_OPS_PIN_JOBS)
    other_audio = next((m for m in engines.modes_for("audio") if m != "sfx"), None)
    if other_audio:
        make_job("other_mode_job", "other_mode_job.flac", A_BYTES, "audio", other_audio, "audio")
        b, c = op(sid, "add_slot", lane="sound", cap="audio", mode="sfx", values={"prompt": "x", "seconds": 5})
        sw = b["added_slot_id"]
        b, c = op(sid, "adopt_take", slot_id=sw, job_id="other_mode_job")
        check("F5: a take of another mode of the same kind turns the slot into that mode, values re-fitted",
              c == 200 and slot_of(sid, sw)["mode"] == other_audio, (c, b if c != 200 else slot_of(sid, sw)["mode"]))

    h1 = add_video(sid, HEAD_MODE, 124)
    op(sid, "set_sing", slot_id=h1, on=True)
    check("F5: a Sing-along shot now plans against the adopted song",
          (slot_of(sid, h1).get("sing_span") or {}).get("start") == 0.0 and get(sid)["song"]["job_id"] == "made_earlier")
    before = len(uploads())
    control([{"filename": "adopt_v.mp4", "subfolder": "", "type": "output"}])
    body, code = mod.seq_generate({"id": sid, "slot_id": h1})
    check("F5: it generates", code == 200 and body.get("ok"), body)
    up = [u for u in uploads()[before:] if u.get("length") == len(A_BYTES)]
    gp = last_prompt()
    why = chain_ok(gp, dict(GOLDEN["cases"]["fl2va_t2v"]["args"], length=124),
                   up[0]["filename"] if len(up) == 1 else None, 0.0)
    check("F5: the fake lane's captured prompt carries sing_audio = the adopted song", len(up) == 1 and why is None, (why, up))
    check("F5: the shot's take records the adopted song",
          slot_of(sid, h1)["takes"][-1]["inputs"].get("sing", {}).get("song") == "made_earlier", slot_of(sid, h1)["takes"][-1]["inputs"])

FAKE_T.terminate()
print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
