"""Engine pack: 3D (turntable: orbit a .glb and film it, Cycles on CPU).

Process-lane pack per the internal Blender process-lane design doc, slice B2. No ComfyUI
graph at all: the mode's "graph" is a run plan for runner.py --
  1. Blender renders N PNGs while a camera orbits the model
     (engines/blender/turntable.py, Cycles, CPU only), then
  2. ffmpeg encodes those frames into turntable.mp4.
poster.png is frame 1, always rendered on a transparent film so it keeps
its alpha even when the job's background is opaque.

Owner ruling 2026-09-22: Cycles runs on the box's PROCESSOR,
never its GPU -- the graphics card stays with whatever else lives on that
box. The per-frame timeout budget is generous by design: measured 2026-09-22
on the reference box, factory scene, 32 samples: 512px = 0.92 s, 1024px =
3.01 s per frame; a real mesh costs more, so 30 s a frame at or under
512 px and 60 s above is a ceiling, not an estimate.
"""

# Per-frame timeout budget (seconds) by frame size. Step timeout = budget *
# frames, capped at _CAP_S.
_PER_FRAME = (30, 60)          # <=512 px, above
_CAP_S = 21600
_FFMPEG_TIMEOUT_S = 600

# Field hard limits; the quality tiers must stay inside them.
_R = {
    "frames": (24, 240),
    "fps": (12, 60),
    "size": (384, 1024),       # the select's options are the real set
    "samples": (8, 256),
    "elevation": (-10, 60),
}
_BACKGROUNDS = ["dark", "light", "transparent"]


def _coerce(key, value, default):
    """Coerce one arg to its declared type, falling back to the field
    default, then validate. A bad value is a plain ValueError with a
    sentence -- the dispatch layer shows it on the job."""
    if value in (None, ""):
        value = default
    if key in ("frames", "fps", "samples"):
        value = int(value)
        lo, hi = _R[key]
        if not lo <= value <= hi:
            raise ValueError("%s must be between %d and %d, got %d." % (key, lo, hi, value))
        return value
    if key == "size":
        # E1: "size" is a select field (its options happen to be numbers),
        # so the generic dispatcher's own int/number coercion never runs on
        # it -- an unguarded int() here would let Python's own exception
        # text reach the user for a value outside the four options.
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("size must be one of 384, 512, 768, 1024, got %r." % value)
        if value not in (384, 512, 768, 1024):
            raise ValueError("size must be one of 384, 512, 768, 1024, got %d." % value)
        return value
    if key == "elevation":
        value = float(value)
        lo, hi = _R["elevation"]
        if not lo <= value <= hi:
            raise ValueError("elevation must be between %d and %d degrees, got %g." % (lo, hi, value))
        return value
    if key == "background":
        if value not in _BACKGROUNDS:
            raise ValueError("background must be %s, got %r." % ("/".join(_BACKGROUNDS), value))
        return value
    raise ValueError("unknown argument %r." % key)


def turntable_plan(args, models):
    """Build the run plan from the (sparse) args the client sent, quality
    tier already applied. Field defaults are applied here, in the graph,
    the same way pixelart's does."""
    frames = _coerce("frames", args.get("frames", 72), 72)
    fps = _coerce("fps", args.get("fps", 24), 24)
    size = _coerce("size", args.get("size", 512), 512)
    samples = _coerce("samples", args.get("samples", 32), 32)
    elevation = _coerce("elevation", args.get("elevation", 20), 20)
    background = _coerce("background", args.get("background", "dark"), "dark")
    budget = _PER_FRAME[0] if size <= 512 else _PER_FRAME[1]
    return {
        "steps": [
            {"argv": [
                 "{bin:blender}", "-b", "--factory-startup", "--python",
                 "{pack}/blender/turntable.py", "--",
                 "--in", "{in:model}",
                 "--out", "{job}/frames",
                 "--frames", str(frames),
                 "--size", str(size),
                 "--samples", str(samples),
                 "--background", background,
                 "--elevation", str(elevation),
                 "--poster", "{job}/poster.png"],
             "timeout_s": min(budget * frames, _CAP_S)},
            {"argv": [
                 "{bin:ffmpeg}", "-y", "-loglevel", "error",
                 "-framerate", str(fps),
                 "-i", "{job}/frames/f_%04d.png",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
                 "-movflags", "+faststart",
                 "{job}/turntable.mp4"],
             "timeout_s": _FFMPEG_TIMEOUT_S},
        ],
        "outputs": ["turntable.mp4", "poster.png"],
        "progress": r"PROGRESS (\d+)/(\d+)",
    }


