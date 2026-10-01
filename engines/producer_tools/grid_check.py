"""Producer room, P1 "Grid check": the helper script the producer pack runs.

Runs under the PYTHON THE USER POINTED BWF AT (the one with beat_this and,
optionally, Demucs installed), never under the server's own Python. One job:

  1. decode the source to 44.1 kHz stereo with ffmpeg,
  2. optionally split off the drum stem with Demucs (htdemucs) and track that,
  3. beat_this places every beat and downbeat (per-beat times: no constant
     period is fitted anywhere). With --beats-per-bar N the model's own bar
     starts are replaced by the bar-start phase its downbeat logits favour
     for that meter (see phase_downbeats),
  4. pure arithmetic on those times: meter per bar, tempo per bar, drift,
  5. write grid.json and grid-check.mp3 (the source with a click on every
     beat and a higher, louder click on every downbeat).

Every failure ends as ONE plain sentence on stdout and exit code 2; the
traceback, when there is one, goes to grid-error.log in the job folder.
`--check` only asks whether the tools are installed.

The analysis functions below are stdlib-only; numpy, torch, beat_this and
demucs are imported lazily so `--check` works on a Python that has none.
"""
import argparse
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import traceback

SR = 44100
BEAT_THIS_CMD = ("pip install torch --index-url https://download.pytorch.org/whl/cpu, "
                 "then pip install git+https://github.com/CPJKU/beat_this")
_MIN_BEATS = 8
# A bar-start phase chosen from the model's downbeat logits is trusted only when
# the best phase beats the runner-up by at least this much mean logit. Observed
# 2026-09-30 (final0): margin about 18-20 when the phase was right (musical 4/4),
# 0.4-0.7 on 5/4, where it picked the wrong phase. 2.0 is the orchestrator's
# ruling, far from both.
PHASE_MARGIN_TRUSTED = 2.0
# How far a typed bar start may sit from the nearest detected beat, in seconds.
ANCHOR_SNAP_S = 0.15


class GridError(Exception):
    """A failure whose message is already the one plain sentence."""


# ── tool presence ───────────────────────────────────────────────────────────
def missing_tool_sentence(drum_stem):
    """One sentence naming what to install and where to point BWF, or None."""
    py = sys.executable or "python"
    if importlib.util.find_spec("beat_this") is None:
        return ("Grid check needs beat_this, and the Python BWF is pointed at (%s) does not have it: "
                "install it there (%s), or point BWF at a Python that has it with the "
                "BWF_PRODUCER_PYTHON setting." % (py, BEAT_THIS_CMD))
    if drum_stem and importlib.util.find_spec("demucs") is None:
        return ("Using the drum stem needs Demucs, and the Python BWF is pointed at (%s) does not have it: "
                "install it there (pip install demucs), or untick the drum stem." % py)
    return None


# ── pure analysis (stdlib) ──────────────────────────────────────────────────
def phase_downbeats(beats, logits, n):
    """Bar starts for a user-given meter of n beats per bar.

    `logits` is the model's downbeat logit sampled at each detected beat. The
    bar starts are every n-th beat; the phase k in 0..n-1 is the one whose beats
    have the highest mean logit. The beats themselves are untouched (the model's
    per-beat times), and nothing is fitted to a period. Returns
    (downbeats, phase, margin, phase_means): margin is best minus second-best
    mean logit, how clearly one phase beat the rest."""
    if len(beats) != len(logits) or len(beats) < 2 * n:
        raise GridError("Only %d beats were found, too few to place bars of %d beats." % (len(beats), n))
    means = [sum(logits[k::n]) / len(logits[k::n]) for k in range(n)]
    order = sorted(range(n), key=lambda k: -means[k])
    k = order[0]
    return list(beats[k::n]), k, means[k] - means[order[1]], means


def anchor_downbeats(beats, anchor, n):
    """Bar starts from one time the user heard a bar begin. The anchor snaps to
    the nearest detected beat j; the phase is j mod n and every beat with index
    congruent to it (mod n) starts a bar. Only the model's per-beat times are
    used, so no constant period is involved. Returns
    (downbeats, phase, snapped_time)."""
    if len(beats) < 2 * n:
        raise GridError("Only %d beats were found, too few to place bars of %d beats." % (len(beats), n))
    j = min(range(len(beats)), key=lambda i: abs(beats[i] - anchor))
    if abs(beats[j] - anchor) > ANCHOR_SNAP_S:
        raise GridError("No beat was found within %.2f s of %.2f s (the nearest is at %.2f s), so 'A bar starts at' "
                        "cannot be placed: play the grid check and type the time of a beat you hear."
                        % (ANCHOR_SNAP_S, anchor, beats[j]))
    k = j % n
    return list(beats[k::n]), k, beats[j]


