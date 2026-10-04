"""Engine pack: the Producer room's process modes (process lane, no ComfyUI graph):
Grid check (beat_this), Fit (beat_this plus ffmpeg's atempo or Rubber Band),
Re-arrange (beat_this plus ffmpeg) and Mix tracks (a plain system python3 with
ffmpeg).

Where every beat and bar of a song actually sits, and a click track to hear it:
the mode's "graph" is a one-step run plan for runner.py that calls
engines/producer_tools/grid_check.py under a Python that has beat_this (MIT
code and weights) and, for the optional drum stem, Demucs (MIT) installed.
beat_this gives per-beat times, so nothing downstream ever fits one constant
period. Fit uses the same per-beat times of two songs and stretches a part
beat by beat, so it follows a drifting tempo instead of one ratio.

Pointing BWF at that Python is the same mechanism turntable uses for Blender:
a declared bin, found by name. Put a program called `bwf-producer-python` on
PATH (a wrapper script that execs your venv's python: a bare symlink to a venv
python loses the venv), or set BWF_PRODUCER_PYTHON to its full path before
starting BWF.
"""
import math
import os
import shutil
import subprocess

_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "producer_tools", "grid_check.py")
_STEP_TIMEOUT_S = 3600      # a ceiling, not an estimate: CPU Demucs plus beat_this on a long song
_PREFLIGHT_TIMEOUT_S = 30
_BPM_RANGE = (30, 300)
_ORDER_SENTENCE = "Type the bars to play, for example 1-4, 1-4, 9-12."


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
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=_PREFLIGHT_TIMEOUT_S)
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


def _mix_number(args, name, label, lo, hi, units, default=0.0):
    """One per-track mix number, validated the way _expected_bpm validates BPM:
    a real number inside a plain range, or one sentence naming the field."""
    value = args.get(name, default)
    if value in ("", None):
        value = default
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number, got %s."
                         % (label, "'" + str(value) + "'" if not isinstance(value, (int, float)) else "%g" % value))
    if not math.isfinite(v) or v < lo or v > hi:
        raise ValueError("%s must be between %g and %g %s, got %s."
                         % (label, lo, hi, units,
                            "'" + str(value) + "'" if not isinstance(value, (int, float)) else "%g" % value))
    return v


def _fit_bar(value, label):
    if value in (None, ""):
        return None
    try:
        t = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a time in seconds, got %s."
                         % (label, "'" + str(value) + "'" if not isinstance(value, (int, float)) else "%g" % value))
    if not math.isfinite(t) or t < 0:
        raise ValueError("%s must be a finite time in seconds, zero or more."
                         % ("'" + str(value) + "'" if not isinstance(value, (int, float)) else "%g" % value))
    return t


