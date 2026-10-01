#!/usr/bin/env python3
"""Mix (Producer room): the helper script the producer pack's "mix" mode runs.

Takes one to four audio files, lays them over each other -- a per-track gain
(dB) and start offset (seconds) -- then two-pass loudness-normalises the
result. It writes mix.wav, mix.mp3 and mix.json (what was mixed and what was
measured) into --out, prints one PROGRESS line per stage, and ends with one
summary line. It drives ffmpeg as a subprocess (its path is passed in with
--ffmpeg); stdlib only.

The filter graph: per input, resample to 48 kHz stereo (aformat), apply the
gain (volume), then delay the start (adelay); the tracks are joined with
amix (no normalisation, as long as the longest track). Loudness is a two-pass
loudnorm: pass 1 measures (print_format=json) and pass 2 applies the measured
numbers (linear=true). The level in mix.json is then measured for real with
ebur128 -- what came out, not the target.

A bad argument is one plain sentence and exit code 2, never a traceback.
"""
import argparse
import json
import os
import re
import subprocess
import sys

GAIN_RANGE = (-24.0, 12.0)
OFFSET_RANGE = (0.0, 600.0)
LUFS_RANGE = (-30.0, -6.0)
TOTAL_STEPS = 3


def fail(msg):
    print(msg)
    sys.exit(2)


def finite(x):
    return not (x != x or x == float("inf") or x == float("-inf"))


def one_line(text):
    return " ".join(str(text).split())[:300]


def per_track(values, what, lo, hi, n):
    """One value per track (a track that was not named takes 0)."""
    if values is None:
        return [0.0] * n
    if len(values) != n:
        fail("Every track needs a %s; got %d for %d tracks." % (what, len(values), n))
    for v in values:
        if not finite(v) or not lo <= v <= hi:
            fail("Every %s must be between %g and %g; got %g." % (what, lo, hi, v))
    return values


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ffmpeg", required=True)
    ap.add_argument("--track", action="append", required=True)
    ap.add_argument("--gain", action="append", type=float)
    ap.add_argument("--offset", action="append", type=float)
    ap.add_argument("--lufs", required=True, type=float)
    args = ap.parse_args()

    n = len(args.track)
    if not 1 <= n <= 4:
        fail("Mix takes between one and four tracks; %d were given." % n)
    for t in args.track:
        if not os.path.isfile(t):
            fail("Track file not found: %s" % os.path.basename(t))
    gains = per_track(args.gain, "gain", GAIN_RANGE[0], GAIN_RANGE[1], n)
    offsets = per_track(args.offset, "start offset", OFFSET_RANGE[0], OFFSET_RANGE[1], n)
    lufs = args.lufs
    if not finite(lufs) or not LUFS_RANGE[0] <= lufs <= LUFS_RANGE[1]:
        fail("The loudness target must be between %g and %g LUFS; got %g." % (LUFS_RANGE[0], LUFS_RANGE[1], lufs))
    if not os.path.isdir(args.out):
        os.makedirs(args.out)
    wav = os.path.join(args.out, "mix.wav")
    mp3 = os.path.join(args.out, "mix.mp3")
    report = os.path.join(args.out, "mix.json")

    def run(*a):
        return subprocess.run([args.ffmpeg, "-hide_banner", "-nostdin", "-y"] + list(a),
                              capture_output=True, text=True)

    # The shared graph: one branch per track, then the amix.
    per = []
    labels = []
    for i in range(n):
        ms = int(round(offsets[i] * 1000.0))
        labels.append("a%d" % i)
        per.append("[%d:a]aformat=sample_rates=48000:channel_layouts=stereo"
                   ",volume=%gdB,adelay=%d:all=1[%s]" % (i, gains[i], ms, labels[i]))
    graph = ";".join(per) + ";" + "".join("[%s]" % l for l in labels) \
        + "amix=inputs=%d:normalize=0:duration=longest[m]" % n
    inputs = []
    for t in args.track:
        inputs += ["-i", t]

    print("PROGRESS 1/%d" % TOTAL_STEPS, flush=True)
    r = run(*inputs, "-filter_complex",
            graph + ";[m]loudnorm=I=%g:TP=-1:LRA=11:print_format=json[m2]" % lufs,
            "-map", "[m2]", "-f", "null", "-")
    if r.returncode != 0:
        fail("The mix could not be built: %s" % one_line(r.stderr))
    m = re.search(r"\{.*\}", r.stderr, re.S)
    if not m:
        fail("The first loudness pass measured nothing: %s" % one_line(r.stderr))
    m1 = json.loads(m.group(0))

    print("PROGRESS 2/%d" % TOTAL_STEPS, flush=True)
    r = run(*inputs, "-filter_complex",
            graph + ";[m]loudnorm=I=%g:TP=-1:LRA=11:linear=true:measured_I=%s:measured_TP=%s:"
            "measured_LRA=%s:measured_thresh=%s:offset=%s[m2]" % (
                lufs, m1["input_i"], m1["input_tp"], m1["input_lra"],
                m1.get("input_thresh") or m1.get("input_ths") or "0",
                m1.get("target_offset") or "0"),
            "-map", "[m2]", "-c:a", "pcm_s16le", "-ar", "48000", wav)
    if r.returncode != 0:
        fail("The levelled mix could not be written: %s" % one_line(r.stderr))

    r = run("-i", wav, "-c:a", "libmp3lame", "-b:a", "320k", mp3)
    if r.returncode != 0:
        fail("The MP3 could not be written: %s" % one_line(r.stderr))

    print("PROGRESS 3/%d" % TOTAL_STEPS, flush=True)
    r = run("-i", wav, "-f", "null", "-")
    d = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not d:
        fail("The finished mix has no readable duration: %s" % one_line(r.stderr))
    duration = float(d.group(1)) * 3600.0 + float(d.group(2)) * 60.0 + float(d.group(3))

    r = run("-i", wav, "-af", "ebur128=peak=true", "-f", "null", "-")
    i_vals = re.findall(r"I:\s*(-?\d+\.?\d*)\s*LUFS", r.stderr)
    p_vals = re.findall(r"Peak:\s*(-?\d+\.?\d*)\s*dBFS", r.stderr)
    if not i_vals or not p_vals:
        fail("The finished mix could not be measured: %s" % one_line(r.stderr))
    measured = float(i_vals[-1])
    peak = float(p_vals[-1])

    doc = {
        "tracks": [{"file": os.path.basename(t), "gain_db": gains[i], "offset_s": offsets[i]}
                   for i, t in enumerate(args.track)],
        "target_lufs": lufs,
        "measured_lufs": measured,
        "true_peak_dbtp": peak,
        "duration_s": duration,
    }
    with open(report, "w") as f:
        json.dump(doc, f, indent=2)

    minutes = int(duration) // 60
    seconds = int(duration) % 60
    print("mixed %d %s, %.1f LUFS, true peak %.1f dBTP, %d:%02d"
          % (n, "track" if n == 1 else "tracks", measured, peak, minutes, seconds), flush=True)


if __name__ == "__main__":
    main()
