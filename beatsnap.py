"""Beat-snapped shot windows: whole bars, edges on detected downbeats, frames rounded cumulatively.

Pure functions, stdlib only. Times are seconds; the cut clock starts at ``master_start`` on the master file.
Rules:
  * a shot starting at s begins its bar count at the downbeat NEAREST s (a tie goes to the earlier one);
  * frames are ``round(t * fps)`` with Python's own round() (half to even), taken on the cut clock and
    cumulative, so each shot's f0 is the previous shot's f1 and the shots tile with no gap;
  * a shot renders on its model's frame grid at the smallest length that covers its window, then is trimmed.
A shot is a dict: ``bars`` (int >= 1) | ``end_at`` (cut-clock seconds) | ``to_end`` (True) | none of them (auto);
``grid`` (key into ``grids``); optional ``id`` (used in error sentences), ``planned_len`` (seconds, for auto),
``tail`` (True when a ``continue`` shot is cabled from this one: keep its last frames, not its first).
A grid is ``{"step", "offset", "min", "max", "carried"}``.
"""
import math
import statistics

ASSUMED_FPS = 24
MAX_LINES = 400
MAX_LINE_CHARS = 300


def _name(shot, i):
    return str(shot.get("id") or "shot %d" % (i + 1))


def _next_frame(f0, t0, t1, fps):
    """Frame index of cut-clock time t1. Cumulative: it ignores f0/t0, so rounding errors never add up."""
    return round(t1 * fps)


def _need(W, grid):
    return W + grid.get("carried", 0) + (1 if grid["step"] == 8 else 0)


def _grid_frames(need, grid):
    step, offset = grid["step"], grid["offset"]
    want = max(need, grid["min"])
    n = max(0, math.ceil((want - offset) / step))
    return step * n + offset


def render_frames(W, grid, label="the shot"):
    """Smallest frame count on the grid that covers a W-frame window (LTX also needs one spare frame)."""
    r = _grid_frames(_need(W, grid), grid)
    if r > grid["max"]:
        raise ValueError("%s needs %d frames, but this model makes at most %d." % (label, r, grid["max"]))
    return r


def _fits(W, grid):
    return W >= 1 and _grid_frames(_need(W, grid), grid) <= grid["max"]


def _clock(downbeats, end, master_start):
    D = [d - master_start for d in downbeats if d - master_start >= 0]
    return D, end - master_start


def _nearest(D, s):
    return min(range(len(D)), key=lambda i: (abs(D[i] - s), i))


def fit_max_bars(D, f0, k, grid, fps=ASSUMED_FPS):
    """Most bars a shot starting at frame f0 on downbeat index k can take, by the song and by the model's
    longest render. D is on the cut clock. Returns 0 when not even one bar fits."""
    best = 0
    for b in range(1, len(D) - k):
        W = round(D[k + b] * fps) - f0
        if not _fits(W, grid):
            if W >= 1:
                break
            continue
        best = b
    return best


