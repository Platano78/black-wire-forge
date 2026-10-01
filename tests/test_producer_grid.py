"""Producer room P1 "Grid check" gate.

Three layers:
  - pack + pure analysis (stdlib only): the mode's shape, analyse() on exact
    beat times, the missing-tool sentences (no traceback, ever);
  - a constant-period baseline that must FAIL the drifting case (needs numpy);
  - the real helper end to end on synthetic click tracks made here with numpy
    (nothing committed): beat F-measure at +-70 ms, downbeat period, per-bar
    bpm. Needs beat_this + numpy + ffmpeg; without beat_this they SKIP with
    the reason printed.

Run:  python3 tests/test_producer_grid.py
      /path/to/venv-with-beat_this/bin/python tests/test_producer_grid.py
Exit 0 = all good (skips are not failures).
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from _portable_exec import make_program  # noqa: E402

FAILED, SKIPPED = [], []
TOL = 0.070


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


def skip(name, why):
    print("  SKIP  %s (%s)" % (name, why))
    SKIPPED.append(name)


if importlib.util.find_spec("beat_this") is not None:
    # This Python can run the tool, so the pack (which reads this when imported)
    # is pointed at it for the through-the-server check below.
    os.environ.setdefault("BWF_PRODUCER_PYTHON", sys.executable)
import engines  # noqa: E402

HELPER = os.path.join(ROOT, "engines", "producer_tools", "grid_check.py")
_spec = importlib.util.spec_from_file_location("grid_check", HELPER)
gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gc)
try:
    import numpy as np
except ImportError:
    np = None
HAVE_BEAT_THIS = importlib.util.find_spec("beat_this") is not None
FFMPEG = shutil.which("ffmpeg")
WORK = tempfile.mkdtemp(prefix="bwf_p1grid_")


# ── truth and measurement ───────────────────────────────────────────────────
def truth(case):
    """(beats, downbeats, bars [(start, n, true_bpm)]) in seconds."""
    per_bar, kind, dur = case["per_bar"], case["kind"], case["dur"]
    lead = 0.5
    beats = []
    k = 0
    while True:
        if kind == "const":
            t = lead + k * 60.0 / case["bpm"]
        else:  # linear tempo ramp b0 -> b1 over dur seconds: phase(t) = (b0*t + (b1-b0)*t*t/(2*dur))/60
            b0, b1 = case["bpm"], case["bpm1"]
            a, b, c = (b1 - b0) / (2.0 * dur) / 60.0, b0 / 60.0, -k
            t = lead + (-b + (b * b - 4 * a * c) ** 0.5) / (2 * a)
        if t > dur - 0.3:
            break
        beats.append(t)
        k += 1
    downbeats = beats[::per_bar]
    bars = []
    for i in range(0, len(beats) - per_bar, per_bar):
        d = beats[i + per_bar] - beats[i]
        bars.append((beats[i], per_bar, 60.0 * per_bar / d))
    return beats, downbeats, bars


def f_measure(est, ref, tol=TOL):
    """One-to-one greedy match within +-tol (the usual beat-tracking F)."""
    est, ref = sorted(est), sorted(ref)
    used, hit = set(), 0
    for r in ref:
        best, bd = None, tol + 1
        for j, e in enumerate(est):
            if j in used:
                continue
            d = abs(e - r)
            if d <= tol and d < bd:
                best, bd = j, d
        if best is not None:
            used.add(best)
            hit += 1
    if not est or not ref or not hit:
        return 0.0
    p, r = hit / len(est), hit / len(ref)
    return 2 * p * r / (p + r)


def synth(case, path):
    """Click track: a 1 kHz tick on each beat, and on each downbeat a louder,
    lower-pitched thud with a 2 kHz tick on top (an accented downbeat)."""
    sr = 44100
    n = int(case["dur"] * sr)
    y = np.zeros(n, dtype="float64")
    beats, downs, _ = truth(case)
    t = np.arange(int(0.05 * sr)) / sr
    tick = 0.45 * np.sin(2 * np.pi * 1000 * t) * np.exp(-t / 0.008)
    accent = (0.9 * np.sin(2 * np.pi * 200 * t) * np.exp(-t / 0.02)
              + 0.6 * np.sin(2 * np.pi * 2000 * t) * np.exp(-t / 0.01))
    ds = {round(d, 4) for d in downs}
    for b in beats:
        i = int(round(b * sr))
        s = accent if round(b, 4) in ds else tick
        y[i:i + len(s)] += s[:n - i]
    pcm = (np.clip(y, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return y


MSR = 22050


def musical(bpb, bpm, bars=24, seed=1234):
    """A bar-structured toy song, the kind of material the downbeat head was
    trained on: hats on every half beat, kick and snare in a per-meter
    pattern, and a bass/chord root that changes every bar (the bar-start cue
    a bare click track lacks). Fixed RNG seed, so it is deterministic.
    Returns (signal, bar seconds); beat k of bar b sits at (b*bpb + k)*60/bpm."""
    rng = np.random.RandomState(seed)

    def tone(f, d, a):
        t = np.arange(int(d * MSR)) / MSR
        return a * np.sin(2 * np.pi * f * t) * np.exp(-t * 3)

    def kick():
        t = np.arange(int(.15 * MSR)) / MSR
        return .9 * np.sin(2 * np.pi * (60 + 80 * np.exp(-t * 30)) * t) * np.exp(-t * 20)

    def snare():
        n = int(.12 * MSR)
        return .5 * rng.randn(n) * np.exp(-np.arange(n) / MSR * 25)

    def hat():
        n = int(.04 * MSR)
        return .15 * rng.randn(n) * np.exp(-np.arange(n) / MSR * 80)
    p = 60.0 / bpm
    y = np.zeros(int((bars * bpb * p + 2) * MSR))
    roots = [55, 73.4, 65.4, 49]

    def put(sig, t):
        i = int(t * MSR)
        y[i:i + len(sig)] += sig[:len(y) - i]
    for b in range(bars):
        t0 = b * bpb * p
        put(tone(roots[b % 4], bpb * p, .35), t0)
        put(tone(roots[b % 4] * 2.5, bpb * p * .9, .12), t0)
        for k in range(bpb):
            t = t0 + k * p
            put(hat(), t)
            put(hat(), t + p / 2)
            if k == 0 or (bpb == 4 and k == 2) or (bpb == 5 and k == 3):
                put(kick(), t)
            if k in ((1, 3) if bpb == 4 else (2, 4)):
                put(snare(), t)
    return y / np.abs(y).max() * .8, p * bpb


def write_wav(path, y, sr):
    pcm = (np.clip(y, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


MUSICAL = {
    "M4 musical 4/4 @110": {"bpb": 4, "bpm": 110.0},
    "M5 musical 5/4 @137.5": {"bpb": 5, "bpm": 137.5},
}


def musical_truth(c, bars=24):
    p = 60.0 / c["bpm"]
    return [i * p for i in range(bars * c["bpb"])], [b * c["bpb"] * p for b in range(bars)]


CASES = {
    "i  4/4 @110 constant": {"kind": "const", "bpm": 110.0, "per_bar": 4, "dur": 40.0},
    "ii 4/4 drifting 110->112 over 60 s": {"kind": "ramp", "bpm": 110.0, "bpm1": 112.0, "per_bar": 4, "dur": 60.0},
    "iii 5/4 @137.5 accented downbeat": {"kind": "const", "bpm": 137.5, "per_bar": 5, "dur": 40.0},
}
DRIFT_CASE = "ii 4/4 drifting 110->112 over 60 s"


def onsets(y, sr=44100):
    """Plain onset picker for the baseline: rising edge of the rectified
    signal, 0.1 s refractory. Independent of any model."""
    a = np.abs(y)
    hot = np.flatnonzero(a > 0.2)
    out, last = [], -1
    for i in hot:
        if last < 0 or i - last > 0.1 * sr:
            out.append(i / sr)
        last = i
    return out


def constant_grid(ons, dur, anchor="median"):
    """What a constant-ratio method does: one period for the whole song.
    anchor="median": the median inter-onset tempo, phase from the first onset
    (a typical tempo estimator). anchor="leastsq": the best possible single
    line through every onset (the most a constant fit can ever do)."""
    if anchor == "median":
        gaps = sorted(b - a for a, b in zip(ons, ons[1:]))
        p = gaps[len(gaps) // 2]
        t0 = ons[0]
    else:
        k = np.arange(len(ons))
        p, t0 = np.polyfit(k, ons, 1)
    out, k = [], 0
    while t0 + k * p < dur:
        out.append(t0 + k * p)
        k += 1
    return out


# ── 1. pack shape (stdlib) ──────────────────────────────────────────────────
print("the producer pack: two process-lane modes, grid and mix, in the Producer room")
pack = next(p for p in engines.packs() if p["id"] == "producer")
check("process lane, producer cap", pack["lane_kind"] == "process" and pack["cap"] == "producer")
check("mode ids grid and mix, words, room", list(pack["graphs"]) == ["grid", "mix"]
      and pack["mode_words"]["grid"] == "Grid check (beats and bars)"
      and pack["mode_words"]["mix"] == "Mix tracks"
      and engines.mode_room("producer", "grid") == "producer"
      and engines.mode_room("producer", "mix") == "producer")
check("the room's 'Which one?' list gets a one-line note for grid, as every other mode does",
      bool(engines.mode_note("producer", "grid")), engines.mode_note("producer", "grid"))
fids = [f["id"] for f in engines.fields("producer", "grid")]
check("fields: audio input (cover's own id), drum stem, beats per bar, bar-start anchor, expected bpm",
      fids == ["source_audio_name", "use_drum_stem", "beats_per_bar", "first_downbeat", "expected_bpm"], fids)
check("the audio field is an 'audio' upload like Cover's",
      engines.fields("producer", "grid")[0]["type"] == "audio"
      and next(f for f in engines.fields("audio", "cover") if f["id"] == "source_audio_name")["type"] == "audio")
plan = engines.graph_for("producer", "grid", {"use_drum_stem": True, "expected_bpm": 110}, {})
argv = plan["steps"][0]["argv"]
check("plan: python, helper, input, job dir, drum stem, expected bpm",
      argv[0] == "{bin:producer_python}" and "--drum-stem" in argv and argv[argv.index("--expected-bpm") + 1] == "110"
      and argv[argv.index("--in") + 1] == "{in:source_audio_name}" and "--out" in argv, argv)
check("plan outputs are the two artefacts", plan["outputs"] == ["grid-check.mp3", "grid.json"])
try:
    engines.graph_for("producer", "grid", {"expected_bpm": 9000}, {})
    check("expected_bpm out of range is refused", False, "no error")
except ValueError as e:
    check("expected_bpm out of range is refused with a sentence", "between" in str(e), str(e))

# ── 2. analyse() on exact beat times (stdlib) ───────────────────────────────
print("analyse(): per-beat in, meter / tempo per bar / drift out")
exact = {n: truth(c) for n, c in CASES.items()}
for name, (beats, downs, bars) in exact.items():
    rep = gc.analyse(beats, downs)
    want = CASES[name]["per_bar"]
    check("%s: every bar has %d beats" % (name, want), {b["beats"] for b in rep["bars"]} == {want},
          rep["meter"])
    worst = max(abs(b["bpm"] - tb[2]) / tb[2] for b, tb in zip(rep["bars"], bars))
    check("%s: per-bar bpm within 1%% on exact times (worst %.3f%%)" % (name, worst * 100), worst < 0.01)
rep = gc.analyse(*exact[DRIFT_CASE][:2])
check("drift is reported for the ramp (first vs last quarter; got %+.2f%%)" % rep["drift_pct"],
      1.0 < rep["drift_pct"] < 2.6, rep["drift_pct"])
check("constant case reports ~0 drift", abs(gc.analyse(*exact["i  4/4 @110 constant"][:2])["drift_pct"]) < 0.05)
check("summary line shape", gc.summary_line(gc.analyse(*exact["i  4/4 @110 constant"][:2]), 110)
      .startswith("4/4, 110.0 BPM (asked 110), drift "), gc.summary_line(gc.analyse(*exact["i  4/4 @110 constant"][:2]), 110))
try:
    gc.analyse([1.0, 2.0], [1.0])
    check("too few beats is refused", False, "no error")
except gc.GridError as e:
    check("too few beats is refused with a sentence", "Only 2 beats" in str(e), str(e))

# beats-per-bar: the phase whose beats carry the highest mean downbeat logit
logits = [0.1, 2.0, 0.0, -0.2, 0.3, 1.8, 0.1, 0.0, -0.1, 2.2, 0.2, 0.1]   # peaks at beats 1, 5, 9 (period 4)
bt = [0.5 * i for i in range(12)]
d, k, margin, means = gc.phase_downbeats(bt, logits, 4)
check("phase_downbeats picks the phase with the highest mean logit (phase 1)", k == 1 and d == bt[1::4], (k, d))
check("the margin is best minus second-best mean logit", abs(margin - (means[1] - sorted(means)[-2])) < 1e-9 and margin > 1.0, margin)
check("the beats are the caller's, untouched, and bars are n beats apart",
      all(abs((b - a) - 2.0) < 1e-9 for a, b in zip(d, d[1:])))
rep = gc.analyse(bt, d, 4)
check("user-set meter says so in the label", rep["meter"]["label"] == "4/4 (you set 4)", rep["meter"])
try:
    gc.phase_downbeats(bt[:5], logits[:5], 4)
    check("too few beats for the meter is refused", False, "no error")
except gc.GridError as e:
    check("too few beats for the meter is refused with a sentence", "too few" in str(e), str(e))
try:
    engines.graph_for("producer", "grid", {"beats_per_bar": 9}, {})
    check("beats_per_bar out of range is refused", False, "no error")
except ValueError as e:
    check("beats_per_bar out of range is refused with a sentence", "2 to 7" in str(e), str(e))
pl = engines.graph_for("producer", "grid", {"beats_per_bar": "5"}, {})["steps"][0]["argv"]
check("beats_per_bar 5 reaches the helper; auto does not",
      pl[pl.index("--beats-per-bar") + 1] == "5"
      and "--beats-per-bar" not in engines.graph_for("producer", "grid", {"beats_per_bar": "auto"}, {})["steps"][0]["argv"])
# first_downbeat: advanced number field, used only with a numeric meter
f_fd = next(f for f in engines.fields("producer", "grid") if f["id"] == "first_downbeat")
check("first_downbeat field: advanced number 'A bar starts at (seconds)' with units",
      f_fd["type"] == "number" and f_fd["tier"] == "advanced" and f_fd["label"] == "A bar starts at (seconds)"
      and f_fd.get("units") and "ignored with auto" in f_fd["hint"], f_fd)
pl = engines.graph_for("producer", "grid", {"beats_per_bar": "5", "first_downbeat": 12.4}, {})["steps"][0]["argv"]
check("first_downbeat reaches the helper with a numeric meter",
      pl[pl.index("--first-downbeat") + 1] == "12.4", pl)
check("first_downbeat is not passed with auto, nor when blank",
      "--first-downbeat" not in engines.graph_for("producer", "grid", {"beats_per_bar": "auto", "first_downbeat": 12.4}, {})["steps"][0]["argv"]
      and "--first-downbeat" not in engines.graph_for("producer", "grid", {"beats_per_bar": "5", "first_downbeat": ""}, {})["steps"][0]["argv"])
for bad in (-1, "abc", "inf", "-inf", "nan", float("inf")):
    try:
        engines.graph_for("producer", "grid", {"beats_per_bar": "5", "first_downbeat": bad}, {})
        check("first_downbeat %r is refused" % (bad,), False, "no error")
    except ValueError as e:
        check("first_downbeat %r is refused with a sentence" % (bad,), "first_downbeat" in str(e) and "\n" not in str(e), str(e))

for bad in ("inf", "-inf", "nan", 9000, 5):
    try:
        engines.graph_for("producer", "grid", {"expected_bpm": bad}, {})
        check("expected_bpm %r is refused" % (bad,), False, "no error")
    except ValueError as e:
        check("expected_bpm %r is refused with a sentence" % (bad,), "expected_bpm" in str(e) and "\n" not in str(e), str(e))

# anchor snapping (pure): beats every 0.5 s from 1.0, 5 per bar
ab = [1.0 + 0.5 * i for i in range(20)]
d, k, snapped = gc.anchor_downbeats(ab, 4.05, 5)        # nearest beat is index 6 (t=4.0): phase 6 mod 5 = 1
check("anchor snaps to the nearest beat, phase = index mod n, bars every n beats",
      k == 1 and abs(snapped - 4.0) < 1e-9 and d == ab[1::5] and all(abs((b - a) - 2.5) < 1e-9 for a, b in zip(d, d[1:])), (k, snapped, d))
d2, k2, _ = gc.anchor_downbeats(ab, 1.14, 5)            # 0.14 s off beat 0: inside the 0.15 s limit
check("an anchor 0.14 s from a beat still snaps (phase 0)", k2 == 0 and d2 == ab[0::5], (k2, d2))
try:
    gc.anchor_downbeats(ab, 4.2, 5)                      # 0.2 s from the nearest beat
    check("an anchor more than 0.15 s from any beat is refused", False, "no error")
except gc.GridError as e:
    check("an anchor more than 0.15 s from any beat is refused with one sentence naming the times",
          "0.15 s" in str(e) and "4.20 s" in str(e) and "\n" not in str(e), str(e))
rep_a = gc.analyse(ab, d, 5, snapped)
check("anchored report: label, anchor and source in the meter",
      rep_a["meter"]["label"] == "5/4 (you set 5, bars from 4.00 s)" and rep_a["meter"]["anchor"] == 4.0
      and rep_a["meter"]["source"] == "anchor", rep_a["meter"])
check("unanchored reports: source model (auto) / phase-logit (set), anchor null",
      gc.analyse(ab, ab[0::4])["meter"]["source"] == "model" and gc.analyse(ab, ab[0::4])["meter"]["anchor"] is None
      and gc.analyse(ab, ab[0::5], 5)["meter"]["source"] == "phase-logit", "")
for mg, guess in ((0.5, True), (gc.PHASE_MARGIN_TRUSTED, False), (19.0, False)):
    r5 = gc.analyse(ab, ab[0::5], 5)
    r5["meter"]["margin"] = mg
    check("margin %.1f on a set meter: 'is a guess' %s" % (mg, "appended" if guess else "absent"),
          ("is a guess" in gc.summary_line(r5)) == guess, gc.summary_line(r5))
check("an anchored or auto summary never says guess, whatever the margin",
      "guess" not in gc.summary_line(dict(rep_a, meter=dict(rep_a["meter"], margin=0.1)))
      and "guess" not in gc.summary_line(gc.analyse(ab, ab[0::4])))
check("anchored summary wording",
      gc.summary_line(rep_a).startswith("5/4 (you set 5, bars from 4.00 s), "), gc.summary_line(rep_a))

f_bpb = next(f for f in engines.fields("producer", "grid") if f["id"] == "beats_per_bar")
check("beats_per_bar field: select auto,2..7 default auto",
      f_bpb["type"] == "select" and f_bpb["default"] == "auto" and f_bpb["options"] == ["auto", 2, 3, 4, 5, 6, 7])

# ── 3. missing tool: one sentence, no traceback ─────────────────────────────
print("missing tool: a Python without beat_this ends the job with one sentence")


def wrapper(name, env=""):
    """A configured 'python' that cannot see the installed packages (-S), the
    way a user's wrong python would; env lets a stub beat_this through.
    POSIX it is the shell script it always was; on Windows the same wrapper is
    a .cmd that sets those variables and runs this Python with -S."""
    p = os.path.join(WORK, name)
    lines = ""
    if env.strip():
        name_, _, value = env.strip().partition("=")
        lines += 'set "%s=%s"\r\n' % (name_, value)
    lines += '@"%s" -S %%*\r\n' % sys.executable
    return make_program(p, "#!/bin/sh\n%sexec %s -S \"$@\"\n" % (env, sys.executable),
                        windows_cmd=lines)


import runner  # noqa: E402

bare = wrapper("python-no-beat-this")
try:
    engines.graph_for("producer", "grid", {}, {"producer_python": bare, "ffmpeg": "ffmpeg"})
    check("request refused when beat_this is absent", False, "no error")
except ValueError as e:
    msg = str(e)
    check("the refusal is ONE sentence naming beat_this, what to install and where to point BWF",
          "beat_this" in msg and "pip install" in msg and "BWF_PRODUCER_PYTHON" in msg
          and "\n" not in msg and "Traceback" not in msg and msg.count(". ") == 0, msg)
    named = msg.split("pointed at (", 1)[-1].split(")", 1)[0]
    check("its install command uses that Python's own pip (a bare pip may be another Python's)",
          "%s -m pip install torch torchaudio" % named in msg and "%s -m pip install git+" % named in msg, msg)

# And the job itself (the run plan with no preflight), through the real runner:
plan = engines.graph_for("producer", "grid", {}, {})
steps = runner.resolve_plan(plan, {"producer_python": bare, "ffmpeg": "ffmpeg"}, WORK,
                            {"source_audio_name": os.path.join(WORK, "none.wav")}, os.path.join(ROOT, "engines"))
ok, tail, err = runner.run_steps(steps, cwd=WORK, progress=plan["progress"])
check("job fails (not crashes): exit 2", (not ok) and "exit code 2" in (err or ""), (ok, err))
check("job output is exactly the sentence: no traceback, no stack lines",
      len(tail) == 1 and "beat_this" in tail[0] and not any("Traceback" in t or 'File "' in t for t in tail), tail)
check("no outputs were written", not os.path.exists(os.path.join(WORK, "grid.json")))

stub = os.path.join(WORK, "stub")
os.makedirs(os.path.join(stub, "beat_this"))
open(os.path.join(stub, "beat_this", "__init__.py"), "w").close()
no_demucs = wrapper("python-no-demucs", "PYTHONPATH=%s " % stub)
try:
    engines.graph_for("producer", "grid", {"use_drum_stem": True}, {"producer_python": no_demucs, "ffmpeg": "ffmpeg"})
    check("request refused when Demucs is absent and the drum stem is on", False, "no error")
except ValueError as e:
    check("Demucs sentence names Demucs, pip install, and the tick-box alternative",
          "Demucs" in str(e) and "pip install demucs" in str(e) and "\n" not in str(e), str(e))
try:
    engines.graph_for("producer", "grid", {"use_drum_stem": False}, {"producer_python": no_demucs, "ffmpeg": "ffmpeg"})
    check("Demucs is not needed when the drum stem is off", True)
except ValueError as e:
    check("Demucs is not needed when the drum stem is off", False, str(e))

# ── 4. the constant-period baseline: must FAIL the drifting case ────────────
print("baseline: one constant period (what round 8 did) vs the drifting case")
if np is None:
    skip("constant-period baseline", "numpy not installed")
else:
    wavs, ys = {}, {}
    for name, case in CASES.items():
        wavs[name] = os.path.join(WORK, name.split()[0] + ".wav")
        ys[name] = synth(case, wavs[name])
    f = {}
    for name, case in CASES.items():
        ref = truth(case)[0]
        f[name] = f_measure(constant_grid(onsets(ys[name]), case["dur"]), ref)
        print("        baseline (median tempo, first-onset phase) F=%.3f  %s" % (f[name], name))
    check("baseline passes the constant case (F>=0.95): it is a fair baseline", f["i  4/4 @110 constant"] >= 0.95)
    check("baseline FAILS the drifting case (F<0.95)", f[DRIFT_CASE] < 0.95, f[DRIFT_CASE])
    ls = f_measure(constant_grid(onsets(ys[DRIFT_CASE]), 60.0, "leastsq"), truth(CASES[DRIFT_CASE])[0])
    print("        info: the best possible single line (least squares) scores F=%.3f on the same case" % ls)

# ── 5. the real helper on the synthetic cases ───────────────────────────────
print("beat_this (checkpoint final0) through the real helper: F at +-70 ms, meter, per-bar bpm")

# Bar starts need musical material: beat_this's main downbeat cue is a harmonic
# change at the bar start, which a bare click track does not have (measured: on
# these click tracks the downbeat head marks the wrong bars in all three cases,
# so they gate BEAT placement, tempo and drift only). The downbeat and meter
# gates use the bar-structured toy songs further down. 5/4 with the model's own
# bar starts is a KNOWN LIMIT: the observed numbers print every run, it does not
# fail the suite, and an unexpected pass is announced so it can be promoted.
KNOWN_LIMIT = "beat_this's own bar starts do not find a 5-beat bar (measured 2026-09-30, final0, dbn off)"


def limit(name, cond, detail="", why=None):
    why = why or KNOWN_LIMIT
    if cond:
        print("  XPASS %s -- passed unexpectedly: promote this known limit to a hard check" % name)
    else:
        print("  KNOWN-FAIL %s\n        -> observed: %s\n        -> limit: %s" % (name, detail, why))


if not HAVE_BEAT_THIS:
    skip("beat_this synthetic cases", "beat_this is not importable in this Python; run with the producer venv's python")
elif np is None or not FFMPEG:
    skip("beat_this synthetic cases", "needs numpy and ffmpeg")
else:
    for name, case in CASES.items():
        out = os.path.join(WORK, "out_" + name.split()[0])
        r = subprocess.run([sys.executable, HELPER, "--in", wavs[name], "--out", out, "--ffmpeg", FFMPEG,
                            "--expected-bpm", "%g" % case["bpm"]], capture_output=True, text=True)
        gj = os.path.join(out, "grid.json")
        if r.returncode != 0 or not os.path.isfile(gj):
            check("%s: helper ran" % name, False, (r.returncode, r.stdout[-400:], r.stderr[-400:]))
            continue
        print("  RAN   %s: helper finished, grid.json written (beat_this model ran, not skipped)" % name)
        g = json.load(open(gj))
        ref_b, ref_d, ref_bars = truth(case)
        fb, fd = f_measure(g["beats"], ref_b), f_measure(g["downbeats"], ref_d)
        inner = g["bars"][1:-1] or g["bars"]           # the first/last bar touch the file edges
        counts = {b["beats"] for b in inner}
        errs = []
        for b in inner:
            tb = min(ref_bars, key=lambda x: abs(x[0] - b["start"]))
            errs.append(abs(b["bpm"] - tb[2]) / tb[2])
        ref_bpm = 60.0 * (len(ref_b) - 1) / (ref_b[-1] - ref_b[0])
        print("        %s: beat F=%.3f downbeat F=%.3f meter=%s max bar-bpm err=%.2f%% median bpm=%.2f (true mean %.2f) drift=%+.2f%%"
              % (name, fb, fd, g["meter"]["label"], max(errs) * 100, g["bpm_median"], ref_bpm, g["drift_pct"]))
        print("        summary: " + g["summary"])
        check("%s: beat F >= 0.95 at +-70 ms" % name, fb >= 0.95, fb)
        check("%s: median tempo within 1%% of the true mean" % name,
              abs(g["bpm_median"] - ref_bpm) / ref_bpm <= 0.01, (g["bpm_median"], ref_bpm))
        check("%s: per-bar bpm within 1%% of the true bar tempo (worst %.2f%%)" % (name, max(errs) * 100),
              max(errs) <= 0.01, max(errs))
        true_drift = gc.analyse(ref_b, ref_d)["drift_pct"]        # the same measure, on the true beat times
        check("%s: drift %+.2f%% vs %+.2f%% on the true beats (within 0.6 points)" % (name, g["drift_pct"], true_drift),
              abs(g["drift_pct"] - true_drift) <= 0.6, (g["drift_pct"], true_drift))
        mp3 = os.path.join(out, "grid-check.mp3")
        pk = subprocess.run([FFMPEG, "-v", "error", "-i", mp3, "-f", "f32le", "-ac", "2", "-"],
                            capture_output=True).stdout
        peak = float(np.max(np.abs(np.frombuffer(pk, dtype="<f4")))) if pk else 9
        check("%s: grid-check.mp3 decodes and is not clipped (peak %.2f)" % (name, peak), 0.05 < peak <= 1.0, peak)

# ── 6. bar starts on musical material ───────────────────────────────────────
print("bar starts on bar-structured toy songs (hats, kick, snare, a root that changes each bar)")


def run_helper(wav, name, extra=()):
    out = os.path.join(WORK, "out_" + name.split()[0] + "_" + "_".join(extra).replace("-", ""))
    r = subprocess.run([sys.executable, HELPER, "--in", wav, "--out", out, "--ffmpeg", FFMPEG] + list(extra),
                       capture_output=True, text=True)
    gj = os.path.join(out, "grid.json")
    if r.returncode != 0 or not os.path.isfile(gj):
        check("%s %s: helper ran" % (name, " ".join(extra)), False, (r.returncode, r.stdout[-400:], r.stderr[-400:]))
        return None
    return json.load(open(gj))


def bar_stats(g, c):
    _, true_d = musical_truth(c)
    inner = g["bars"][1:-1] or g["bars"]
    share = sum(1 for b in inner if b["beats"] == c["bpb"]) / float(len(inner))
    return f_measure(g["downbeats"], true_d), share


if not HAVE_BEAT_THIS:
    skip("musical bar-start cases", "beat_this is not importable in this Python; run with the producer venv's python")
elif np is None or not FFMPEG:
    skip("musical bar-start cases", "needs numpy and ffmpeg")
else:
    for name, c in MUSICAL.items():
        y, _bar = musical(c["bpb"], c["bpm"])
        wav = os.path.join(WORK, name.split()[0] + ".wav")
        write_wav(wav, y, MSR)
        true_b, _true_d = musical_truth(c)
        # auto: the model's own bar starts
        g = run_helper(wav, name)
        if g:
            print("  RAN   %s auto: helper finished (beat_this model ran, not skipped)" % name)
            fd, share = bar_stats(g, c)
            print("        %s auto: beat F=%.3f downbeat F=%.3f bars with %d beats %.0f%% meter=%s"
                  % (name, f_measure(g["beats"], true_b), fd, c["bpb"], share * 100, g["meter"]["label"]))
            check("%s auto: beat F >= 0.95" % name, f_measure(g["beats"], true_b) >= 0.95)
            ok_auto = fd >= 0.9 and share >= 0.9
            if c["bpb"] == 4:
                check("%s auto: downbeat F >= 0.9 and >= 90%% of bars have 4 beats" % name, ok_auto, (fd, share))
            else:
                limit("%s auto: downbeat F >= 0.9 and >= 90%% of bars have %d beats" % (name, c["bpb"]), ok_auto,
                      "downbeat F=%.3f, %.0f%% of bars right, meter %s" % (fd, share * 100, g["meter"]["label"]))
        # the meter given by the user
        g = run_helper(wav, name, ("--beats-per-bar", str(c["bpb"])))
        if g:
            fd, share = bar_stats(g, c)
            m = g["meter"]
            print("        %s with beats-per-bar=%d: downbeat F=%.3f bars right %.0f%% phase=%s margin=%.3f | %s"
                  % (name, c["bpb"], fd, share * 100, m["phase"], m["margin"], g["summary"]))
            if c["bpb"] == 4:
                check("%s with beats-per-bar=%d: downbeat F >= 0.9 at +-70 ms" % (name, c["bpb"]), fd >= 0.9, fd)
            else:
                limit("%s with beats-per-bar=%d: downbeat F >= 0.9 at +-70 ms" % (name, c["bpb"]), fd >= 0.9,
                      "downbeat F=%.3f, phase %s chosen, mean logit per phase %s" % (fd, m["phase"], m["mean_logit_per_phase"]),
                      "the downbeat logits do not single out the true bar start in 5/4 either: phase 4 (one beat "
                      "early) won on all 6 seeds tried (measured 2026-09-30, final0); the bars are 5 beats by construction, so that check proves nothing")
            check("%s with beats-per-bar=%d: >= 90%% of bars have %d beats" % (name, c["bpb"], c["bpb"]), share >= 0.9, share)
            check("%s with beats-per-bar=%d: summary says the meter came from the user" % (name, c["bpb"]),
                  "(you set %d)" % c["bpb"] in g["summary"], g["summary"])
            check("%s with beats-per-bar=%d, no anchor: %s in the summary (margin %.2f)"
                  % (name, c["bpb"], "'is a guess'" if c["bpb"] == 5 else "no 'guess'", m["margin"]),
                  ("is a guess" in g["summary"]) == (c["bpb"] == 5), (m["margin"], g["summary"]))
            check("%s no anchor: meter source is phase-logit, anchor null" % name,
                  m["source"] == "phase-logit" and m["anchor"] is None, m)
        if c["bpb"] == 5:
            # the user's ear: bar 3 (0-based 2) of the true grid
            anchor_t = _true_d[2]
            g = run_helper(wav, name, ("--beats-per-bar", "5", "--first-downbeat", "%.3f" % anchor_t))
            if g:
                print("  RAN   %s anchored at the true bar 3 (%.3f s): helper finished (beat_this model ran, not skipped)" % (name, anchor_t))
                fd, share = bar_stats(g, c)
                inner = g["bars"][1:-1] or g["bars"]
                print("        %s anchored: downbeat F=%.3f full bars with 5 beats %.0f%% | %s" % (name, fd, share * 100, g["summary"]))
                check("%s anchored at the true bar 3: downbeat F >= 0.95 at +-70 ms" % name, fd >= 0.95, fd)
                check("%s anchored: every full bar has 5 beats" % name, all(b["beats"] == 5 for b in inner), [b["beats"] for b in inner])
                check("%s anchored: source anchor, anchor within 70 ms of the typed time, no 'guess'" % name,
                      g["meter"]["source"] == "anchor" and abs(g["meter"]["anchor"] - anchor_t) <= TOL
                      and "guess" not in g["summary"] and "bars from" in g["summary"], (g["meter"], g["summary"]))

# ── 7. through the real server: notes are the one summary line, json is a file tile ──
print("process job through the real server: notes == the summary line")
if not HAVE_BEAT_THIS or np is None or not FFMPEG:
    skip("server job notes", "needs beat_this, numpy and ffmpeg in this Python")
else:
    import socket
    import threading
    import time
    import urllib.request
    from http.server import ThreadingHTTPServer
    s0 = socket.socket()
    s0.bind(("127.0.0.1", 0))
    PORT = s0.getsockname()[1]
    s0.close()
    os.makedirs(os.path.join(WORK, "srvdata"))
    cfg = os.path.join(WORK, "srv-config.json")
    with open(cfg, "w") as f:
        json.dump({"port": PORT, "bind": "127.0.0.1", "title": "p1",
                   "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
                   "lanes": [{"id": "proc", "name": "Proc", "kind": "process", "caps": ["producer"]}]}, f)
    os.environ["GENCENTER_CONFIG"] = cfg
    os.environ["GENCENTER_DATA"] = os.path.join(WORK, "srvdata")
    spec = importlib.util.spec_from_file_location("srv_p1", os.path.join(ROOT, "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=srv.process_worker, args=(srv.LANE_BY_ID["proc"],), daemon=True).start()
    check("server: a .json output is a 'file' (a named download tile), not an image",
          srv._process_media("grid.json") == "file" and srv._process_media("grid-check.mp3") == "audio")
    if os.environ.get("BWF_PRODUCER_PYTHON") != sys.executable:
        skip("server job notes", "run with BWF_PRODUCER_PYTHON unset so this Python is the producer python")
    else:
        time.sleep(1.0)
        srv.poll_process_lane(srv.LANE_BY_ID["proc"])

        def http(path, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data, headers={"Content-Type": ctype})
            try:
                with urllib.request.urlopen(r) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return json.loads(e.read())
        bnd = "p1b"
        wavb = open(wavs[next(iter(CASES))], "rb").read()
        up = http("/api/upload", ("".join("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (bnd, k, v)
                                          for k, v in (("lane", "proc"), ("kind", "producer"), ("mode", "grid")))
                                  + "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                                  "Content-Type: application/octet-stream\r\n\r\n" % bnd).encode() + wavb
                  + ("\r\n--%s--\r\n" % bnd).encode(), "multipart/form-data; boundary=" + bnd)
        res = http("/api/generate", {"lane": "proc", "kind": "producer", "mode": "grid",
                                     "source_audio_name": up["files"][0]["name"], "seed": 1})
        jid = (res.get("job") or {}).get("id")
        check("server: the grid job was accepted", bool(jid), res)
        j = {}
        for _ in range(600):
            with srv.JOBS_LOCK:
                j = json.loads(json.dumps(srv.JOBS.get(jid) or {}))
            if j.get("status") in ("done", "error"):
                break
            time.sleep(0.5)
        check("server: the grid job finished done", j.get("status") == "done", (j.get("status"), j.get("error"), j.get("notes")))
        jg = json.load(open(os.path.join(srv.LOCAL_OUTPUTS_DIR, jid, "grid.json")))
        check("server: job notes are exactly the one summary line", j.get("notes") == [jg["summary"]], (j.get("notes"), jg["summary"]))
        check("server: outputs are the mp3 first, then the json as a file tile",
              [(o["filename"], o["media"]) for o in j.get("outputs", [])] == [("grid-check.mp3", "audio"), ("grid.json", "file")],
              j.get("outputs"))

shutil.rmtree(WORK, ignore_errors=True)
print()
if SKIPPED:
    print("SKIPPED %d: %s" % (len(SKIPPED), "; ".join(SKIPPED)))
if FAILED:
    print("FAILED %d:" % len(FAILED))
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("all checks passed")
