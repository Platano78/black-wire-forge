#!/usr/bin/env python3
"""Fit (Producer room): the helper script the producer pack's "fit" mode runs.

Reads the beats of a SOURCE song (the song a part was made in) and a TARGET
song (the beat to sit on), pairs source beat i0+k with target beat j0+k, and
time-stretches the PART beat by beat so that source beat i0+k lands on
target beat j0+k. A drifting tempo is followed beat by beat; no single ratio
is applied.

Inputs (wav paths) are decoded to stereo float32 at gc.SR with the same
ffmpeg decode grid_check uses; the part is optional and defaults to the
source song. Beats come from grid_check's track_beats under the "final0"
checkpoint. The stretch method is chosen once for the whole job: the
Rubber Band binary when --rubberband names an existing file, else ffmpeg
atempo (chained atempo=0.5/2.0 when the per-segment ratio would fall
outside [0.5, 2]).

Each source segment (beat i0+k up to the next beat) is stretched to exactly
the target span (beat j0+k up to j0+k+1), written at the target beat time,
with a 5 ms linear crossfade against what the previous segment left there
(the stretched segment is extended 5 ms past its end so the next one has
something to fade from). The pickup before the first source beat and the
tail after the last paired beat are stretched with the first/last segment's
ratio and placed so the part starts and ends where the target does.

Outputs in --out: fitted.wav (pcm_s16le, peak 0.98), preview.mp3 (target +
fitted part at 0.7 each, peak 0.9, via grid_check.encode_mp3) and fit.json
(pairs, the two anchor times, both songs' median BPM, per-segment ratio
min/median/max, method, the repair counts of both beat grids). Before
pairing, repair_grid() fills a beat the tracker missed and drops one it
invented, so a single slip does not shift the rest of the part. One PROGRESS
n/5 line per stage; the final line is the summary, e.g.
    fitted 74 beats onto the target: 112.0 -> 95.0 BPM (stretch 0.85x), ffmpeg atempo
plus, when a repair happened, a note of how many beats were added and
removed, and, for a big stretch done with ffmpeg (|1 - median| > 0.15), a
plain note that big stretches can sound phasey and Rubber Band does better.

A bad argument or an impossible request is one plain sentence and exit code
2, never a traceback (catch gc.GridError and ValueError in main()).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid_check as gc  # noqa: E402

SR = gc.SR


def _detail(stderr):
    """The last line of a tool's stderr, for use inside a one-sentence error."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    return lines[-1] if lines else "no detail"


FADE_S = 0.005
MIN_PAIRS = 8
FIT_ERROR_LOG = "fit-error.log"
TOTAL_STEPS = 5
PHASEY_LIMIT = 0.15
FIT_TIMEOUT_S = 600


def nearest_beat(beats, t):
    """The index of the beat nearest a time in seconds. Pure: a plain list
    of times, no model. Refuses (ValueError, one sentence) when the nearest
    beat is farther than gc.ANCHOR_SNAP_S."""
    j = min(range(len(beats)), key=lambda i: abs(beats[i] - t))
    if abs(beats[j] - t) > gc.ANCHOR_SNAP_S:
        raise ValueError("No beat was found within %.2f s of the %.2f s you gave as the bar "
                         "to line up (the nearest is at %.2f s), so it cannot be used."
                         % (gc.ANCHOR_SNAP_S, t, beats[j]))
    return j


