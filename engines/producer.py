"""Engine pack: "Grid check" in the Cover room (process lane, no ComfyUI graph).

Where every beat and bar of a song actually sits, and a click track to hear it:
the mode's "graph" is a one-step run plan for runner.py that calls
engines/producer_tools/grid_check.py under a Python that has beat_this (MIT
code and weights) and, for the optional drum stem, Demucs (MIT) installed.
beat_this gives per-beat times, so nothing downstream ever fits one constant
period.

Pointing BWF at that Python is the same mechanism turntable uses for Blender:
a declared bin, found by name. Put a program called `bwf-producer-python` on
PATH (a wrapper script that execs your venv's python: a bare symlink to a venv
python loses the venv), or set BWF_PRODUCER_PYTHON to its full path before
starting BWF.
"""
import math
import os
import subprocess

_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "producer_tools", "grid_check.py")
_STEP_TIMEOUT_S = 3600      # a ceiling, not an estimate: CPU Demucs plus beat_this on a long song
_PREFLIGHT_TIMEOUT_S = 30
_BPM_RANGE = (30, 300)


def _expected_bpm(value):
    if value in (None, ""):
        return None
    try:
        bpm = float(value)
    except (TypeError, ValueError):
        raise ValueError("expected_bpm must be a number of beats per minute, got %r." % (value,))
    lo, hi = _BPM_RANGE
    if not lo <= bpm <= hi:
        raise ValueError("expected_bpm must be between %d and %d, got %g." % (lo, hi, bpm))
    return bpm


def _beats_per_bar(value):
    if value in (None, "", "auto"):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    if not 2 <= n <= 7:
        raise ValueError("beats_per_bar must be auto or a whole number from 2 to 7, got %r." % (value,))
    return n


def _first_downbeat(value):
    if value in (None, ""):
        return None
    try:
        t = float(value)
    except (TypeError, ValueError):
        raise ValueError("first_downbeat must be a time in seconds, got %r." % (value,))
    if not math.isfinite(t) or t < 0:
        raise ValueError("first_downbeat must be a finite time in seconds, zero or more, got %g." % t)
    return t


def _preflight(python, drum_stem):
    """Ask the configured Python (through the helper's own --check, so the
    sentence has one source) whether the tools are installed. A miss is the
    one plain sentence on the request, never a traceback on a job."""
    argv = [python, _HELPER, "--check"] + (["--drum-stem"] if drum_stem else [])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=_PREFLIGHT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ValueError("Grid check could not start the Python BWF is pointed at (%s): %s."
                         % (python, e.__class__.__name__))
    if r.returncode != 0:
        said = (r.stdout or "").strip().splitlines()
        raise ValueError(said[-1] if said else
                         "The Python BWF is pointed at (%s) could not be used for the grid check." % python)


def grid_plan(args, models):
    """Run plan for one grid check. Field defaults are applied here, in the
    graph, the same way turntable's are."""
    drum = bool(args.get("use_drum_stem", False))
    bpm = _expected_bpm(args.get("expected_bpm"))
    per_bar = _beats_per_bar(args.get("beats_per_bar"))
    anchor = _first_downbeat(args.get("first_downbeat"))
    python = (models or {}).get("producer_python")
    # Discovery hands over which()'s path, always a real program; anything else (a placeholder
    # in a test) is not run here, and the runner says what it could not start.
    if python and os.path.isfile(python) and os.access(python, os.X_OK):
        _preflight(python, drum)
    argv = ["{bin:producer_python}", "{pack}/producer_tools/grid_check.py",
            "--in", "{in:source_audio_name}", "--out", "{job}",
            "--ffmpeg", "{bin:ffmpeg}"]
    if drum:
        argv.append("--drum-stem")
    if bpm is not None:
        argv += ["--expected-bpm", "%g" % bpm]
    if per_bar is not None:
        argv += ["--beats-per-bar", str(per_bar)]
        if anchor is not None:          # ignored with auto: there is no phase to fix
            argv += ["--first-downbeat", "%g" % anchor]
    return {
        "steps": [{"argv": argv, "timeout_s": _STEP_TIMEOUT_S}],
        "outputs": ["grid-check.mp3", "grid.json"],   # the playable one first: it is what the page shows
        "progress": r"PROGRESS (\d+)/(\d+)",
        "summary": True,      # grid_check.py prints its one-line result last: the job keeps it as its notes
    }


