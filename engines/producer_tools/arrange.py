#!/usr/bin/env python3
"""Re-arrange (Producer room): the helper script the producer pack's "arrange"
mode runs.

Cuts a song at its bar starts and plays the bars back in the order you type,
for example "1-4, 1-4, 9-16" (the first four bars twice, then a jump). The bars
come from grid_check's beat tracking under the "final0" checkpoint, with the
same meter handling grid_check uses: --beats-per-bar searches only the bar-start
position at that period, --first-downbeat is the user's ear fixing where bars
start. Consecutive pieces are joined with a short equal-power crossfade (10 ms
by default, 2..50 ms), with a short fade in at the very start and out at the
very end, and the result is never allowed to clip (peak 0.98).

The cut is sample-exact: each piece is a plain slice of the decoded song at
gc.SR, so a bar replays at the sample it was played at; nothing is stretched,
pitched or re-timed.

Outputs in --out: arranged.mp3 (gc.encode_mp3), arranged.wav (pcm_s16le) and
arrange.json (bars, bar starts, the order as typed, the segments played and the
length of the result). One PROGRESS n/4 line per stage; the final line is the
summary, e.g.
    re-arranged 8 bars into 20.0 s (the song has 12 bars)

A bad argument or an impossible order is one plain sentence and exit code 2,
never a traceback (catch gc.GridError and ValueError in main()).
"""
import argparse
import json
import os
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import grid_check as gc  # noqa: E402

SR = gc.SR
TOTAL_STEPS = 4
MAX_ITEMS = 200
PEAK = 0.98
FADE_MIN_MS, FADE_MAX_MS, FADE_DEFAULT_MS = 2, 50, 10
ARRANGE_ERROR_LOG = "arrange-error.log"
EXAMPLES_SENTENCE = "Type the bars to play, for example 1-4, 1-4, 9-12."