def repair_grid(beats):
    """A beat the tracker missed is a gap about twice the usual length; one it
    invented is a gap far too short. Fill and drop such beats so that pairing
    by index does not slip past them. Pure: a list of times in seconds, numpy
    only. m(i) is the median of the up-to-16 gaps around gap i (8 on each
    side, fewer near the ends). A gap below half of m(i) has its LATER beat
    dropped (re-evaluated from the same position); a gap that is close to an
    integer multiple n of m(i) (n >= 2, within 0.2 * m(i)) gets n-1 evenly
    spaced beats inserted inside it. A gap that does not divide cleanly (for
    example a real break in the music) is left alone. Fewer than 4 gaps: the
    list is returned unchanged. -> (repaired beats, inserted, dropped), the
    beats as plain Python floats."""
    b = sorted(float(t) for t in beats)
    if len(b) - 1 < 4:
        return list(b), 0, 0
    import numpy as np
    inserted = 0
    dropped = 0
    i = 0
    while i < len(b) - 1:
        lo, hi = max(0, i - 8), min(len(b) - 2, i + 8)
        window = [b[j + 1] - b[j] for j in range(lo, hi + 1)]
        window.remove(b[i + 1] - b[i])  # the gap itself is not its own context
        m = float(np.median(window))
        gap = b[i + 1] - b[i]
        if m <= 0:
            i += 1
            continue
        if gap < 0.5 * m:
            del b[i + 1]
            dropped += 1
            continue  # re-evaluate from the same position
        n = int(round(gap / m))
        if n >= 2 and abs(gap - n * m) <= 0.2 * m:
            b[i + 1:i + 1] = [b[i] + gap * k / n for k in range(1, n)]
            inserted += n - 1
        i += 1
    return b, inserted, dropped


def match_tempos(beats_s, beats_t, how):
    """The two beat grids, checked against each other before anything is
    paired: if one song's beat runs about twice as fast as the other's, then
    pairing beat by beat would squash or stretch the part by 2x, so that is
    refused unless the job asked for every other beat of that song. Pure:
    two lists of times in seconds, no model.
    r is the source's typical beat over the target's (r about 2 means the
    target's beats come twice as fast). "every": unchanged, or one sentence
    (gc.GridError) when r is outside [0.625, 1.6]. "half_target": the
    target keeps every other beat (it is the faster one). "half_source": the
    source does. -> (source beats, target beats, note), the note being "" when
    nothing was halved."""
    import numpy as np
    if how not in ("every", "half_target", "half_source"):
        raise gc.GridError("Unknown beat match %r." % how)
    if how == "half_target":
        return beats_s, beats_t[::2], "used every other beat of the song to fit onto"
    if how == "half_source":
        return beats_s[::2], beats_t, "used every other beat of the first song"
    ds = np.diff(np.asarray(beats_s, dtype="float64"))
    dt = np.diff(np.asarray(beats_t, dtype="float64"))
    if len(ds) == 0 or len(dt) == 0:
        return beats_s, beats_t, ""
    r = float(np.median(ds)) / float(np.median(dt))
    if 0.625 <= r <= 1.6:
        return beats_s, beats_t, ""
    # the setting that fixes it is the one that halves the FASTER song's beats
    fix = "half_target (the song to fit onto has the faster beat)" if r > 1.6 \
        else "half_source (the first song has the faster beat)"
    raise gc.GridError(
        "The two songs' beats are about %dx apart (%.0f vs %.0f BPM), so pairing "
        "them beat by beat would squash or stretch the part. Set Beat match to %s."
        % (round(max(r, 1.0 / r)), 60.0 / float(np.median(ds)), 60.0 / float(np.median(dt)), fix))


def anchor_index(beats, downbeats, bar_at):
    """i0/j0: the first beat to line up. The requested bar anchor when one
    was given (refused with one sentence when no beat is within the snap
    window), else the beat nearest the model's first downbeat, else 0."""
    if bar_at is not None:
        return nearest_beat(beats, bar_at)
    if downbeats:
        return min(range(len(beats)), key=lambda i: abs(beats[i] - downbeats[0]))
    return 0


def atempo_chain(r):
    """One ffmpeg atempo filter (or a comma-chained one) for a ratio:
    atempo only accepts [0.5, 2], so chain atempo=0.5 / atempo=2 as many
    times as needed until the remainder fits."""
    steps = []
    while r < 0.5:
        steps.append(0.5)
        r /= 0.5
    while r > 2.0:
        steps.append(2.0)
        r /= 2.0
    steps.append(r)
    return ",".join("atempo=%g" % x for x in steps)


def _decode_pcm(ffmpeg, raw, want):
    import numpy as np
    if not raw:
        raise gc.GridError("The audio came back empty; is the file playable?")
    a = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
    if len(a) < want:
        a = np.concatenate([a, np.zeros((want - len(a), 2), dtype="float32")], axis=0)
    return a[:want]