ENGINE = {
    "id": "turntable",
    "cap": "3d",
    "lane_kind": "process",
    "bins": {"blender": "blender", "ffmpeg": "ffmpeg"},
    "provides": {"turntable": ["blender", "ffmpeg"]},
    "words": {
        "blender": "Blender",
        "ffmpeg": "ffmpeg",
    },
    "graphs": {
        "turntable": turntable_plan,
    },
    "describe": lambda models: (
        "Blender (Cycles, processor)"
        if models.get("blender") and models.get("ffmpeg") else ""),
    "mode_words": {
        "turntable": "Turn a 3D model around",
    },
    "mode_rooms": {"turntable": "3d"},
    "mode_notes": {
        "turntable": "renders on the processor, so it never competes for the graphics card; about 1 s a frame at 512 px on a simple scene",  # source: the internal Blender process-lane design doc (measured 2026-09-22, Cycles CPU)
    },
    "fields": {
        "turntable": [
            {"id": "model", "label": "3D model (.glb)", "type": "model",
             "tier": "primary", "group": "Content", "order": 1,
             "hint": "a .glb file you upload"},
            {"id": "frames", "label": "Frames (one full turn)", "type": "int",
             "default": 72, "range": list(_R["frames"]), "ui_range": [24, 120],
             "units": "frames", "tier": "primary", "group": "Turn", "order": 1,
             "hint": "one full circle; more makes the spin smoother"},
            {"id": "size", "label": "Size (px, square)", "type": "select",
             "default": 512, "options": [384, 512, 768, 1024],
             "tier": "primary", "group": "Turn", "order": 2},
            {"id": "background", "label": "Background", "type": "select",
             "default": "dark", "options": _BACKGROUNDS,
             "tier": "advanced", "group": "Turn", "order": 3,
             "hint": "transparent keeps alpha in the poster; the video is on black"},
            {"id": "fps", "label": "Frame rate", "type": "int",
             "default": 24, "range": list(_R["fps"]), "ui_range": [12, 30],
             "units": "fps", "tier": "advanced", "group": "Turn", "order": 4},
            {"id": "samples", "label": "Render samples", "type": "int",
             "default": 32, "range": list(_R["samples"]), "ui_range": [8, 64],
             "units": "samples", "tier": "advanced", "group": "Turn", "order": 5,
             "hint": "more cleans up the grain but costs seconds per frame"},
            {"id": "elevation", "label": "Camera height angle", "type": "number",
             "default": 20, "range": list(_R["elevation"]), "ui_range": [-10, 45],
             "units": "degrees", "tier": "advanced", "group": "Turn", "order": 6},
        ],
    },
    "presets": {
        "turntable": [
            {"id": "default", "label": "Default",
             "note": "One full circle, 24 fps, 512 px, dark background.",
             "values": {}},
        ],
    },
    "quality": {
        "turntable": [
            {"id": "draft", "label": "Draft",
             "why": "quick look, about half a minute on a simple model",
             "values": {"frames": 48, "size": 384, "samples": 16}},
            {"id": "standard", "label": "Standard", "default": True,
             "why": "smooth turn at a readable size",
             "values": {"frames": 72, "size": 512, "samples": 32}},
            {"id": "high", "label": "High",
             "why": "slow smooth turn, sharper; minutes on a detailed model",
             "values": {"frames": 120, "size": 768, "samples": 64}},
        ],
    },
    # H2: "Try this" -- takes a .glb the user uploads; nothing else to fill.
    "examples": {
        "turntable": [
            {"id": "turntable-try", "label": "Turn it around", "recipe": "default",
             "quality": "standard", "values": {},
             "why": "turns your 3D model around for a look from every side",
             "needs": "3D model"},
        ],
    },
    "licence": {
        "name": "GPL-2.0-or-later (Blender)",
        "shippable": True,
        "attribution": "Rendered with Blender. Blender's licence covers the program, not the pictures you make with it.",
    },
}