def analyse(beats, downbeats, user_beats_per_bar=None, anchor=None):
    """Per-beat times in, the whole grid report out. Nothing here fits one
    period: tempo is read off each bar and each inter-beat gap."""
    beats = sorted(float(b) for b in beats)
    downbeats = sorted(float(d) for d in downbeats)
    if len(beats) < _MIN_BEATS:
        raise GridError("Only %d beats were found, too few to read a tempo or bars from; "
                        "this may not be a rhythmic recording, or it is very short." % len(beats))
    gaps = [b - a for a, b in zip(beats, beats[1:])]
    # Local tempo over a sliding 8-beat window. beat_this places beats on a
    # 20 ms frame grid, so one gap alone is good to only a few percent; eight
    # beats average that down under half a percent, and it is still local:
    # the window moves, no single period is fitted to the song.
    w = min(8, len(beats) - 1)
    local_bpm = [60.0 * w / (beats[i + w] - beats[i]) for i in range(len(beats) - w) if beats[i + w] > beats[i]]
    bpm_median = statistics.median(local_bpm)

    q = max(1, len(local_bpm) // 4)
    first_q = statistics.median(local_bpm[:q])
    last_q = statistics.median(local_bpm[-q:])
    drift_pct = (last_q - first_q) / first_q * 100.0

    bars = []
    # A bar runs from one downbeat up to (not including) the next; its beat
    # count is the meter. Half a gap of slack keeps a downbeat that the
    # tracker placed a hair off its own beat from being counted in two bars.
    # Each bar's bpm is the same sliding-window tempo, centred on that bar, so
    # the tempo curve stays sound even where the downbeat head is wrong.
    eps = 0.5 * statistics.median(gaps)
    for start, end in zip(downbeats, downbeats[1:]):
        idx = [i for i, b in enumerate(beats) if start - eps <= b < end - eps]
        bpm = None
        if idx:
            j = min(max(idx[0] + len(idx) // 2 - w // 2, 0), len(beats) - w - 1)
            if beats[j + w] > beats[j]:
                bpm = round(60.0 * w / (beats[j + w] - beats[j]), 3)
        bars.append({"start": round(start, 4), "beats": len(idx), "bpm": bpm})
    pickup = sum(1 for b in beats if downbeats and b < downbeats[0] - eps)
    tail = (sum(1 for b in beats if b >= downbeats[-1] - eps)
            if downbeats else 0)

    counts = {}
    for bar in bars:
        counts[str(bar["beats"])] = counts.get(str(bar["beats"]), 0) + 1
    if not bars:
        meter = {"beats_per_bar": None, "label": "meter unknown (fewer than two downbeats found)",
                 "bars": 0, "counts": {}, "consistency": None}
    else:
        top = max(counts, key=lambda k: counts[k])
        share = counts[top] / len(bars)
        if top == "1" and share >= 0.5:
            label = "meter unclear (almost every beat was marked as a bar start)"
        elif share >= 0.9:
            label = "%s/4" % top
            if user_beats_per_bar:
                label += (" (you set %d, bars from %.2f s)" % (user_beats_per_bar, anchor) if anchor is not None
                          else " (you set %d)" % user_beats_per_bar)
        else:
            label = "mixed (%s)" % ", ".join(
                "%s beats x %d" % (k, counts[k]) for k in sorted(counts, key=lambda k: -counts[k]))
        meter = {"beats_per_bar": int(top), "label": label, "bars": len(bars),
                 "counts": counts, "consistency": round(share, 3),
                 "anchor": round(anchor, 4) if anchor is not None else None,
                 "source": ("anchor" if anchor is not None else "phase-logit" if user_beats_per_bar else "model"),
                 "note": "counts beats between downbeats; the /4 assumes a quarter-note beat"}
    return {"beats": [round(b, 4) for b in beats],
            "downbeats": [round(d, 4) for d in downbeats],
            "bars": bars, "pickup_beats": pickup, "tail_beats": tail,
            "meter": meter,
            "bpm_median": round(bpm_median, 3), "bpm_window_beats": w,
            "drift_pct": round(drift_pct, 3),
            "drift_detail": {"first_quarter_bpm": round(first_q, 3), "last_quarter_bpm": round(last_q, 3)}}


def summary_line(report, expected_bpm=None):
    asked = " (asked %g)" % expected_bpm if expected_bpm else ""
    line = "%s, %.1f BPM%s, drift %+.1f%%" % (
        report["meter"]["label"], report["bpm_median"], asked, report["drift_pct"])
    m = report["meter"]
    if m.get("source") == "phase-logit" and m.get("margin") is not None and m["margin"] < PHASE_MARGIN_TRUSTED:
        line += "; where bars start is a guess \u2014 set 'A bar starts at' to fix it"
    return line


# ── audio I/O (ffmpeg + numpy) ──────────────────────────────────────────────
def decode(ffmpeg, path):
    import numpy as np
    r = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise GridError("ffmpeg could not read that audio file (%s)."
                        % (r.stderr.decode(errors="replace").strip().splitlines() or ["no detail"])[-1])
    a = np.frombuffer(r.stdout, dtype="<f4").reshape(-1, 2).astype("float32")
    return a


def click_track(n_samples, beats, downbeats):
    """Mono click layer: 1 kHz 30 ms tick on each beat, 1.8 kHz 70 ms louder
    tick on each downbeat (a downbeat is always drawn as the accent, even if
    the tracker left it out of the beat list)."""
    import numpy as np
    out = np.zeros(n_samples, dtype="float32")

    def tick(freq, dur, amp):
        t = np.arange(int(dur * SR)) / SR
        return (amp * np.sin(2 * np.pi * freq * t) * np.exp(-t / (dur / 4))).astype("float32")
    low, high = tick(1000, 0.03, 0.35), tick(1800, 0.07, 0.6)
    down = {round(d, 3) for d in downbeats}
    for t in beats:
        if round(t, 3) in down:
            continue
        i = int(round(t * SR))
        seg = low[:max(0, n_samples - i)]
        if i >= 0 and len(seg):
            out[i:i + len(seg)] += seg
    for t in downbeats:
        i = int(round(t * SR))
        seg = high[:max(0, n_samples - i)]
        if i >= 0 and len(seg):
            out[i:i + len(seg)] += seg
    return out


def mix_clicks(source, beats, downbeats):
    """Source at 0.7 plus the click layer; scaled down as a whole only if the
    sum would pass 0.9 of full scale, so it is never clipped or blown out."""
    import numpy as np
    clicks = click_track(len(source), beats, downbeats)
    mix = source * 0.7 + clicks[:, None]
    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > 0.9:
        mix = mix * (0.9 / peak)
    return mix.astype("float32")


def encode_mp3(ffmpeg, stereo, path):
    r = subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "f32le", "-ac", "2", "-ar", str(SR), "-i", "-",
                        "-c:a", "libmp3lame", "-q:a", "2", path],
                       input=stereo.astype("<f4").tobytes(), capture_output=True)
    if r.returncode != 0:
        raise GridError("ffmpeg could not write grid-check.mp3 (%s)."
                        % (r.stderr.decode(errors="replace").strip().splitlines() or ["no detail"])[-1])


# ── model steps ─────────────────────────────────────────────────────────────
def drum_stem_mono(stereo):
    import numpy as np
    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    try:
        model = get_model("htdemucs")
    except Exception as e:
        raise GridError("Demucs could not load its htdemucs model (%s); it downloads once, so it needs "
                        "internet the first time." % str(e).splitlines()[0][:120])
    model.eval()
    wav = torch.from_numpy(np.ascontiguousarray(stereo.T))
    ref = wav.mean(0)
    norm = (wav - ref.mean()) / (ref.std() + 1e-8)
    with torch.no_grad():
        out = apply_model(model, norm[None], device="cpu", split=True, overlap=0.25, progress=False)[0]
    drums = out[model.sources.index("drums")] * (ref.std() + 1e-8) + ref.mean()
    return drums.mean(0).numpy().astype("float32")


def track_beats(signal_mono, checkpoint):
    """-> (beats, model_downbeats, downbeat_logit_at_each_beat). The logits are
    beat_this's own frame-level downbeat output (inference.py, Audio2Frames
    .__call__ -> Spect2Frames.spect2frames, 50 frames a second), which
    Audio2Beats normally hands straight to its peak-picking postprocessor; it is
    run once here and the same logits feed both."""
    import numpy as np
    from beat_this.inference import Audio2Beats, Audio2Frames
    try:
        tracker = Audio2Beats(checkpoint_path=checkpoint, device="cpu", dbn=False)
    except ValueError as e:
        raise GridError("beat_this could not load its %s checkpoint (%s); it downloads once, so it "
                        "needs internet the first time." % (checkpoint, e.args[0] if e.args else e))
    beat_logits, down_logits = Audio2Frames.__call__(tracker, signal_mono, SR)
    beats, downbeats = tracker.frames2beats(beat_logits, down_logits)
    dl = down_logits.cpu().numpy()
    at = []
    for t in beats:
        f = int(round(float(t) * tracker.frames2beats.fps))
        at.append(float(np.max(dl[max(0, f - 2):f + 3])) if len(dl) else 0.0)
    return [float(b) for b in beats], [float(d) for d in downbeats], at


def _version(dist):
    try:
        from importlib.metadata import version
        return version(dist)
    except Exception:
        return None


def run(args):
    progress = lambda n: print("PROGRESS %d/4" % n, flush=True)  # noqa: E731
    sentence = missing_tool_sentence(args.drum_stem)
    if sentence:
        raise GridError(sentence)
    import numpy as np
    os.makedirs(args.out, exist_ok=True)

    progress(1)
    stereo = decode(args.ffmpeg, args.audio)
    if len(stereo) < SR * 4:
        raise GridError("That audio is under four seconds long, too short to find bars in.")
    tracked = "mix"
    mono = stereo.mean(1)
    if args.drum_stem:
        progress(2)
        mono = drum_stem_mono(stereo)
        tracked = "drum stem (Demucs htdemucs)"
    progress(3)
    beats, downbeats, logits = track_beats(mono.astype("float64"), args.checkpoint)
    phase = None
    snapped = None
    if args.beats_per_bar:
        downbeats, k, margin, means = phase_downbeats(beats, logits, args.beats_per_bar)
        phase = {"phase": k, "margin": round(margin, 3), "mean_logit_per_phase": [round(m, 3) for m in means]}
        if args.first_downbeat is not None:     # the user's ear replaces the model's phase guess
            downbeats, k, snapped = anchor_downbeats(beats, args.first_downbeat, args.beats_per_bar)
            phase["phase"] = k
    report = analyse(beats, downbeats, args.beats_per_bar, snapped)
    if phase:
        report["meter"].update(phase)
    report["source"] = {"file": os.path.basename(args.audio), "duration_s": round(len(stereo) / SR, 2),
                        "tracked": tracked}
    report["expected_bpm"] = args.expected_bpm
    report["tools"] = {"beat_this": _version("beat_this"), "checkpoint": args.checkpoint,
                       "demucs": _version("demucs") if args.drum_stem else None,
                       "torch": _version("torch"), "numpy": np.__version__}
    report["summary"] = summary_line(report, args.expected_bpm)
    with open(os.path.join(args.out, "grid.json"), "w") as f:
        json.dump(report, f, indent=1)
    progress(4)
    encode_mp3(args.ffmpeg, mix_clicks(stereo, report["beats"], report["downbeats"]),
               os.path.join(args.out, "grid-check.mp3"))
    print(report["summary"], flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="only report whether the tools are installed")
    ap.add_argument("--in", dest="audio")
    ap.add_argument("--out")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--drum-stem", action="store_true")
    ap.add_argument("--expected-bpm", type=float, default=None)
    ap.add_argument("--beats-per-bar", type=int, default=None, choices=range(2, 8),
                    help="the meter you know; bar starts then come from the model's downbeat logits at that period")
    ap.add_argument("--first-downbeat", type=float, default=None,
                    help="seconds: a time where a bar starts (with --beats-per-bar); snaps to the nearest detected beat")
    ap.add_argument("--checkpoint", default="final0")
    args = ap.parse_args(argv)
    try:
        if args.check:
            s = missing_tool_sentence(args.drum_stem)
            if s:
                print(s)
                return 2
            return 0
        if not args.audio or not args.out:
            raise GridError("Grid check was started without an audio file or an output folder.")
        run(args)
        return 0
    except GridError as e:
        print(str(e))
        return 2
    except Exception as e:  # never a traceback on the job: the sentence, plus a log beside the outputs
        try:
            if args.out:
                os.makedirs(args.out, exist_ok=True)
                with open(os.path.join(args.out, "grid-error.log"), "w") as f:
                    f.write(traceback.format_exc())
        except OSError:
            pass
        print("Grid check stopped on an unexpected problem (%s: %s); the details are in grid-error.log."
              % (type(e).__name__, str(e).splitlines()[0][:160] if str(e) else "no message"))
        return 2


if __name__ == "__main__":
    sys.exit(main())