# atempo drops inputs this small (its buffer never flushes); those get resampled by hand
SHORT = int(round(0.1 * SR))


def _linear_resample(seg, want):
    """Re-lengthen one short segment, sample by sample (linear); the error is
    inaudible at the sub-0.1 s lengths this path sees."""
    import numpy as np
    if want == 0:
        return np.zeros((0, 2), dtype="float32")
    if len(seg) == 0:
        return np.zeros((want, 2), dtype="float32")
    pos = np.linspace(0.0, len(seg) - 1, want)
    i0 = np.floor(pos).astype(int)
    i1 = np.minimum(i0 + 1, len(seg) - 1)
    frac = (pos - i0)[:, None]
    return (seg[i0] * (1.0 - frac) + seg[i1] * frac).astype("float32")


def make_ffmpeg_stretcher(ffmpeg):
    import numpy as np

    def stretch(seg, want):
        if len(seg) < SHORT or want < SHORT:
            return _linear_resample(seg, want)
        if want == 0:
            return np.zeros((0, 2), dtype="float32")
        r = len(seg) / want
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-v", "error",
             "-f", "f32le", "-ac", "2", "-ar", str(SR), "-i", "-",
             "-af", atempo_chain(r),
             "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
            input=seg.astype("<f4").tobytes(), capture_output=True)
        if proc.returncode != 0:
            raise gc.GridError("The audio stretch failed on one beat: " + _detail(proc.stderr))
        return _decode_pcm(ffmpeg, proc.stdout, want)

    return stretch


def make_rubberband_stretcher(ffmpeg, rubberband):
    import numpy as np

    def stretch(seg, want):
        if len(seg) < SHORT or want < SHORT:
            return _linear_resample(seg, want)
        if want == 0:
            return np.zeros((0, 2), dtype="float32")
        tmp = None
        try:
            tmp = __import__("tempfile").mkdtemp(prefix="bwf_fit_rb_")
            src = os.path.join(tmp, "seg.wav")
            dst = os.path.join(tmp, "seg_st.wav")
            ok = subprocess.run(
                [ffmpeg, "-hide_banner", "-v", "error", "-y",
                 "-f", "f32le", "-ac", "2", "-ar", str(SR), "-i", "-",
                 "-c:a", "pcm_s16le", src],
                input=seg.astype("<f4").tobytes(), capture_output=True)
            if ok.returncode != 0:
                raise gc.GridError("The audio stretch failed on one beat: " + _detail(ok.stderr))
            proc = subprocess.run(
                [rubberband, "-q", "-3", "-D", "%.6f" % (want / SR), src, dst],
                capture_output=True, text=True)
            if proc.returncode != 0 or not os.path.isfile(dst):
                raise gc.GridError("The audio stretch failed on one beat: "
                                   + _detail(proc.stderr or "Rubber Band wrote no file"))
            raw = subprocess.run(
                [ffmpeg, "-hide_banner", "-v", "error", "-i", dst,
                 "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"], capture_output=True).stdout
            if not raw:
                raise gc.GridError("The audio stretch came back empty.")
            a = np.frombuffer(raw, dtype="<f4").reshape(-1, 2)
            if len(a) < want:
                a = np.concatenate([a, np.zeros((want - len(a), 2), dtype="float32")], axis=0)
            return a[:want]
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)

    return stretch


def _write_xfade(buf, pos, sig, fade, n):
    """Write sig into buf at pos; over the first `fade` samples, fade sig in
    and whatever is already there out (complementary linear ramps). Samples
    that would land before time zero are dropped."""
    import numpy as np
    if len(sig) == 0 or pos >= n:
        return
    if pos < 0:
        sig = sig[-pos:]
        pos = 0
        if len(sig) == 0:
            return
    end = min(n, pos + len(sig))
    m = end - pos
    f = min(fade, m)
    ramp = np.linspace(0.0, 1.0, f)
    buf[pos:pos + f] = sig[:f] * ramp[:, None] + buf[pos:pos + f] * (1.0 - ramp[:, None])
    buf[pos + f:end] = sig[f:m]