def fit_plan(args, models):
    """The process run plan for the producer's "fit" mode: read the beats of
    the song a part was made in and of the target song, then stretch the
    part beat by beat onto the target's beats (engines/producer_tools/fit.py
    under a Python that has beat_this; the stretch is ffmpeg atempo, or
    Rubber Band when one is on PATH and not refused). The part is optional
    and defaults to the source song itself."""
    if not args.get("source_audio_name"):
        raise ValueError("Fit needs the song the part was made in: upload it first.")
    if not args.get("target_audio_name"):
        raise ValueError("Fit needs the song to fit the part onto: upload it first.")
    part = args.get("part_audio_name")
    bar_s = _fit_bar(args.get("source_bar_at"), "the first-song anchor")
    bar_t = _fit_bar(args.get("target_bar_at"), "the target anchor")
    tool = args.get("stretch_tool")
    if tool not in (None, "", "auto", "ffmpeg"):
        raise ValueError("The stretch method must be auto or ffmpeg, got %s."
                         % ("'" + str(tool) + "'" if not isinstance(tool, (int, float)) else "%g" % tool))
    beat_match = args.get("beat_match")
    if beat_match in (None, ""):
        beat_match = "every"
    if beat_match not in ("every", "half_target", "half_source"):
        raise ValueError("The beat match must be every, half_target or half_source, got %s."
                         % ("'" + str(beat_match) + "'" if not isinstance(beat_match, (int, float)) else "%g" % beat_match))
    python = (models or {}).get("producer_python")
    # Discovery hands over which()'s path, always a real program; anything else (a placeholder
    # in a test) is not run here, and the runner says what it could not start.
    if python and os.path.isfile(python) and os.access(python, os.X_OK):
        _preflight(python, False)
    argv = ["{bin:producer_python}", "{pack}/producer_tools/fit.py",
            "--source", "{in:source_audio_name}", "--target", "{in:target_audio_name}",
            "--out", "{job}", "--ffmpeg", "{bin:ffmpeg}"]
    if part:
        argv += ["--part", "{in:part_audio_name}"]
    if bar_s is not None:
        argv += ["--source-bar-at", "%g" % bar_s]
    if bar_t is not None:
        argv += ["--target-bar-at", "%g" % bar_t]
    argv += ["--beat-match", beat_match]
    rb = shutil.which("rubberband")
    if rb:
        argv += ["--rubberband", rb]
    return {
        "steps": [{"argv": argv, "timeout_s": _STEP_TIMEOUT_S}],
        "outputs": ["preview.mp3", "fitted.wav", "fit.json"],   # the playable one first: it is what the page shows
        "progress": r"PROGRESS (\d+)/(\d+)",
        "summary": True,      # fit.py prints its one-line result last: the job keeps it as its notes
    }


def arrange_plan(args, models):
    """The process run plan for the producer's "arrange" mode: find the song's
    bar starts the way grid_check does, then cut the song there and play the
    bars back in the order the job names (engines/producer_tools/arrange.py
    under a Python that has beat_this, plus ffmpeg for the writes). The order
    is typed by hand ("1-4, 1-4, 9-16"); an empty one is refused here, with
    the same sentence the helper uses."""
    order = args.get("order")
    if order is None or not str(order).strip():
        raise ValueError(_ORDER_SENTENCE)
    fade = _mix_number(args, "fade_ms", "Join fade", 2, 50, "ms", default=10.0)
    per_bar = _beats_per_bar(args.get("beats_per_bar"))
    anchor = _first_downbeat(args.get("first_downbeat"))
    python = (models or {}).get("producer_python")
    # Discovery hands over which()'s path, always a real program; anything else (a placeholder
    # in a test) is not run here, and the runner says what it could not start.
    if python and os.path.isfile(python) and os.access(python, os.X_OK):
        _preflight(python, False)
    argv = ["{bin:producer_python}", "{pack}/producer_tools/arrange.py",
            "--in", "{in:source_audio_name}", "--out", "{job}",
            "--ffmpeg", "{bin:ffmpeg}", "--order", str(order),
            "--fade-ms", "%g" % fade]
    if per_bar is not None:
        argv += ["--beats-per-bar", str(per_bar)]
        if anchor is not None:          # ignored with auto: there is no phase to fix
            argv += ["--first-downbeat", "%g" % anchor]
    return {
        "steps": [{"argv": argv, "timeout_s": _STEP_TIMEOUT_S}],
        "outputs": ["arranged.mp3", "arranged.wav", "arrange.json"],   # the playable one first: it is what the page shows
        "progress": r"PROGRESS (\d+)/(\d+)",
        "summary": True,      # arrange.py prints its one-line result last: the job keeps it as its notes
    }