ENGINE = {
    "id": "producer",
    "cap": "producer",
    "lane_kind": "process",
    "bins": {"producer_python": os.environ.get("BWF_PRODUCER_PYTHON") or "bwf-producer-python",
             "ffmpeg": "ffmpeg"},
    "provides": {"grid": ["producer_python", "ffmpeg"]},
    "words": {
        "producer_python": "a Python with beat_this installed (put a program called bwf-producer-python "
                           "on PATH, or set BWF_PRODUCER_PYTHON to its full path)",
        "ffmpeg": "ffmpeg",
    },
    "graphs": {
        "grid": grid_plan,
    },
    "describe": lambda models: (
        "beat_this (beats and bars, processor)"
        if models.get("producer_python") and models.get("ffmpeg") else ""),
    "mode_words": {
        "grid": "Grid check (beats and bars)",
    },
    "mode_rooms": {"grid": "cover"},
    "mode_notes": {
        "grid": "finds every beat and bar start in a song and gives you a click track to check them by ear; runs on the processor",  # source: engines/producer_tools/grid_check.py (module docstring: steps 3 and 5)
    },
    "fields": {
        "grid": [
            {"id": "source_audio_name", "label": "Song to check", "type": "audio",
             "tier": "primary", "group": "Content", "order": 1,
             "hint": "a song you already have"},
            {"id": "use_drum_stem", "label": "Use the drum stem", "type": "checkbox",
             "default": False, "tier": "primary", "group": "Content", "order": 2,
             "hint": "splits the drums out first and tracks those (several times slower); try it when the full mix confuses the beat"},
            {"id": "beats_per_bar", "label": "Beats per bar", "type": "select",
             "default": "auto", "options": ["auto", 2, 3, 4, 5, 6, 7],
             "tier": "primary", "group": "Content", "order": 3,
             "hint": "auto trusts the model's bar starts, which are unreliable on odd meters; if you know the meter, set it and only the bar-start position is searched"},
            {"id": "first_downbeat", "label": "A bar starts at (seconds)", "type": "number",
             "range": [0, 3600], "ui_range": [0, 300], "units": "s",
             "tier": "advanced", "group": "Content", "order": 4,
             "hint": "play the grid check, find any bar's first beat, type its time; fixes where bars start (only used when Beats per bar is a number, ignored with auto)"},
            {"id": "expected_bpm", "label": "Tempo you asked for", "type": "number",
             "range": list(_BPM_RANGE), "ui_range": [60, 180], "units": "BPM",
             "tier": "advanced", "group": "Content", "order": 5,
             "hint": "optional; only shown beside the measured tempo in the summary"},
        ],
    },
    "presets": {
        "grid": [
            {"id": "default", "label": "Default",
             "note": "Tracks the full mix; no tempo asked for.",
             "values": {}},
        ],
    },
    "quality": {
        "grid": [
            {"id": "mix", "label": "Full mix", "default": True,
             "why": "tracks the song as it is; a few seconds for a song on the processor",  # source: measured 2026-09-30, 110 s and 178 s songs: 3.8 s and 5 s wall
             "values": {"use_drum_stem": False}},
            {"id": "drums", "label": "Drums first",
             "why": "splits the drums out and tracks those; about five times slower, for when the full mix confuses the beat",  # source: measured 2026-09-30, same 110 s song: 16.6 s vs 3.8 s wall
             "values": {"use_drum_stem": True}},
        ],
    },
    "examples": {
        "grid": [
            {"id": "grid-try", "label": "Check the beat grid", "recipe": None,
             "quality": None, "values": {},
             "why": "finds every beat and bar and gives you a click track to hear where they sit",
             "needs": "sound"},
        ],
    },
    "licence": {
        "name": "MIT (beat_this, Demucs)",
        "shippable": True,
        "attribution": "Beats and bars found with beat_this (CPJKU); drum stems, when asked for, with Demucs (Meta).",
        "summary": "Free to use commercially; code and weights are MIT. beat_this notes some of its training music is under limited licences.",
    },
}
