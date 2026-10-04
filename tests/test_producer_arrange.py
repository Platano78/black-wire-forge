"""Producer room "Re-arrange by bars" gate.

Re-arrange cuts a song at its bar starts (grid_check's beat tracking) and plays
the bars back in the order you type, joined with short crossfades. It runs on a
process lane under the producer Python (beat_this) and drives ffmpeg; the
helper is a standalone script, so the model-backed part runs it as a
subprocess, like tests/test_producer_fit.py.

The pure parts are proved without any model: parse_order reads "1-4, 1-4, 9",
segments turns bar starts into times, and render is checked by FFT on a song
where bar k is a pure sine of 200 + 100k Hz -- so the first stretch of the
output must be bar 3 (500 Hz) and the last bar 1 (300 Hz).

The GREEN check is model-backed: a 100 BPM click track 40 s long with a louder
click every 4th beat, re-arranged as "1-2, 1-2"; the two pieces must be there,
equal in length, and the job must say how many bars the song has. beat_this
reads an erratic meter on this synthetic click track (the model's own bar gaps
are uneven, as they are in Fit's gate), so nothing here asserts a bar length;
what is asserted is that the pieces are exactly the bars the report names.

Run:
    python3 tests/test_producer_arrange.py                # pack + pure checks
    /path/to/producer-venv/bin/python tests/test_producer_arrange.py   # full gate
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

ARRANGE = os.path.join(ROOT, "engines", "producer_tools", "arrange.py")
FFMPEG = shutil.which("ffmpeg")
SR = 44100
TMPDIR = os.environ.get("TMPDIR") or None
WORK = tempfile.mkdtemp(prefix="bwf_arrange_", dir=TMPDIR)
OUT = os.path.join(WORK, "out")

HAVE_NP = importlib.util.find_spec("numpy") is not None

_spec = importlib.util.spec_from_file_location("bwf_producer_arrange_helper", ARRANGE)
_ar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ar)


def write_wav(path, a):
    """float array (N, 2) in [-1, 1] -> 16-bit stereo WAV at SR."""
    import numpy as np
    a16 = (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2")
    raw = a16.tobytes()
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(raw)) + b"WAVEfmt " +
                struct.pack("<IHHIIHH", 16, 1, 2, SR, SR * 4, 4, 16) +
                b"data" + struct.pack("<I", len(raw)) + raw)


# ---------------------------------------------------------------------------
# parse_order: pure, no model
# ---------------------------------------------------------------------------
print("parse_order: a typed order becomes 1-based inclusive pairs")
check('"1-4, 1-4, 9" on a 12-bar song -> [(1,4),(1,4),(9,9)]',
      _ar.parse_order("1-4, 1-4, 9", 12) == [(1, 4), (1, 4), (9, 9)],
      _ar.parse_order("1-4, 1-4, 9", 12))
check("spaces are allowed", _ar.parse_order(" 1 - 2 , 3 ", 12) == [(1, 2), (3, 3)],
      _ar.parse_order(" 1 - 2 , 3 ", 12))
check("a single bar is one item", _ar.parse_order("7", 12) == [(7, 7)], _ar.parse_order("7", 12))
check("the last bar may be named", _ar.parse_order("11-12", 12) == [(11, 12)], _ar.parse_order("11-12", 12))

REFUSALS = [
    ("an empty order", "   ", "Type the bars to play"),
    ("junk", "1-4, banana", "Type the bars to play"),
    ("a trailing comma", "1-4,", "Type the bars to play"),
    ("bar 0", "0", "numbered from 1"),
    ("a bar past the end", "13", "This song has 12 bars; bar 13 is past the end."),
    ("a range past the end", "3-20", "This song has 12 bars; bar 20 is past the end."),
    ("a range that runs backwards", "5-3", "A range runs forward: 5-3."),
    ("more than 200 items", ",".join(["1"] * 201), "keep the order to 200 or fewer"),
]
for label, text, want in REFUSALS:
    try:
        _ar.parse_order(text, 12)
        check("%s refuses" % label, False, "no error raised")
    except _ar.gc.GridError as e:
        msg = str(e)
        check("%s refuses with one sentence (%s)" % (label, want),
              "\n" not in msg and want in msg, repr(msg))
    except Exception as e:
        check("%s refuses with a GridError" % label, False, repr(e))
check("200 items are accepted", len(_ar.parse_order(",".join(["1"] * 200), 12)) == 200)

# ---------------------------------------------------------------------------
# segments: pure, no model
# ---------------------------------------------------------------------------
print("segments: each item plays from its bar start to the next bar start")
segs = _ar.segments([0, 2, 4, 6], 8.0, [(2, 3), (1, 1)])
check('bars [0,2,4,6] in an 8 s song, order [(2,3),(1,1)] -> [(2,6),(0,2)]',
      segs == [(2.0, 6.0), (0.0, 2.0)], segs)
last = _ar.segments([0, 2, 4, 6], 8.0, [(3, 4)])
check("the last bar of the song runs to the end of the song",
      last == [(4.0, 8.0)], last)
whole = _ar.segments([0, 2, 4, 6], 8.0, [(1, 4)])
check("the whole song in one item", whole == [(0.0, 8.0)], whole)

# ---------------------------------------------------------------------------
# render: pure numpy, no model
# ---------------------------------------------------------------------------
print("render: the pieces are cut sample-exactly and joined with a crossfade")
if not HAVE_NP:
    skip("render", "numpy not importable in this Python")
else:
    import numpy as np

    BAR_S, N_BARS = 2.0, 6
    t = np.arange(int(round(BAR_S * N_BARS * SR)), dtype="float64") / SR
    # bar k (1-based) is a pure sine of 100 + 100k Hz, amplitude 1.0
    freqs = [100.0 + 100.0 * (k + 1) for k in range(N_BARS)]
    mono = np.zeros_like(t)
    for k, f in enumerate(freqs):
        piece = slice(int(round(k * BAR_S * SR)), int(round((k + 1) * BAR_S * SR)))
        mono[piece] = np.sin(2.0 * math.pi * f * t[piece])
    song = np.stack([mono, mono], axis=1).astype("float32")

    fade_ms = 10
    fade_n = int(round(fade_ms * SR / 1000.0))
    out = _ar.render(song, SR, _ar.segments([BAR_S * k for k in range(N_BARS)], BAR_S * N_BARS,
                                            [(3, 3), (1, 1)]), fade_ms)
    expect_s = 2 * BAR_S - fade_ms / 1000.0
    check("the output is the two bars less one crossfade (%.4f s)" % expect_s,
          abs(len(out) / SR - expect_s) <= 1.0 / SR, len(out) / SR)
    check("the peak is never above 0.98", float(np.max(np.abs(out))) <= 0.98,
          float(np.max(np.abs(out))))

    def dominant(chunk):
        w = np.hanning(len(chunk))
        spec = np.abs(np.fft.rfft(chunk * w))
        f = np.fft.rfftfreq(len(chunk), 1.0 / SR)
        return float(f[int(np.argmax(spec))])

    x = out.mean(axis=1).astype("float64")
    first = x[int(0.02 * SR):int(1.5 * SR)]
    lastw = x[len(x) - int(1.5 * SR):len(x) - int(0.02 * SR)]
    check("the first piece is bar 3 (400 Hz), not bar 1 (200 Hz)",
          abs(dominant(first) - 400.0) <= 5.0 and abs(dominant(first) - 200.0) > 50.0, dominant(first))
    check("the last piece is bar 1 (200 Hz), not bar 3 (400 Hz)",
          abs(dominant(lastw) - 200.0) <= 5.0 and abs(dominant(lastw) - 400.0) > 50.0, dominant(lastw))

    # the join: a crossfade, not a cut. A hard cut would jump by up to a full
    # sample value between neighbours; a crossfade keeps the step within the
    # song's own slope (checked here as 2x its largest step).
    def max_step(x):
        return float(np.max(np.abs(np.diff(x)))) if len(x) > 1 else 0.0

    mono_all = song.mean(axis=1).astype("float64")
    cut = mono_all[:int(1.87 * SR)]
    i = int(0.934 * SR)
    j = i + int(0.002 * SR)          # a 2 ms gap, the tiniest splice there is
    hard = np.concatenate([mono_all[:i], mono_all[j:j + i]])
    check("a hard cut would jump (the baseline this check rules out)", max_step(hard) > 5 * max_step(cut),
          (max_step(hard), max_step(cut)))
    joined = _ar.render(song, SR, [(0.0, BAR_S), (BAR_S, 2 * BAR_S)], 20).mean(axis=1).astype("float64")
    check("the join is a crossfade, not a cut (step <= 2x the song's own)",
          max_step(joined) <= 2 * max_step(cut), (max_step(joined), max_step(cut)))

    quiet = (song * 0.2).astype("float32")
    out2 = _ar.render(quiet, SR, [(0.0, 2.0), (4.0, 6.0)], 5)
    # an equal-power join of two 0.2 sines can reach sqrt(2) x 0.2, never more
    check("a quieter song is not scaled up", float(np.max(np.abs(out2))) <= 0.2 * math.sqrt(2.0),
          float(np.max(np.abs(out2))))

# ---------------------------------------------------------------------------
# GREEN (model-backed): a click track re-arranged "1-2, 1-2"
# ---------------------------------------------------------------------------
HAVE_BT = importlib.util.find_spec("beat_this") is not None
if not (FFMPEG and HAVE_NP):
    print("green arrange: needs ffmpeg and numpy")
    if FFMPEG is None:
        skip("green arrange (end to end)", "ffmpeg not on PATH")
    else:
        skip("green arrange (end to end)", "numpy not importable in this Python")
elif not HAVE_BT:
    skip("green arrange (end to end)", "beat_this is not importable in this Python; run with the producer venv's python")
else:
    import numpy as np

    BPM, CLICK_LEN = 100.0, 40.0
    STEP = 60.0 / BPM
    n = int(round(CLICK_LEN * SR))
    click = np.zeros((n, 2), dtype="float32")
    beats = int(CLICK_LEN * BPM / 60.0)
    for i in range(beats):
        t0 = i * STEP
        j = int(round(t0 * SR))
        dur = 0.03 if i % 4 else 0.05
        amp = 0.35 if i % 4 else 0.9
        seg = np.arange(int(dur * SR)) / SR
        tick = amp * np.sin(2.0 * math.pi * 1000.0 * seg) * np.exp(-seg / (dur / 4))
        end = min(n, j + len(tick))
        click[j:end, :] += tick[:end - j][:, None].astype("float32")
    SONG = os.path.join(WORK, "click.wav")
    write_wav(SONG, click)

    print("green arrange: a 100 BPM click track, 40 s long, played as 1-2, 1-2")
    r = subprocess.run([sys.executable, ARRANGE, "--in", SONG, "--out", OUT, "--ffmpeg", FFMPEG,
                        "--order", "1-2, 1-2"], capture_output=True, text=True)
    check("arrange.py ran to exit 0", r.returncode == 0,
          "rc=%s\nstdout=%s\nstderr=%s" % (r.returncode, r.stdout[-800:], r.stderr[-500:]))
    for name in ("arranged.mp3", "arranged.wav", "arrange.json"):
        p = os.path.join(OUT, name)
        check("%s exists and is non-empty" % name, os.path.isfile(p) and os.path.getsize(p) > 0, p)
    out_lines = [l for l in r.stdout.splitlines() if l.strip()]
    check("last stdout line starts with 're-arranged '",
          bool(out_lines) and out_lines[-1].startswith("re-arranged "), repr(out_lines[-3:]))
    for k in range(1, 5):
        check("PROGRESS %d/4 was printed" % k, "PROGRESS %d/4" % k in out_lines, r.stdout[-400:])

    doc = {}
    try:
        doc = json.load(open(os.path.join(OUT, "arrange.json")))
    except Exception as e:
        check("arrange.json parses", False, repr(e))
    check("arrange.json says the song has at least 8 bars", int(doc.get("bars", 0)) >= 8, doc.get("bars"))
    segs_json = doc.get("segments") or []
    check("arrange.json has 2 segments", len(segs_json) == 2, segs_json)
    if len(segs_json) == 2:
        lens = [float(s["end_s"]) - float(s["start_s"]) for s in segs_json]
        print("        segments: %s (%.3f s, %.3f s)" % ([s.get("bars") for s in segs_json], lens[0], lens[1]))
        check("the two pieces are within 20 ms of each other", abs(lens[0] - lens[1]) <= 0.02, lens)
        bar_s = [float(b) for b in (doc.get("bar_starts") or [])]
        check("the segments are exactly the bars they name (bar 1 up to bar 3's start)",
              len(bar_s) > 2 and abs(float(segs_json[0]["start_s"]) - bar_s[0]) <= 0.01
              and abs(float(segs_json[0]["end_s"]) - bar_s[2]) <= 0.01,
              (segs_json[0], bar_s[:3]))
        check("arrange.json seconds is the length of the output",
              abs(float(doc.get("seconds", 0.0)) - (lens[0] + lens[1] - 0.01)) <= 0.02,
              doc.get("seconds"))

    # refusals on the command line: exit 2, one plain sentence last, no traceback
    for label, extra, want in (("an empty order", ["--order", " "], "Type the bars to play"),
                               ("a bar past the end", ["--order", "1-999"], "past the end"),
                               ("a range that runs backwards", ["--order", "4-2"], "runs forward"),
                               ("a join fade of 99 ms", ["--order", "1-2", "--fade-ms", "99"], "join fade")):
        rr = subprocess.run([sys.executable, ARRANGE, "--in", SONG, "--out", os.path.join(WORK, "out_bad"),
                             "--ffmpeg", FFMPEG] + extra, capture_output=True, text=True)
        said = [l for l in rr.stdout.splitlines() if l.strip()]
        check("%s -> exit 2, one plain sentence last" % label,
              rr.returncode == 2 and bool(said) and want in said[-1]
              and "Traceback" not in rr.stderr,
              "rc=%s stdout=%r stderr=%s" % (rr.returncode, rr.stdout[-300:], rr.stderr[-200:]))

# ---------------------------------------------------------------------------
# Pack: mode, room, note, fields, plan
# ---------------------------------------------------------------------------
print("pack: the arrange mode is listed, in the producer room, with its plan")
rooms = {r["id"]: r for r in engines.rooms()}
producer_modes = [m["mode"] for m in (rooms.get("producer", {}).get("modes") or [])]
check("the producer room holds grid, fit, arrange, mix in that order",
      producer_modes == ["grid", "fit", "arrange", "mix"], producer_modes)
check("mode word and room",
      (engines.mode_words("producer") or {}).get("arrange") == "Re-arrange by bars"
      and engines.mode_room("producer", "arrange") == "producer")
note = engines.mode_note("producer", "arrange") or ""
check("mode note names the order and the processor",
      "1-4, 1-4, 9-16" in note and "processor" in note, note)

fids = [f["id"] for f in engines.fields("producer", "arrange")]
check("field ids in the spec order",
      fids == ["source_audio_name", "order", "beats_per_bar", "first_downbeat", "fade_ms"], fids)
by_id = {f["id"]: f for f in engines.fields("producer", "arrange")}
order_f = by_id.get("order", {})
song_f = by_id.get("source_audio_name", {})
check("the song is a primary audio field, the order a primary text field",
      song_f.get("type") == "audio" and song_f.get("tier") == "primary"
      and song_f.get("label") == "Song to re-arrange"
      and order_f.get("type") == "text" and order_f.get("tier") == "primary"
      and order_f.get("label") == "Bars to play, in order", order_f)
check("the order hint shows an example", "1-4, 1-4, 9-16" in order_f.get("hint", ""), order_f.get("hint"))
grid_by_id = {f["id"]: f for f in engines.fields("producer", "grid")}
for k in ("beats_per_bar", "first_downbeat"):
    check("%s is the grid mode's field, unchanged" % k, by_id.get(k) == grid_by_id[k],
          (by_id.get(k), grid_by_id[k]))
fade = by_id.get("fade_ms", {})
check("fade_ms: number, default 10, range [2, 50], units ms, advanced",
      fade.get("type") == "number" and fade.get("default") == 10 and fade.get("range") == [2, 50]
      and fade.get("units") == "ms" and fade.get("tier") == "advanced" and fade.get("label") == "Join fade", fade)
check("a default preset, a standard quality tier and one example",
      [p["id"] for p in engines.presets("producer", "arrange")] == ["default"]
      and [q["id"] for q in engines.quality("producer", "arrange")] == ["standard"]
      and [e["id"] for e in engines.examples("producer", "arrange")] == ["arrange-try"],
      (engines.presets("producer", "arrange"), engines.quality("producer", "arrange")))
ex = (engines.examples("producer", "arrange") or [{}])[0]
check("the example plays the first four bars twice, and says why",
      ex.get("values", {}).get("order") == "1-4, 1-4" and ex.get("needs") == "sound" and bool(ex.get("why")), ex)

_spec = importlib.util.spec_from_file_location("bwf_producer_arrange_pack",
                                               os.path.join(ROOT, "engines", "producer.py"))
prod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prod)
if not hasattr(prod, "arrange_plan"):
    check("engines/producer.py declares arrange_plan", False, "no arrange_plan in the pack")
    prod.arrange_plan = lambda a, m: {}
try:
    plan = prod.arrange_plan({"source_audio_name": "s.wav", "order": "1-4, 1-4", "fade_ms": 20}, {})
    argv = plan["steps"][0]["argv"]
    check("the outputs are the three names, the playable one first",
          list(plan.get("outputs", [])) == ["arranged.mp3", "arranged.wav", "arrange.json"],
          plan.get("outputs"))
    check("the plan runs arrange.py under the producer python",
          argv[0] == "{bin:producer_python}" and argv[1].endswith("producer_tools/arrange.py"), argv)
    check("the typed order is passed as one --order argument",
          "--order" in argv and argv[argv.index("--order") + 1] == "1-4, 1-4", argv)
    check("the join fade is passed as --fade-ms 20",
          "--fade-ms" in argv and argv[argv.index("--fade-ms") + 1] == "20", argv)
    check("the song and the out dir are passed",
          "{in:source_audio_name}" in argv and "{job}" in argv, argv)
    check("no meter is passed when the field is auto",
          "--beats-per-bar" not in argv and "--first-downbeat" not in argv, argv)
    check("the plan keeps PROGRESS and the summary line",
          plan.get("progress") == r"PROGRESS (\d+)/(\d+)" and plan.get("summary") is True, plan)
except Exception as e:
    check("arrange_plan(source, order, fade) plans the re-arrange", False, repr(e))
try:
    argv = prod.arrange_plan({"source_audio_name": "s.wav", "order": "1-2",
                              "beats_per_bar": 4, "first_downbeat": 1.5}, {})["steps"][0]["argv"]
    check("a known meter and a bar start are passed on the command line",
          "--beats-per-bar" in argv and argv[argv.index("--beats-per-bar") + 1] == "4"
          and "--first-downbeat" in argv and argv[argv.index("--first-downbeat") + 1] == "1.5", argv)
except Exception as e:
    check("arrange_plan passes --beats-per-bar and --first-downbeat", False, repr(e))
for label, args_ in (("an empty order", {"source_audio_name": "s.wav", "order": "  "}),
                     ("no order at all", {"source_audio_name": "s.wav"}),
                     ("a join fade of 99", {"source_audio_name": "s.wav", "order": "1-2", "fade_ms": 99}),
                     ("a join fade of 0", {"source_audio_name": "s.wav", "order": "1-2", "fade_ms": 0})):
    try:
        prod.arrange_plan(args_, {})
        check("arrange_plan refuses %s" % label, False, "no error raised")
    except ValueError as e:
        check("arrange_plan refuses %s with one sentence" % label,
              "\n" not in str(e) and str(e).strip() != "", repr(e))
    except Exception as e:
        check("arrange_plan refuses %s with a ValueError" % label, False, repr(e))
try:
    prod.arrange_plan({"order": "1-2", "beats_per_bar": 9}, {})
    check("arrange_plan refuses beats_per_bar 9", False, "no error raised")
except ValueError as e:
    check("arrange_plan refuses beats_per_bar 9 with one sentence",
          "\n" not in str(e) and str(e).strip() != "", repr(e))

try:
    g = engines.graph_for("producer", "arrange", {"source_audio_name": "s.wav", "order": "1-2"}, {})
    check("engines.graph_for('producer', 'arrange') plans the re-arrange",
          list(g.get("outputs", []))[:1] == ["arranged.mp3"], g.get("outputs"))
except Exception as e:
    check("engines.graph_for('producer', 'arrange') plans the re-arrange", False, repr(e))

shutil.rmtree(WORK, ignore_errors=True)
print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("all arrange checks passed")