def fit(args, out, progress):
    import numpy as np
    import statistics
    sentence = gc.missing_tool_sentence(False)
    if sentence:
        raise gc.GridError(sentence)
    os.makedirs(out, exist_ok=True)

    # a. decode the three files; the part must be the source's length
    src = gc.decode(args.ffmpeg, args.source)
    tgt = gc.decode(args.ffmpeg, args.target)
    part = src if args.part is None else gc.decode(args.ffmpeg, args.part)
    if abs(len(part) - len(src)) / SR > 0.5:
        raise gc.GridError(
            "The part must be the same length as the song it was made in "
            "(the part is %.1f s, the song is %.1f s)." % (len(part) / SR, len(src) / SR))
    progress(1)

    # b. the beats of both songs. repair_grid fills a beat the tracker missed
    # and drops one it invented, so pairing by index does not slip (the model's
    # downbeat times are kept as they are: the anchors snap to the nearest
    # repaired beat by time, as before)
    beats_s, downs_s, _ = gc.track_beats(src.mean(axis=1).astype("float64"), "final0")
    beats_t, downs_t, _ = gc.track_beats(tgt.mean(axis=1).astype("float64"), "final0")
    beats_s, ins_s, dro_s = repair_grid(beats_s)
    beats_t, ins_t, dro_t = repair_grid(beats_t)
    how = getattr(args, "beat_match", None) or "every"
    beats_s, beats_t, note = match_tempos(beats_s, beats_t, how)
    progress(2)

    # c. where to start lining up
    i0 = anchor_index(beats_s, downs_s, args.source_bar_at)
    j0 = anchor_index(beats_t, downs_t, args.target_bar_at)

    # d. the pairs
    K = min(len(beats_s) - i0, len(beats_t) - j0)
    if K < MIN_PAIRS:
        raise gc.GridError("Only %d beats could be paired between the two songs; Fit needs at least %d."
                           % (K, MIN_PAIRS))
    progress(3)

    # e/f. stretch each segment onto its target span (the method is chosen
    # once for the whole job)
    rb = args.rubberband
    if rb and os.path.isfile(rb) and os.access(rb, os.X_OK):
        stretch = make_rubberband_stretcher(args.ffmpeg, rb)
        method = "rubberband"
    else:
        stretch = make_ffmpeg_stretcher(args.ffmpeg)
        method = "ffmpeg atempo"
    n = len(tgt)
    buf = np.zeros((n, 2), dtype="float32")
    fade = int(round(FADE_S * SR))
    ratios = []
    for k in range(K - 1):
        s0 = int(round(beats_s[i0 + k] * SR))
        s1 = int(round(beats_s[i0 + k + 1] * SR))
        t0 = int(round(beats_t[j0 + k] * SR))
        L = int(round(beats_t[j0 + k + 1] * SR)) - t0
        ratios.append((s1 - s0) / L)
        s1e = min(s1 + fade, len(part))
        seg = stretch(part[s0:s1e], L + fade)
        _write_xfade(buf, t0, seg, fade, n)
    # the pickup before the first source beat, with the first segment's ratio
    s0 = int(round(beats_s[i0] * SR))
    t0 = int(round(beats_t[j0] * SR))
    L0 = int(round(beats_t[j0 + 1] * SR)) - t0
    s10 = int(round(beats_s[i0 + 1] * SR))
    if s0 > 0 and L0 > 0 and s10 > s0:
        ratio0 = L0 / (s10 - s0)
        seg = stretch(part[:s0], int(round(s0 * ratio0)))
        _write_xfade(buf, t0 - len(seg), seg, fade, n)
    # the tail after the last paired beat, with the last segment's ratio
    s_last = int(round(beats_s[i0 + K - 1] * SR))
    t_last = int(round(beats_t[j0 + K - 1] * SR))
    s_prev = int(round(beats_s[i0 + K - 2] * SR))
    t_prev = int(round(beats_t[j0 + K - 2] * SR))
    if s_last < len(part) and s_last > s_prev and t_last > t_prev:
        ratio_l = (t_last - t_prev) / (s_last - s_prev)
        seg = stretch(part[s_last:], int(round((len(part) - s_last) * ratio_l)))
        _write_xfade(buf, t_last, seg, fade, n)
    progress(4)

    # g. fitted.wav, preview.mp3, fit.json
    peak = float(np.max(np.abs(buf))) if len(buf) else 0.0
    fitted = buf
    if peak > 0.98:
        fitted = buf * (0.98 / peak)
    wav_path = os.path.join(out, "fitted.wav")
    proc = subprocess.run(
        [args.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "f32le", "-ac", "2", "-ar", str(SR), "-i", "-",
         "-c:a", "pcm_s16le", wav_path],
        input=fitted.astype("<f4").tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise gc.GridError("Writing the fitted audio failed: " + _detail(proc.stderr))
    mix = tgt * 0.7 + buf * 0.7
    mp3_path = os.path.join(out, "preview.mp3")
    peak = float(np.max(np.abs(mix))) if len(mix) else 0.0
    if peak > 0.9:
        mix = mix * (0.9 / peak)
    gc.encode_mp3(args.ffmpeg, mix.astype("float32"), mp3_path)

    sbpm = statistics.median(60.0 / d for d in np.diff(beats_s))
    tbpm = statistics.median(60.0 / d for d in np.diff(beats_t))
    doc = {
        "pairs": K,
        "source_anchor_s": float(beats_s[i0]),
        "target_anchor_s": float(beats_t[j0]),
        "source_bpm": sbpm,
        "target_bpm": tbpm,
        "stretch": {"min": min(ratios), "median": statistics.median(ratios), "max": max(ratios)},
        "method": method,
        "beat_match": how,
        "repaired": {"source": {"inserted": ins_s, "dropped": dro_s},
                     "target": {"inserted": ins_t, "dropped": dro_t}},
    }
    with open(os.path.join(out, "fit.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    progress(5)

    line = "fitted %d beats onto the target: %.1f -> %.1f BPM (stretch %.2fx), %s" % (
        K, sbpm, tbpm, statistics.median(ratios), method)
    if note:
        line += "; " + note
    if ins_s + ins_t + dro_s + dro_t:
        line += "; repaired the beat grids (%d added, %d removed)" % (ins_s + ins_t, dro_s + dro_t)
    if method == "ffmpeg atempo" and abs(1.0 - statistics.median(ratios)) > PHASEY_LIMIT:
        line += "; big stretches can sound phasey with ffmpeg, Rubber Band does better"
    print(line, flush=True)
    return doc


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--source", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--part")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ffmpeg", required=True)
    ap.add_argument("--source-bar-at", dest="source_bar_at", type=float)
    ap.add_argument("--target-bar-at", dest="target_bar_at", type=float)
    ap.add_argument("--rubberband")
    ap.add_argument("--beat-match", dest="beat_match", choices=["every", "half_target", "half_source"],
                    default="every")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.check:
        sentence = gc.missing_tool_sentence(False)
        if sentence:
            print(sentence)
            return 2
        return 0

    try:
        for name, what in (("source_bar_at", "the first-song anchor"),
                           ("target_bar_at", "the target anchor")):
            v = getattr(args, name)
            if v is not None and not (v == v and v not in (float("inf"), float("-inf")) and v >= 0):
                raise gc.GridError("%s must be a time in seconds, zero or more." % what)
        for name, what in (("--source", "the song the part was made in"),
                           ("--target", "the song to fit it onto")):
            if not os.path.isfile(getattr(args, name.lstrip("-").replace("-", "_"))):
                raise gc.GridError("Fit was started without %s: upload it first." % what)
        def progress(k):
            print("PROGRESS %d/%d" % (k, TOTAL_STEPS), flush=True)
        fit(args, args.out, progress)
        return 0
    except (gc.GridError, ValueError) as e:
        print(str(e))
        return 2
    except Exception:
        path = os.path.join(args.out or ".", FIT_ERROR_LOG)
        try:
            os.makedirs(args.out, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        print("Fit stopped on an unexpected problem; the details went to %s." % FIT_ERROR_LOG)
        return 2


if __name__ == "__main__":
    sys.exit(main())
