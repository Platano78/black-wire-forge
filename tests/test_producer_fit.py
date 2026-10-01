"""Producer room "Fit" gate.

Fit reads the beats of a source song (the song a part was made in), a target
song (the beat it should sit on) and time-stretches a PART beat by beat so
that source beat i0+k lands on target beat j0+k -- it follows a drifting
tempo, not a constant ratio. It runs on a process lane under the producer
Python (beat_this) and drives ffmpeg. mix.py's precedent: the helper is a
standalone script; this gate runs it as a subprocess.

The GREEN check is model-backed: a synthetic source that drifts 98 -> 102
BPM over 45 s, a constant 95 BPM target over 50 s, and a part that is a
60 ms 1 kHz burst on every source beat. A correct fit puts every burst on
the TRUE target beat (median <= 25 ms, p90 <= 50 ms). A single constant
ratio cannot: the same measurement on its output must exceed 50 ms at p90
(pure numpy, no model).

Refusals: the part longer than the source, a negative bar anchor, and a bar
anchor with no beat within gc.ANCHOR_SNAP_S -- each one sentence, exit 2,
no traceback. The anchor case is proved on the helper's pure anchor function
(it is importable without beat_this).

Run:
    python3 tests/test_producer_fit.py                # pack + pure checks
    /path/to/producer-venv/bin/python tests/test_producer_fit.py   # full gate
"""
import importlib.util
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    if detail != "" and not isinstance(detail, str):
        detail = repr(detail)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


def skip(section, reason):
    print("  SKIP  %s: %s" % (section, reason))


import engines  # noqa: E402

FIT = os.path.join(ROOT, "engines", "producer_tools", "fit.py")
FFMPEG = shutil.which("ffmpeg")
SR = 44100
TMPDIR = os.environ.get("TMPDIR") or None
WORK = tempfile.mkdtemp(prefix="bwf_fit_", dir=TMPDIR)
OUT = os.path.join(WORK, "out")

HAVE_NP = importlib.util.find_spec("numpy") is not None

# ---------------------------------------------------------------------------
# The true beat times (ground truth, never touched by the model)
#
# SOURCE: 4/4, drifting 98 -> 102 BPM (45 s). 98 is where beat_this locks
# in phase at this arrangement; from ~100 BPM up it half-steps (checked
# before baking), so the test sits just below the half-step zone.
# TARGET: 4/4, constant 95 BPM (50 s)
# PART:   a 60 ms 1 kHz burst on every source beat, as long as the source
# ---------------------------------------------------------------------------
SRC_START, N_SRC = 0.03, 75
TGT_START, N_TGT = 0.15, 79
SRC_LEN, TGT_LEN = 45.0, 50.2

SRC_BEATS = []
_b = SRC_START
_bpm = 98.0
for _k in range(N_SRC):
    SRC_BEATS.append(_b)
    _b += 60.0 / _bpm
    _bpm = 98.0 + 4.0 * (_k + 1) / (N_SRC - 1)   # smooth drift 98 -> 102

TGT_BEATS = [TGT_START + _k * 60.0 / 95.0 for _k in range(N_TGT)]
TGT_STEP = 60.0 / 95.0
SRC_MEAN_STEP = (SRC_BEATS[-1] - SRC_BEATS[0]) / (N_SRC - 1)


def write_wav(path, a):
    """float array (N, 2) in [-1, 1] -> 16-bit stereo WAV at SR."""
    import numpy as np
    a16 = (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2")
    raw = a16.tobytes()
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVEfmt " +
                struct.pack("<IHHIIHH", 16, 1, 2, SR, SR * 4, 4, 16) +
                b"data" + struct.pack("<I", len(raw)) + raw)


def _add_tone(a, t0, dur, freq, amp, decay, n):
    import numpy as np
    s = int(round(t0 * SR))
    e = min(n, s + int(round(dur * SR)))
    if e <= s:
        return
    t = np.arange(e - s, dtype="float64") / SR
    a[s:e, :] += (amp * np.exp(-decay * t) * np.sin(2.0 * math.pi * freq * t))[:, None]


