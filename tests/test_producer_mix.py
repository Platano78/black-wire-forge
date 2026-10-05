"""Producer room "Mix" gate.

Mix takes up to four uploaded audio files, lays them over each other with a
per-track gain (dB) and start offset (seconds), then two-pass loudness-
normalises the result. It runs on the system python3 and ffmpeg only -- no
beat_this, no ComfyUI. mix.py drives ffmpeg as a subprocess. Checks:
end-to-end mix of two synthetic tones (duration, loudness, true peak), the
declared offset and gain actually landing where they were declared, the
refusal cases, and the pack's own "mix" field list and plan.

Run: python3 tests/test_producer_mix.py
"""
from array import array
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


import engines  # noqa: E402

MIX = os.path.join(ROOT, "engines", "producer_tools", "mix.py")
FFMPEG = shutil.which("ffmpeg")
if FFMPEG is None:
    print("SKIP: ffmpeg not on PATH (the Mix gate needs it)")
    sys.exit(0)

SR = 48000
WORK = tempfile.mkdtemp(prefix="bwf_mix_")
A = os.path.join(WORK, "a.wav")
B = os.path.join(WORK, "b.wav")
OUT = os.path.join(WORK, "out")

# A: a 440 Hz sine, 6 s; B: an 880 Hz sine, 4 s; both at -20 dB.
for path, freq, dur in ((A, 440, 6), (B, 880, 4)):
    r = subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y",
                        "-f", "lavfi", "-i", "sine=frequency=%d:duration=%d" % (freq, dur),
                        "-af", "volume=-20dB", "-ar", "48000", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("  (could not make a %d Hz tone: %s)" % (freq, r.stderr[-300:]))

print("end-to-end: two tones, B -6 dB and 2 s late, levelled to -14 LUFS")
r = subprocess.run([sys.executable, MIX,
                    "--out", OUT, "--ffmpeg", FFMPEG,
                    "--track", A, "--track", B,
                    "--gain", "0", "--gain", "-6",
                    "--offset", "0", "--offset", "2",
                    "--lufs", "-14"], capture_output=True, text=True)
check("mix.py ran to exit 0", r.returncode == 0,
      "rc=%s\nstdout=%s\nstderr=%s" % (r.returncode, r.stdout[-500:], r.stderr[-500:]))
for name in ("mix.wav", "mix.mp3", "mix.json"):
    p = os.path.join(OUT, name)
    check("%s exists and is non-empty" % name, os.path.isfile(p) and os.path.getsize(p) > 0, p)
lines = [l for l in r.stdout.splitlines() if l.strip()]
check("last stdout line starts with 'mixed 2 tracks'",
      bool(lines) and lines[-1].startswith("mixed 2 tracks"), r.stdout[-500:])

doc = {}
try:
    doc = json.load(open(os.path.join(OUT, "mix.json")))
except Exception as e:
    check("mix.json parses", False, repr(e))
check("mix.json measured_lufs within 1.0 of -14",
      abs(float(doc.get("measured_lufs", 1e9)) - (-14.0)) <= 1.0, doc)
check("mix.json true peak under -0.5 dBTP",
      float(doc.get("true_peak_dbtp", 1e9)) <= -0.5, doc)
check("mix.json duration within 0.1 s of 6",
      abs(float(doc.get("duration_s", 1e9)) - 6.0) <= 0.1, doc)

print("offset and gain landed where declared")
raw = subprocess.run([FFMPEG, "-hide_banner", "-v", "error",
                      "-i", os.path.join(OUT, "mix.wav"),
                      "-f", "s16le", "-ac", "1", "-ar", "48000", "-"],
                     capture_output=True).stdout
samples = array("h")
samples.frombytes(raw)

def goertzel(window, freq):
    """The squared power of one frequency in one window of s16 samples
    (the window is exactly a whole number of its periods, so the bin is exact)."""
    c = math.cos(2.0 * math.pi * freq / SR)   # per-sample angle
    s1 = s2 = 0.0
    for x in window:
        s0 = x + 2.0 * c * s1 - s2
        s2 = s1
        s1 = s0
    return s1 * s1 + s2 * s2 - 2.0 * c * s1 * s2


WIN = 0.05
n = int(WIN * SR)
windows = []
for i0 in range(0, len(samples) - n, n):
    w = samples[i0:i0 + n]
    windows.append((goertzel(w, 440.0), goertzel(w, 880.0)))
check("the tone windows were analysed", len(windows) >= 100, len(windows))
max880 = max((p[1] for p in windows), default=0)
first880 = None
for i, p in enumerate(windows):
    if p[1] > 0.25 * max880:
        first880 = i * WIN
        break
check("the 880 Hz tone starts within 0.06 s of 2.0 s",
      first880 is not None and abs(first880 - 2.0) <= 0.06, first880)
seg = [p for i, p in enumerate(windows) if 2.5 <= i * WIN < 3.5]
sum440 = sum(p[0] for p in seg)
sum880 = sum(p[1] for p in seg)
ratio_db = 10.0 * math.log10(sum880 / sum440) if sum440 > 0 else float("nan")
check("880 vs 440 level is -6 dB (within 1.0) over 2.5-3.5 s",
      abs(ratio_db - (-6.0)) <= 1.0, ratio_db)

print("gains and offsets are given in track order; missing ones default to 0")
OUT2 = os.path.join(WORK, "out2")
r = subprocess.run([sys.executable, MIX,
                    "--out", OUT2, "--ffmpeg", FFMPEG,
                    "--track", A, "--track", B,
                    "--gain", "3",
                    "--lufs", "-14"], capture_output=True, text=True)