def windows(downbeats, end, shots, grids, master_start=0.0, fps=ASSUMED_FPS):
    """One window per shot, in order. downbeats/end are on the master's clock (end = song seconds).

    Window keys: id, f0, f1, W, start, len, render_frames, E, keep ("head"|"tail"), trim_in, slice_start,
    bars, k (intended downbeat index, or None), edge ("bars"|"end_at"|"to_end"), clamped (a last shot that ran
    to the end of the song but was cut back to the most bars its model can render).
    slice_start may be negative: that means leading silence before the master slice (audio side's job).
    """
    D, end_c = _clock(downbeats, end, master_start)
    if not D:
        raise ValueError("The song map has no downbeat at or after the master start.")
    if not shots:
        return []
    bar_s = statistics.median([b - a for a, b in zip(D, D[1:])]) if len(D) > 1 else None
    out, f0, s, prev = [], 0, 0.0, None
    for i, shot in enumerate(shots):
        name = _name(shot, i)
        if prev is not None and prev["edge"] == "to_end":
            raise ValueError("%s comes after a shot that already runs to the end of the song." % name)
        gname = shot.get("grid")
        if gname not in grids:
            raise ValueError("%s has no frame grid (%r), so it cannot be snapped to the beats." % (name, gname))
        grid = grids[gname]
        last = i == len(shots) - 1
        k = _nearest(D, s)
        modes = [m for m in ("bars", "end_at", "to_end") if shot.get(m) is not None and shot.get(m) is not False]
        if len(modes) > 1:
            raise ValueError("%s sets more than one of bars, end_at and to_end." % name)
        mode = modes[0] if modes else None
        bars = None
        clamped = False
        auto = mode is None
        if auto and last:
            mode = "to_end"
        left = len(D) - 1 - k
        after = "after shot %s" % _name(shots[i - 1], i - 1) if i else "from the start"
        if mode == "bars":
            bars = shot["bars"]
            if not isinstance(bars, int) or isinstance(bars, bool) or bars < 1:
                raise ValueError("%s: bars must be a whole number, 1 or more." % name)
            if bars > left:
                raise ValueError("%s asks for %d bars, but the song has %d bar%s left %s."
                                 % (name, bars, left, "" if left == 1 else "s", after))
            t1 = D[k + bars]
        elif mode == "end_at":
            t1 = shot["end_at"]
        elif mode == "to_end":
            t1 = end_c
            if last and not _fits(_next_frame(f0, s, t1, fps) - f0, grid):
                # The model cannot render the rest of the song in one shot: take the most whole bars it can and
                # say so (the cut then ends there). Not even one bar fitting is still an error, below.
                fit = fit_max_bars(D, f0, k, grid, fps)
                if fit >= 1:
                    mode, bars, t1, clamped = "bars", fit, D[k + fit], True
        else:
            if bar_s is None or left < 1:
                raise ValueError("%s: the song has no bars left %s." % (name, after))
            fit = fit_max_bars(D, f0, k, grid, fps)
            if fit < 1:
                raise ValueError("%s cannot fit even one bar on this model's frame grid." % name)
            planned = shot.get("planned_len")
            bars = 1 if planned is None else max(1, min(fit, round(planned / bar_s)))
            t1 = D[k + bars]
        if t1 > end_c + 1e-9:
            raise ValueError("%s ends at %.3f s, past the end of the song (%.3f s)." % (name, t1, end_c))
        f1 = _next_frame(f0, s, t1, fps)
        W = f1 - f0
        if W < 1:
            raise ValueError("%s would be %d frames long; it needs at least 1." % (name, W))
        if mode == "bars" and not _fits(W, grid):
            fit = fit_max_bars(D, f0, k, grid, fps)
            raise ValueError("%s asks for %d bars (%d frames), but this model makes at most %d frames; "
                             "at most %d bars fit." % (name, bars, W, grid["max"], fit))
        rf = render_frames(W, grid, "%s (%d frames)" % (name, W))
        carried = grid.get("carried", 0)
        E = rf - carried - W
        tail = bool(shot.get("tail"))
        start = f0 / fps
        prev = dict(
            id=name, f0=f0, f1=f1, W=W, start=start, len=W / fps, render_frames=rf, E=E,
            keep="tail" if tail else "head", trim_in=(E / fps) if tail else 0.0,
            slice_start=start - carried / fps - ((E / fps) if tail else 0.0),
            bars=bars, k=None if bars is None else k, edge="bars" if bars else mode, clamped=clamped)
        out.append(prev)
        f0, s = f1, t1
    return out


def slice_plan(slice_start, master_start, render_frames, fps=ASSUMED_FPS):
    """How to cut a shot's sound out of the master file. ``slice_start`` is on the cut clock and may be
    negative. -> {"seek": seconds into the file, "lead": seconds of silence to put in front (the window starts
    before the file does), "len": slice seconds (the render plus one frame)}."""
    pos = master_start + slice_start
    return {"seek": max(0.0, pos), "lead": max(0.0, -pos), "len": render_frames / fps + 1.0 / fps}


def parse_timed_lines(text):
    """``[m:ss.x] words`` lines -> [{"start": seconds, "text": words}]. Blank lines are skipped."""
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        row = raw.strip()
        if not row:
            continue
        ok = row.startswith("[") and "]" in row
        if ok:
            stamp, words = row[1:].split("]", 1)
            mm, sep, ss = stamp.strip().partition(":")
            try:
                if not sep:
                    raise ValueError
                start = int(mm) * 60 + float(ss)
                if not (math.isfinite(start) and start >= 0 and float(ss) < 60):
                    raise ValueError
            except ValueError:
                ok = False
        if not ok:
            raise ValueError("Line %d is not in the form [m:ss.x] words: %r" % (n, row[:60]))
        words = words.strip()
        if len(words) > MAX_LINE_CHARS:
            raise ValueError("Line %d is longer than %d characters." % (n, MAX_LINE_CHARS))
        lines.append({"start": start, "text": words})
        if len(lines) > MAX_LINES:
            raise ValueError("More than %d timed lines." % MAX_LINES)
    return lines