def render_song(beats, length_s):
    """A 4/4 musical track on the given beats: a decaying kick with a click,
    a snare colour on beat 3, an off-beat hat, a low bass note. The click
    plus the bass keep beat_this locked at the written tempo at both 100 and
    95 BPM (checked before baking: drop the click and it half-locks, drop
    the bass and it loses the grid). Both channels equal."""
    import numpy as np
    n = int(round(length_s * SR))
    a = np.zeros((n, 2), dtype="float64")
    for i, b in enumerate(beats):
        step = (beats[i + 1] - beats[i]) if i + 1 < len(beats) else (beats[i] - beats[i - 1])
        _add_tone(a, b, 0.25, 55.0, 0.55, 2.5, n)
        _add_tone(a, b, 0.06, 200.0, 0.3, 12.0, n)
        if i % 4 == 2:
            _add_tone(a, b, 0.15, 1500.0, 0.2, 10.0, n)
        _add_tone(a, b + step / 2.0, 0.05, 6000.0, 0.09, 20.0, n)
        _add_tone(a, b, step, 82.0, 0.22, 0.8, n)
    return a


def render_part(beats, length_s):
    """Silence with a 60 ms 1 kHz burst starting on every beat."""
    import numpy as np
    n = int(round(length_s * SR))
    a = np.zeros((n, 2), dtype="float64")
    for b in beats:
        _add_tone(a, b, 0.06, 1000.0, 0.7, 0.0, n)
    return a


def build_audio():
    """(source, part, target, longer-part) wav paths, or None when the
    needed tools (numpy, ffmpeg) are absent."""
    if not HAVE_NP:
        return None
    S, P, T, L = (os.path.join(WORK, name) for name in ("source.wav", "part.wav", "target.wav", "part_long.wav"))
    write_wav(S, render_song(SRC_BEATS, SRC_LEN))
    write_wav(P, render_part(SRC_BEATS, SRC_LEN))
    write_wav(T, render_song(TGT_BEATS, TGT_LEN))
    write_wav(L, render_part(SRC_BEATS, SRC_LEN + 3.0))
    return S, P, T, L


AUDIO = build_audio() if FFMPEG else None

# ---------------------------------------------------------------------------
# GREEN (model-backed): the fit lands every burst on the true target beat
# ---------------------------------------------------------------------------
HAVE_BT = importlib.util.find_spec("beat_this") is not None
if not AUDIO:
    print("green fit: needs ffmpeg and numpy")
    if FFMPEG is None:
        skip("green fit (end to end)", "ffmpeg not on PATH")
    else:
        skip("green fit (end to end)", "numpy not importable in this Python")
elif not HAVE_BT:
    skip("green fit (end to end)", "beat_this is not importable in this Python; run with the producer venv's python")