def _detail(stderr):
    """The last line of a tool's stderr, for use inside a one-sentence error."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    return lines[-1] if lines else "no detail"


def _number(bar, n_bars):
    """One bar number: whole, one or more, and inside the song. Pure."""
    if bar < 1:
        raise gc.GridError("Bars are numbered from 1, so bar %d does not exist." % bar)
    if bar > n_bars:
        raise gc.GridError("This song has %d bars; bar %d is past the end." % (n_bars, bar))
    return bar


def parse_order(text, n_bars):
    """The bars to play, in the order they are written: "1-4, 1-4, 9-12" ->
    [(1, 4), (1, 4), (9, 12)], 1-based and inclusive at both ends. Pure: a
    string and a bar count in, a plain list of pairs out. Spaces are allowed
    anywhere. A number is one bar, a range "N-M" is N up to M. Junk, a bar
    that is not in the song, a range that runs backwards and more than
    MAX_ITEMS items are each refused with one plain sentence."""
    if not text or not text.strip():
        raise gc.GridError(EXAMPLES_SENTENCE)
    items = [i.replace(" ", "") for i in text.split(",")]
    if any(i == "" for i in items):
        raise gc.GridError(EXAMPLES_SENTENCE)
    if len(items) > MAX_ITEMS:
        raise gc.GridError("That is %d items of bars; keep the order to %d or fewer."
                           % (len(items), MAX_ITEMS))
    order = []
    for item in items:
        parts = item.split("-")
        if len(parts) > 2 or not all(p.isdigit() for p in parts):
            raise gc.GridError("Bars are written as a number or a range like 1-4, so %r is not one; %s"
                               % (item, EXAMPLES_SENTENCE))
        first = _number(int(parts[0]), n_bars)
        last = first if len(parts) == 1 else _number(int(parts[1]), n_bars)
        if last < first:
            raise gc.GridError("A range runs forward: %s." % item)
        order.append((first, last))
    return order


def bar_label(pair):
    """One item as it is written back: 4 is "4", 1-4 is "1-4"."""
    return str(pair[0]) if pair[0] == pair[1] else "%d-%d" % pair


def segments(bar_starts, song_seconds, order):
    """The times each item of the order plays, in seconds. Pure: the song's
    bar start times, its length, and the pairs parse_order returned. Bar k
    runs from bar_starts[k-1] up to the next bar start; the last bar of the
    song runs to the end of the song. -> [(start, end), ...] in the typed
    order."""
    segs = []
    for first, last in order:
        start = float(bar_starts[first - 1])
        end = float(bar_starts[last]) if last < len(bar_starts) else float(song_seconds)
        if end < start:
            end = start
        segs.append((start, end))
    return segs


def _ramp(n, equal_power):
    """A fade ramp of n samples: equal power (cos/sin, for a crossfade between
    two pieces) or the plain linear rise a fade in or out wants."""
    import numpy as np
    t = np.linspace(0.0, 1.0, n)
    return (np.cos(t * np.pi / 2.0), np.sin(t * np.pi / 2.0)) if equal_power \
        else (t, 1.0 - t)


def render(stereo, sr, segs, fade_ms):
    """The pieces of the song in the typed order, cut sample-exactly and
    joined by an equal-power crossfade of fade_ms between consecutive pieces,
    with a short fade in at the very start and out at the very end. Never
    clips: a peak above 0.98 is scaled down to it. Pure: a (samples, 2) array,
    a sample rate, the pairs segments returned and a fade length in ms."""
    import numpy as np
    fade = max(1, int(round(fade_ms * sr / 1000.0)))
    pieces = []
    for start, end in segs:
        i = int(round(start * sr))
        j = int(round(end * sr))
        pieces.append(stereo[max(0, i):max(0, j)])
    pieces = [p for p in pieces if len(p)]
    if not pieces:
        return np.zeros((0, 2), dtype="float32")
    out = pieces[0].astype("float32")
    for piece in pieces[1:]:
        f = min(fade, len(out), len(piece))
        a, b = _ramp(f, True)
        head = out[-f:].astype("float64") * a[:, None] + piece[:f].astype("float64") * b[:, None]
        out = np.concatenate([out[:-f], head.astype("float32"), piece[f:]], axis=0)
    f = min(fade, len(out))
    rise, fall = _ramp(f, False)
    out = out.copy()
    out[:f] = (out[:f].astype("float64") * rise[:, None]).astype("float32")
    out[len(out) - f:] = (out[len(out) - f:].astype("float64") * fall[:, None]).astype("float32")
    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > PEAK:
        # a hair under, so the float32 the file is written from cannot round back over 0.98
        out = (out * (PEAK * (1.0 - 1e-5) / peak)).astype("float32")
    return out


def bar_starts_of(args, beats, downbeats, logits):
    """The song's bar start times, found exactly the way grid_check finds them:
    the model's downbeats, or -- with --beats-per-bar -- that period's phase
    (and the user's ear replacing the guess when --first-downbeat is given).
    -> (bar starts, downbeats, snapped anchor, beats per bar)."""
    snapped = None
    if args.beats_per_bar:
        downbeats, _k, _margin, _means = gc.phase_downbeats(beats, logits, args.beats_per_bar)
        if args.first_downbeat is not None:
            downbeats, _k, snapped = gc.anchor_downbeats(beats, args.first_downbeat, args.beats_per_bar)
    report = gc.analyse(beats, downbeats, args.beats_per_bar, snapped)
    starts = [float(b["start"]) for b in report["bars"]]
    return starts, downbeats, snapped, report


def run(args):
    progress = lambda n: print("PROGRESS %d/%d" % (n, TOTAL_STEPS), flush=True)  # noqa: E731
    sentence = gc.missing_tool_sentence(False)
    if sentence:
        raise gc.GridError(sentence)
    if not args.order or not args.order.strip():
        raise gc.GridError(EXAMPLES_SENTENCE)
    if not FADE_MIN_MS <= args.fade_ms <= FADE_MAX_MS:
        raise gc.GridError("The join fade must be between %d and %d ms, got %d."
                           % (FADE_MIN_MS, FADE_MAX_MS, args.fade_ms))
    import numpy as np
    os.makedirs(args.out, exist_ok=True)

    progress(1)
    stereo = gc.decode(args.ffmpeg, args.audio)
    song_seconds = len(stereo) / float(SR)

    progress(2)
    beats, downbeats, logits = gc.track_beats(stereo.mean(axis=1).astype("float64"), "final0")
    starts, downbeats, _snapped, report = bar_starts_of(args, beats, downbeats, logits)
    if len(starts) < 2:
        raise gc.GridError("Only %d bars could be found in this song; re-arranging needs at least 2."
                           % len(starts))

    progress(3)
    order = parse_order(args.order, len(starts))
    # every bar ends at the next downbeat (analyse's bars run downbeat to downbeat), so the
    # song's last bar stops there too, not at the end of the song with its tail
    edges = starts + [d for d in sorted(float(x) for x in report["downbeats"]) if d > starts[-1]][:1]
    segs = segments(edges, song_seconds, order)
    out = render(stereo, SR, segs, args.fade_ms)
    seconds = len(out) / float(SR)

    # arranged.wav (pcm_s16le, like fit.py's fitted.wav) and arranged.mp3
    wav_path = os.path.join(args.out, "arranged.wav")
    proc = subprocess.run(
        [args.ffmpeg, "-hide_banner", "-v", "error", "-y",
         "-f", "f32le", "-ac", "2", "-ar", str(SR), "-i", "-",
         "-c:a", "pcm_s16le", wav_path],
        input=out.astype("<f4").tobytes(), capture_output=True)
    if proc.returncode != 0:
        raise gc.GridError("Writing the re-arranged audio failed: " + _detail(proc.stderr))
    gc.encode_mp3(args.ffmpeg, out.astype("float32"), os.path.join(args.out, "arranged.mp3"))

    played = sum(last - first + 1 for first, last in order)
    doc = {
        "bars": len(starts),
        "bar_starts": [round(s, 4) for s in starts],
        "order": args.order,
        "segments": [{"bars": bar_label(pair), "start_s": round(s, 4), "end_s": round(e, 4)}
                     for pair, (s, e) in zip(order, segs)],
        "seconds": round(seconds, 3),
        "beats_per_bar": args.beats_per_bar if args.beats_per_bar else report["meter"].get("beats_per_bar"),
    }
    with open(os.path.join(args.out, "arrange.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    progress(4)

    print("re-arranged %d bars into %.1f s (the song has %d bars)" % (played, seconds, len(starts)),
          flush=True)
    return doc


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--in", dest="audio")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ffmpeg", required=True)
    ap.add_argument("--order", default="")
    ap.add_argument("--beats-per-bar", dest="beats_per_bar", type=int, choices=range(2, 8))
    ap.add_argument("--first-downbeat", dest="first_downbeat", type=float)
    ap.add_argument("--fade-ms", dest="fade_ms", type=int, default=FADE_DEFAULT_MS)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.check:
        sentence = gc.missing_tool_sentence(False)
        if sentence:
            print(sentence)
            return 2
        return 0

    try:
        if not args.audio or not os.path.isfile(args.audio):
            raise gc.GridError("Re-arrange was started without a song to work on: upload it first.")
        if args.beats_per_bar and args.first_downbeat is not None \
                and not (args.first_downbeat >= 0 and args.first_downbeat == args.first_downbeat):
            raise gc.GridError("A bar start must be a time in seconds, zero or more.")
        run(args)
        return 0
    except (gc.GridError, ValueError) as e:
        print(str(e))
        return 2
    except Exception:
        path = os.path.join(args.out or ".", ARRANGE_ERROR_LOG)
        try:
            os.makedirs(args.out, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        print("Re-arrange stopped on an unexpected problem; the details went to %s." % ARRANGE_ERROR_LOG)
        return 2


if __name__ == "__main__":
    sys.exit(main())