check("2 tracks with 1 gain -> exit 0", r.returncode == 0,
      "rc=%s stdout=%s stderr=%s" % (r.returncode, r.stdout[-300:], r.stderr[-300:]))
doc2 = {}
try:
    doc2 = json.load(open(os.path.join(OUT2, "mix.json")))
except Exception as e:
    check("out2/mix.json parses", False, repr(e))
check("mix.json shows the missing gain as 0 (gains [3, 0])",
      [t.get("gain_db") for t in doc2.get("tracks", [])] == [3.0, 0.0], doc2)
check("mix.json shows both offsets defaulted to 0",
      [t.get("offset_s") for t in doc2.get("tracks", [])] == [0.0, 0.0], doc2)

print("a track whose tags hold JSON (a BWF song's prompt graph) still mixes")
# BWF's own audio files carry the ComfyUI graph in a "prompt" tag, so ffmpeg
# echoes brace-laden metadata into stderr before loudnorm's own flat JSON.
C = os.path.join(WORK, "c.mp3")
GRAPH = '{"104": {"inputs": {"a": 1}}, "105": {"b": "x\ty"}}'
r = subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y", "-i", A,
                    "-c:a", "libmp3lame", "-b:a", "128k",
                    "-metadata", "prompt=" + GRAPH, C],
                   capture_output=True, text=True)
check("made a track carrying a JSON-shaped prompt tag", os.path.isfile(C) and os.path.getsize(C) > 0,
      r.stderr[-300:])
OUT3 = os.path.join(WORK, "out3")
r = subprocess.run([sys.executable, MIX,
                    "--out", OUT3, "--ffmpeg", FFMPEG,
                    "--track", C, "--track", B,
                    "--gain", "0", "--gain", "-6",
                    "--lufs", "-14"], capture_output=True, text=True)
check("mixing two tracks whose tags hold JSON -> exit 0", r.returncode == 0,
      "rc=%s\nstdout=%s\nstderr=%s" % (r.returncode, r.stdout[-500:], r.stderr[-500:]))
check("...with no traceback in stderr", "Traceback" not in r.stderr, r.stderr[-500:])
check("...and the output written", os.path.isfile(os.path.join(OUT3, "mix.wav")))

print("refusals: one sentence, exit 2, no traceback")


def refuse(label, argv):
    rr = subprocess.run([sys.executable, MIX, "--out", OUT, "--ffmpeg", FFMPEG] + argv,
                        capture_output=True, text=True)
    out_lines = [l for l in rr.stdout.splitlines() if l.strip()]
    check("%s -> exit 2" % label, rr.returncode == 2,
          "rc=%s stderr=%s" % (rr.returncode, rr.stderr[-300:]))
    check("%s -> exactly one line of stdout" % label, len(out_lines) == 1, repr(rr.stdout))
    check("%s -> no traceback in stderr" % label, "Traceback" not in rr.stderr, rr.stderr[-300:])


refuse("a 5th track", ["--track", A, "--track", B, "--track", A, "--track", B,
                        "--track", A, "--lufs", "-14"])
refuse("3 gains for 2 tracks", ["--track", A, "--track", B, "--gain", "0", "--gain", "0",
                                 "--gain", "0", "--lufs", "-14"])
refuse("3 offsets for 2 tracks", ["--track", A, "--track", B, "--offset", "0", "--offset", "0",
                                   "--offset", "1", "--lufs", "-14"])
refuse("gain 20 dB", ["--track", A, "--track", B, "--gain", "20", "--gain", "0",
                       "--offset", "0", "--offset", "2", "--lufs", "-14"])
refuse("offset -1 s", ["--track", A, "--track", B, "--gain", "0", "--gain", "0",
                        "--offset", "-1", "--offset", "2", "--lufs", "-14"])
refuse("lufs inf", ["--track", A, "--lufs", "inf"])

print("pack: the mix field list and plan")
fids = [f["id"] for f in engines.fields("producer", "mix")]
check("fields list track_1..4, gain_1..4, offset_1..4, lufs",
      fids == ["track_1", "track_2", "track_3", "track_4",
               "gain_1", "gain_2", "gain_3", "gain_4",
               "offset_1", "offset_2", "offset_3", "offset_4", "lufs"], fids)

import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("bwf_producer_mix_gate",
                                              os.path.join(ROOT, "engines", "producer.py"))
prod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prod)
try:
    plan = prod.mix_plan({"track_1": "a.wav"}, {})
    check("mix_plan outputs start with mix.mp3",
          list(plan.get("outputs", []))[:1] == ["mix.mp3"], plan.get("outputs"))
except Exception as e:
    check("mix_plan({'track_1': 'a.wav'}, {}) plans the mix", False, repr(e))
try:
    prod.mix_plan({"track_1": "a.wav", "gain_1": "inf"}, {})
    check("mix_plan refuses gain_1 'inf'", False, "no error raised")
except ValueError as e:
    check("mix_plan refuses gain_1 'inf' with one sentence",
          "\n" not in str(e) and e.args[0].strip() != "", str(e))
except Exception as e:
    check("mix_plan refuses gain_1 'inf' with a ValueError", False, repr(e))
try:
    g = engines.graph_for("producer", "mix", {"track_1": "a.wav"}, {})
    check("engines.graph_for('producer', 'mix') plans the mix",
          list(g.get("outputs", []))[:1] == ["mix.mp3"], g.get("outputs"))
except Exception as e:
    check("engines.graph_for('producer', 'mix') plans the mix", False, repr(e))

shutil.rmtree(WORK, ignore_errors=True)
print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("all mix checks passed")