else:
    S, P, T, _ = AUDIO
    print("green fit: a drifting 98->102 BPM source, a constant 95 BPM target, a burst-per-beat part")
    r = subprocess.run([sys.executable, FIT, "--source", S, "--part", P, "--target", T,
                        "--out", OUT, "--ffmpeg", FFMPEG], capture_output=True, text=True)
    check("fit.py ran to exit 0", r.returncode == 0,
          "rc=%s\nstdout=%s\nstderr=%s" % (r.returncode, r.stdout[-800:], r.stderr[-500:]))
    for name in ("fitted.wav", "preview.mp3", "fit.json"):
        p = os.path.join(OUT, name)
        check("%s exists and is non-empty" % name, os.path.isfile(p) and os.path.getsize(p) > 0, p)
    out_lines = [l for l in r.stdout.splitlines() if l.strip()]
    check("last stdout line starts with 'fitted '", bool(out_lines) and out_lines[-1].startswith("fitted "),
          repr(out_lines[-3:]))
    for k in range(1, 6):
        check("PROGRESS %d/5 was printed" % k, "PROGRESS %d/5" % k in out_lines, r.stdout[-400:])

    doc = {}
    try:
        doc = json.load(open(os.path.join(OUT, "fit.json")))
    except Exception as e:
        check("fit.json parses", False, repr(e))
    K = int(doc.get("pairs", 0))
    check("fit.json pairs >= 40", K >= 40, doc.get("pairs"))
    check("fit.json method is 'ffmpeg atempo'", doc.get("method") == "ffmpeg atempo", doc.get("method"))
    check("fit.json source_bpm near 100", abs(float(doc.get("source_bpm", 1e9)) - 100.0) <= 2.5, doc.get("source_bpm"))
    check("fit.json target_bpm near 95", abs(float(doc.get("target_bpm", 1e9)) - 95.0) <= 2.5, doc.get("target_bpm"))
    stretch_med = float(doc.get("stretch", {}).get("median", 1e9))
    check("fit.json stretch median near %.3f" % (SRC_MEAN_STEP / TGT_STEP),
          abs(stretch_med - SRC_MEAN_STEP / TGT_STEP) <= 0.05, repr(stretch_med))

    # Onset detection in fitted.wav: 5 ms Goertzel windows at 1 kHz.
    import numpy as np
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", os.path.join(OUT, "fitted.wav"),
                          "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"], capture_output=True).stdout
    x = np.frombuffer(raw, dtype="<f4")
    WIN = int(round(0.005 * SR))
    nw = (len(x) - WIN) // WIN

    def goertzel_power(freq):
        c = math.cos(2.0 * math.pi * freq / SR)
        powers = []
        for i in range(nw):
            s1 = s2 = 0.0
            for v in x[i * WIN:(i + 1) * WIN]:
                s0 = v + 2.0 * c * s1 - s2
                s2, s1 = s1, s0
            powers.append(s1 * s1 + s2 * s2 - 2.0 * c * s1 * s2)
        return powers

    powers = goertzel_power(1000.0)
    mx = max(powers) if powers else 0.0
    THR = 0.3 * mx
    onsets = []
    for i in range(nw):
        if powers[i] > THR and (i < 10 or max(powers[i - 10:i]) < THR):
            onsets.append((i + 0.5) * (WIN / SR))
    check("at least %d onsets were detected (0.8 of %d bursts)" % (math.ceil(0.8 * K), K),
          len(onsets) >= math.ceil(0.8 * K), len(onsets))
    # anchor the expectation where the model said it started
    anchor_t = float(doc.get("target_anchor_s", TGT_START))
    j0 = min(range(N_TGT), key=lambda i: abs(TGT_BEATS[i] - anchor_t))
    expected = TGT_BEATS[j0:j0 + K]
    tgt = np.array(TGT_BEATS)
    errs = [min(abs(o - b) for b in TGT_BEATS) for o in onsets]
    errs_sorted = sorted(errs)
    med = errs_sorted[len(errs_sorted) // 2] if errs_sorted else float("inf")
    p90 = errs_sorted[min(len(errs_sorted) - 1, int(0.9 * len(errs_sorted)))] if errs_sorted else float("inf")
    print("        measured: %d onsets, median %.4f s, p90 %.4f s" % (len(errs), med, p90))
    check("median onset error <= 0.025 s", med <= 0.025, med)
    check("p90 onset error <= 0.05 s", p90 <= 0.05, p90)
    landed = sum(1 for b in expected if min(abs(o - b) for o in onsets) < 0.15)
    check("at least 0.8*K bursts sit on a target beat", landed >= 0.8 * K, "%d of %d" % (landed, K))

# ---------------------------------------------------------------------------
# RED baseline (pure numpy, no model): one constant ratio cannot follow drift
# ---------------------------------------------------------------------------
if not HAVE_NP:
    skip("red constant-ratio baseline", "numpy not importable in this Python")
else:
    import numpy as np
    ratio = TGT_STEP / SRC_MEAN_STEP
    pos = ratio * np.array(SRC_BEATS)          # the whole part stretched once, by that ratio
    tgt = np.array(TGT_BEATS)
    errs = np.array([min(abs(p - t) for t in TGT_BEATS) for p in pos])
    r_med = float(np.median(errs))
    r_p90 = float(np.percentile(errs, 90))
    print("        constant ratio %.4fx: median %.4f s, p90 %.4f s" % (ratio, r_med, r_p90))
    check("RED: the constant-ratio fit's p90 error is > 0.05 s", r_p90 > 0.05,
          "median=%.4f p90=%.4f" % (r_med, r_p90))

# ---------------------------------------------------------------------------
# Refusals: one sentence, exit 2, never a traceback
# ---------------------------------------------------------------------------


def refuse(label, argv):
    rr = subprocess.run([sys.executable, FIT] + argv, capture_output=True, text=True)
    out_lines = [l for l in rr.stdout.splitlines() if l.strip()]
    check("%s -> exit 2" % label, rr.returncode == 2,
          "rc=%s stdout=%s stderr=%s" % (rr.returncode, rr.stdout[-300:], rr.stderr[-300:]))
    check("%s -> exactly one line of stdout" % label, len(out_lines) == 1, repr(rr.stdout))
    check("%s -> no traceback in stderr" % label, "Traceback" not in rr.stderr, rr.stderr[-300:])


if AUDIO:
    S, P, T, L = AUDIO
    refuse("part 3 s longer than the source",
           ["--source", S, "--part", L, "--target", T, "--out", OUT, "--ffmpeg", FFMPEG])
else:
    skip("refusal: part longer than the source", "no audio could be built")
refuse("source-bar-at -1",
       ["--source", "s.wav", "--target", "t.wav", "--out", OUT, "--source-bar-at", "-1",
        "--ffmpeg", FFMPEG or "ffmpeg"])

# the anchor far from every beat: proved on the helper's pure anchor function
_spec = importlib.util.spec_from_file_location("bwf_producer_fit_gate", FIT)
_fit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fit)
try:
    _fit.nearest_beat([0.0, 1.0, 2.0], 10.5)
    check("anchor 8.5 s from the nearest beat refuses", False, "no error raised")