def mix_plan(args, models):
    """The process run plan for the producer's "mix" mode: lay up to four
    tracks over each other with a per-track gain and start, then two-pass
    loudness-normalise. mix.py drives ffmpeg; it needs no models, only the
    uploaded track names ({in:track_N})."""
    if not args.get("track_1"):
        raise ValueError("Mix needs a first track: upload the main track, for example the beat or the first part.")
    gains = [_mix_number(args, "gain_%d" % k, "Gain %d" % k, -24, 12, "dB") for k in (1, 2, 3, 4)]
    offsets = [_mix_number(args, "offset_%d" % k, "Start %d" % k, 0, 600, "s") for k in (1, 2, 3, 4)]
    lufs = _mix_number(args, "lufs", "Loudness target", -30, -6, "LUFS", default=-14.0)
    argv = ["{bin:python3}", "{pack}/producer_tools/mix.py",
            "--out", "{job}", "--ffmpeg", "{bin:ffmpeg}", "--lufs", "%g" % lufs]
    for k in (1, 2, 3, 4):
        if not args.get("track_%d" % k):
            continue
        argv += ["--track", "{in:track_%d}" % k,
                 "--gain", "%g" % gains[k - 1],
                 "--offset", "%g" % offsets[k - 1]]
    return {
        "steps": [{"argv": argv, "timeout_s": _STEP_TIMEOUT_S}],
        "outputs": ["mix.mp3", "mix.wav", "mix.json"],   # the playable one first: it is what the page shows
        "progress": r"PROGRESS (\d+)/(\d+)",
        "summary": True,      # mix.py prints its one-line result last: the job keeps it as its notes
    }