except ValueError as e:
    check("anchor 8.5 s from the nearest beat refuses with one sentence",
          "\n" not in str(e) and str(e).strip() != "", repr(e))
except Exception as e:
    check("anchor 8.5 s from the nearest beat refuses with a ValueError", False, repr(e))
try:
    _fit.nearest_beat([0.0, 1.0, 2.0], 0.9)
    check("an anchor 0.1 s from a beat is accepted", True)
except Exception as e:
    check("an anchor 0.1 s from a beat is accepted", False, repr(e))
try:
    _fit.nearest_beat([0.0, 1.0, 2.0], 0.62)
    check("an anchor 0.38 s from a beat refuses (snap window 0.15 s)", False, "no error raised")
except ValueError:
    check("an anchor 0.38 s from a beat refuses (snap window 0.15 s)", True)
except Exception as e:
    check("an anchor 0.38 s from a beat refuses (snap window 0.15 s)", False, repr(e))

# ---------------------------------------------------------------------------
# Pack: field list, words, room, note, plan
# ---------------------------------------------------------------------------
print("pack: the fit field list and plan")
fids = [f["id"] for f in engines.fields("producer", "fit")]
check("field ids in the spec order",
      fids == ["source_audio_name", "part_audio_name", "target_audio_name",
               "source_bar_at", "target_bar_at", "stretch_tool"], fids)
by_id = {f["id"]: f for f in engines.fields("producer", "fit")}
check("source/part/target are audio fields",
      all(by_id[k]["type"] == "audio" for k in ("source_audio_name", "part_audio_name", "target_audio_name")),
      {k: by_id[k]["type"] for k in ("source_audio_name", "part_audio_name", "target_audio_name")})
check("the part is optional, the two songs are not",
      by_id["part_audio_name"].get("optional") and not by_id["source_audio_name"].get("optional")
      and not by_id["target_audio_name"].get("optional"))
for k in ("source_bar_at", "target_bar_at"):
    f = by_id[k]
    check("%s: number, range [0, 3600], units s, advanced, group Anchors" % k,
          f["type"] == "number" and f["range"] == [0, 3600] and f["units"] == "s"
          and f["tier"] == "advanced" and f["group"] == "Anchors", f)
st = by_id["stretch_tool"]
check("stretch_tool: select, default auto, options auto/ffmpeg",
      st["type"] == "select" and st["default"] == "auto" and st["options"] == ["auto", "ffmpeg"], st)
check("mode word and room", engines.mode_words("producer")["fit"] == "Fit a part onto another beat"
      and engines.mode_room("producer", "fit") == "producer")
check("mode note exists", bool(engines.mode_note("producer", "fit")), engines.mode_note("producer", "fit"))

_spec = importlib.util.spec_from_file_location("bwf_producer_fit_pack", os.path.join(ROOT, "engines", "producer.py"))
prod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prod)
try:
    plan = prod.fit_plan({"source_audio_name": "s.wav", "target_audio_name": "t.wav"}, {})
    argv = plan["steps"][0]["argv"]
    check("fit_plan outputs start with preview.mp3",
          list(plan.get("outputs", []))[:1] == ["preview.mp3"], plan.get("outputs"))
    check("fit_plan argv runs fit.py under the producer python",
          argv[0] == "{bin:producer_python}" and argv[1].endswith("producer_tools/fit.py"), argv)
    check("no --part when the part was not named", "--part" not in argv, argv)
    check("no --source-bar-at when it was not given", "--source-bar-at" not in argv, argv)
except Exception as e:
    check("fit_plan(source, target) plans the fit", False, repr(e))
try:
    argv = prod.fit_plan({"source_audio_name": "s.wav", "part_audio_name": "p.wav",
                          "target_audio_name": "t.wav"}, {})["steps"][0]["argv"]
    check("with a part, --part is passed", "--part" in argv, argv)
except Exception as e:
    check("fit_plan with part_audio_name passes --part", False, repr(e))
try:
    argv = prod.fit_plan({"source_audio_name": "s.wav", "target_audio_name": "t.wav",
                          "source_bar_at": 12.34}, {})["steps"][0]["argv"]
    check("a bar anchor is passed on the command line",
          "--source-bar-at" in argv and argv[argv.index("--source-bar-at") + 1] == "12.34", argv)
except Exception as e:
    check("fit_plan passes --source-bar-at", False, repr(e))
for bad in ("inf", -1, "abc"):
    for field in ("source_bar_at", "target_bar_at"):
        try:
            prod.fit_plan({"source_audio_name": "s.wav", "target_audio_name": "t.wav", "bad": 0,
                           field: bad}, {})
            check("fit_plan refuses %s=%r" % (field, bad), False, "no error raised")
        except ValueError as e:
            check("fit_plan refuses %s=%r with one sentence" % (field, bad),
                  "\n" not in str(e) and str(e).strip() != "", repr(e))
        except Exception as e:
            check("fit_plan refuses %s=%r with a ValueError" % (field, bad), False, repr(e))
try:
    prod.fit_plan({"target_audio_name": "t.wav"}, {})
    check("fit_plan without a source refuses", False, "no error raised")
except ValueError as e:
    check("fit_plan without a source refuses with one sentence",
          "\n" not in str(e) and str(e).strip() != "", repr(e))
try:
    prod.fit_plan({"source_audio_name": "s.wav"}, {})
    check("fit_plan without a target refuses", False, "no error raised")
except ValueError as e:
    check("fit_plan without a target refuses with one sentence",
          "\n" not in str(e) and str(e).strip() != "", repr(e))
try:
    prod.fit_plan({"source_audio_name": "s.wav", "target_audio_name": "t.wav",
                   "stretch_tool": "sox"}, {})
    check("fit_plan refuses stretch_tool 'sox'", False, "no error raised")
except ValueError as e:
    check("fit_plan refuses stretch_tool 'sox' with one sentence",
          "\n" not in str(e) and str(e).strip() != "", repr(e))

try:
    g = engines.graph_for("producer", "fit",
                          {"source_audio_name": "s.wav", "target_audio_name": "t.wav"}, {})
    check("engines.graph_for('producer', 'fit') plans the fit",
          list(g.get("outputs", []))[:1] == ["preview.mp3"], g.get("outputs"))
except Exception as e:
    check("engines.graph_for('producer', 'fit') plans the fit", False, repr(e))

shutil.rmtree(WORK, ignore_errors=True)
print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("all fit checks passed")