ENGINE = {
    "id": "producer",
    "cap": "producer",
    "lane_kind": "process",
    "bins": {"producer_python": os.environ.get("BWF_PRODUCER_PYTHON") or "bwf-producer-python",
             "ffmpeg": "ffmpeg",
             "python3": "python3"},
    # Mix runs on the plain system python3; Grid check needs beat_this's own Python.
    "provides": {"grid": ["producer_python", "ffmpeg"],
                 "fit": ["producer_python", "ffmpeg"],
                 "arrange": ["producer_python", "ffmpeg"],
                 "mix": ["python3", "ffmpeg"]},
    "words": {
        "producer_python": "a Python with beat_this installed (put a program called bwf-producer-python "
                           "on PATH, or set BWF_PRODUCER_PYTHON to its full path)",
        "ffmpeg": "ffmpeg",
        "python3": "Python 3",
    },
    "graphs": {
        "grid": grid_plan,
        "fit": fit_plan,
        "arrange": arrange_plan,
        "mix": mix_plan,
    },
    "describe": lambda models: (
        "beat_this (beats and bars, processor)"
        if models.get("producer_python") and models.get("ffmpeg") else ""),
    "mode_words": {
        "grid": "Grid check (beats and bars)",
        "fit": "Fit a part onto another beat",
        "arrange": "Re-arrange by bars",
        "mix": "Mix tracks",
    },
    "mode_rooms": {"grid": "producer", "fit": "producer", "arrange": "producer", "mix": "producer"},
    "mode_notes": {
        "grid": "finds every beat and bar start in a song and gives you a click track to check them by ear; runs on the processor",  # source: engines/producer_tools/grid_check.py (module docstring: steps 3 and 5)
        "fit": "moves a part (for example a vocal) from the song it was made in onto another song's beats, stretching it beat by beat; runs on the processor",  # source: engines/producer_tools/fit.py (module docstring)
        "arrange": "cuts a song at its bar starts and plays the bars back in the order you type, for example 1-4, 1-4, 9-16; runs on the processor",  # source: engines/producer_tools/arrange.py (module docstring)
        "mix": "lays up to four tracks over each other with a gain and a start time for each, then levels the loudness; runs on the processor",  # source: engines/producer_tools/mix.py (module docstring)
    },
    # The arrange mode's one text field is a bar list, not words for a model: a writer
    # asked to fill it must return only that list.
    "prompt_guides": {
        "arrange": "The bars to play, in order, as comma-separated bar numbers or ranges, for example "  # source: engines/producer_tools/arrange.py (parse_order docstring)
                   "1-4, 1-4, 9-16 (bar 1 is the first full bar; a range runs forward). Return only that list.",
    },
    "fields": {
        "fit": [
            {"id": "source_audio_name", "label": "The song the part was made in", "type": "audio",
             "tier": "primary", "group": "Content", "order": 1,
             "hint": "the song whose beats the part follows; without a separate part, this whole song is what gets fitted"},
            {"id": "part_audio_name", "label": "The part to move", "type": "audio", "optional": True,
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "optional; the part, cut out of the first song at the same length"},
            {"id": "target_audio_name", "label": "The song to fit it onto", "type": "audio",
             "tier": "primary", "group": "Content", "order": 3,
             "hint": "the beat the part should sit on"},
            {"id": "source_bar_at", "label": "A beat in the first song (seconds)", "type": "number",
             "range": [0, 3600], "ui_range": [0, 300], "units": "s",
             "tier": "advanced", "group": "Anchors", "order": 1,
             "hint": "the time of a beat you can hear in the first song; without it, the first bar is used"},
            {"id": "target_bar_at", "label": "The beat it should first land on (seconds)", "type": "number",
             "range": [0, 3600], "ui_range": [0, 300], "units": "s",
             "tier": "advanced", "group": "Anchors", "order": 2,
             "hint": "without it, the target's first bar is used"},
            {"id": "stretch_tool", "label": "Stretch method", "type": "select",
             "default": "auto", "options": ["auto", "ffmpeg"],
             "tier": "advanced", "group": "Stretch", "order": 1,
             "hint": "auto uses Rubber Band when it is installed, otherwise ffmpeg (ffmpeg can sound phasey on big stretches)"},
            {"id": "beat_match", "label": "Beat match", "type": "select",
             "default": "every", "options": ["every", "half_target", "half_source"],
             "tier": "advanced", "group": "Stretch", "order": 2,
             "hint": "pair every beat (default); when one song's beat runs twice as fast, pair every other beat of it"},
        ],
        "arrange": [
            {"id": "source_audio_name", "label": "Song to re-arrange", "type": "audio",
             "tier": "primary", "group": "Content", "order": 1,
             "hint": "a song you already have"},
            {"id": "order", "label": "Bars to play, in order", "type": "text", "default": "1-4, 1-4",
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "bar 1 is the first full bar; for example 1-4, 1-4, 9-16 repeats the first four bars, then jumps"},
            {"id": "beats_per_bar", "label": "Beats per bar", "type": "select",
             "default": "auto", "options": ["auto", 2, 3, 4, 5, 6, 7],
             "tier": "primary", "group": "Content", "order": 3,
             "hint": "auto trusts the model's bar starts, which are unreliable on odd meters; if you know the meter, set it and only the bar-start position is searched"},
            {"id": "first_downbeat", "label": "A bar starts at (seconds)", "type": "number",
             "range": [0, 3600], "ui_range": [0, 300], "units": "s",
             "tier": "advanced", "group": "Content", "order": 4,
             "hint": "play the grid check, find any bar's first beat, type its time; fixes where bars start (only used when Beats per bar is a number, ignored with auto)"},
            {"id": "fade_ms", "label": "Join fade", "type": "number",
             "default": 10, "range": [2, 50], "ui_range": [2, 30], "units": "ms",
             "tier": "advanced", "group": "Content", "order": 5,
             "hint": "how long the crossfade is where two pieces meet; longer hides a jump, shorter sounds tighter"},
        ],
        "mix": [
            {"id": "track_1", "label": "Track 1", "type": "audio",
             "tier": "primary", "group": "Tracks", "order": 1,
             "hint": "the main track, for example the beat or the first part"},
            {"id": "track_2", "label": "Track 2", "type": "audio", "optional": True,
             "tier": "primary", "group": "Tracks", "order": 2,
             "hint": "up to three more tracks can go over the first"},
            {"id": "track_3", "label": "Track 3", "type": "audio", "optional": True,
             "tier": "advanced", "group": "Tracks", "order": 3},
            {"id": "track_4", "label": "Track 4", "type": "audio", "optional": True,
             "tier": "advanced", "group": "Tracks", "order": 4},
            {"id": "gain_1", "label": "Gain 1", "type": "number",
             "default": 0, "range": [-24, 12], "ui_range": [-12, 6], "units": "dB",
             "tier": "advanced", "group": "Levels", "order": 1},
            {"id": "gain_2", "label": "Gain 2", "type": "number",
             "default": 0, "range": [-24, 12], "ui_range": [-12, 6], "units": "dB",
             "tier": "advanced", "group": "Levels", "order": 2},
            {"id": "gain_3", "label": "Gain 3", "type": "number",
             "default": 0, "range": [-24, 12], "ui_range": [-12, 6], "units": "dB",
             "tier": "advanced", "group": "Levels", "order": 3},
            {"id": "gain_4", "label": "Gain 4", "type": "number",
             "default": 0, "range": [-24, 12], "ui_range": [-12, 6], "units": "dB",
             "tier": "advanced", "group": "Levels", "order": 4},
            {"id": "offset_1", "label": "Start 1", "type": "number",
             "default": 0, "range": [0, 600], "ui_range": [0, 120], "units": "s",
             "tier": "advanced", "group": "Starts", "order": 1},
            {"id": "offset_2", "label": "Start 2", "type": "number",
             "default": 0, "range": [0, 600], "ui_range": [0, 120], "units": "s",
             "tier": "advanced", "group": "Starts", "order": 2},
            {"id": "offset_3", "label": "Start 3", "type": "number",
             "default": 0, "range": [0, 600], "ui_range": [0, 120], "units": "s",
             "tier": "advanced", "group": "Starts", "order": 3},
            {"id": "offset_4", "label": "Start 4", "type": "number",
             "default": 0, "range": [0, 600], "ui_range": [0, 120], "units": "s",
             "tier": "advanced", "group": "Starts", "order": 4},
            {"id": "lufs", "label": "Loudness target", "type": "number",
             "default": -14, "range": [-30, -6], "ui_range": [-24, -10], "units": "LUFS",
             "tier": "advanced", "group": "Levels", "order": 9},
        ],
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
        "fit": [
            {"id": "default", "label": "Default",
             "note": "Lines the two songs up at their first bars; the stretch uses Rubber Band when installed, else ffmpeg.",
             "values": {}},
        ],
        "mix": [
            {"id": "default", "label": "Default",
             "note": "Every track at 0 dB from the start, levelled to -14 LUFS.",
             "values": {}},
        ],
        "arrange": [
            {"id": "default", "label": "Default",
             "note": "Plays the bars in the order you type, joined by 10 ms crossfades.",
             "values": {}},
        ],
        "grid": [
            {"id": "default", "label": "Default",
             "note": "Tracks the full mix; no tempo asked for.",
             "values": {}},
        ],
    },
    "quality": {
        "fit": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "beat by beat, with short crossfades; a minute or so for a song on the processor",
             "values": {}},
        ],
        "mix": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "two-pass loudness levelling; a few seconds for a song",  # source: measured 2026-10-01, 6 s mix: 0.7 s wall
             "values": {}},
        ],
        "arrange": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "a few seconds for a song on the processor",
             "values": {}},
        ],
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
        "fit": [
            {"id": "fit-try", "label": "Put a vocal on a new beat", "recipe": None,
             "quality": None,
             "values": {},
             "why": "the whole song onto the new beat; upload just the vocal as the part to move only that",
             "needs": "sound"},
        ],
        "mix": [
            {"id": "mix-try", "label": "Layer two parts", "recipe": None,
             "quality": None,
             "values": {"gain_2": -3, "offset_2": 4},
             "why": "a second part, 3 dB lower, coming in after the first bar",
             "needs": "sound"},
        ],
        "arrange": [
            {"id": "arrange-try", "label": "Repeat the first four bars", "recipe": None,
             "quality": None,
             "values": {"order": "1-4, 1-4"},
             "why": "the first four bars twice, as a loop to write over",
             "needs": "sound"},
        ],
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
