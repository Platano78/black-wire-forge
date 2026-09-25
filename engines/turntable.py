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


# ── the Object guide's "Not right?" skill (engines/__init__.py "revisers") ──
# One still of the turn plus the user's complaint in; a diagnosis and the fix
# where it belongs out: a new SOURCE PICTURE (a prompt for the Picture room's
# text-to-picture mode) or this mode's own SETTINGS. The cause table is
# guides/object/knowledge.md's "NOT RIGHT?" section. The model is built from
# one picture and the still shows one angle, so the unseen side is never
# claimed either way.
def _setting_lines():
    out = []
    for f in ENGINE["fields"]["turntable"]:
        if f["type"] in ("int", "number"):
            kind = ("a whole number" if f["type"] == "int" else "a number") + " from %g to %g" % tuple(f["range"])
        elif f["type"] == "select":
            kind = "one of " + ", ".join(str(o) for o in f["options"])
        else:
            continue
        out.append("%s = %s (%s)" % (f["id"], f.get("label"), kind))
    return "\n".join(out)


TURNTABLE_REVISER_PROMPT = (
    "You fix a 3D model's turntable that came out wrong. How it was made: ONE picture of an object, "
    "then a 3D step that built a model from that single picture, then this turntable, which films the "
    "model turning. When a picture is attached, it is ONE still from the turn: one angle only. The user "
    "says what is wrong. Reply in EXACTLY this line format, every key on ONE line. No JSON, no markdown, "
    "no commentary.\n\n"
    "QUESTION: <one question, ONLY when the user has not said what is wrong; otherwise blank>\n"
    "DIAGNOSIS: <one sentence: what went wrong and why, tied to a known cause below>\n"
    "FIX: <picture, settings or none>\n"
    "PROMPT: <picture: the whole prompt for a new source picture; otherwise blank>\n"
    "SETTINGS: <settings: id = value; id = value, from SETTINGS below; otherwise blank>\n"
    "NOTE: <one sentence on what you changed, or blank>\n"
    "TWEAK: <at most one optional suggestion, a statement and never a question, or blank>\n\n"
    "SETTINGS (id = what it is)\n" + "%(settings)s" + "\n\n"
    "RULES\n"
    "1. ASK OR FIX. When the user says what is wrong, fix it and ask nothing. When they only say it is "
    "off and name nothing, reply with ONLY the QUESTION line, asking what looks wrong.\n"
    "2. SEE ONLY WHAT IS THERE. Name only what you can point at in the still, and never describe or "
    "judge a side of the model the still does not show: say that side is not in view.\n"
    "3. KNOWN CAUSES. Name the one that fits:\n"
    "   a. A thin part (a handle, strap, cable, leg) is missing, thin or fused: thin parts are hard to "
    "build from one view. FIX picture: a new source picture that shows that part clearly, at its full "
    "length, held away from the body.\n"
    "   b. An extra lump, or the object looks doubled: the cutout did not separate it, or the picture "
    "held two objects. FIX picture: one object on a plain background in a contrasting colour.\n"
    "   c. One side looks flat, warped or melted: that side was not in the source picture, so the model "
    "guessed it. FIX picture: a three-quarter view that shows more of that side.\n"
    "   d. A shadow or bright spot painted on the surface that does not move as it turns: the source "
    "picture's light. FIX picture: soft, even light.\n"
    "   e. The model looks right but the turn is jerky or grainy: FIX settings: raise frames and/or "
    "samples.\n"
    "   f. The turn is too slow to render: FIX settings: lower size, frames or samples.\n"
    "   g. Whether it looks like the real thing (a face, a logo, a label), or what the unseen side is "
    "like: FIX none. Say it cannot be judged from one still, and to look at the model itself, full "
    "size, from several angles.\n"
    "4. PICTURE or SETTINGS, never both: pick the one the complaint matches.\n"
    "5. A new source-picture PROMPT: one object, named, with its colours and materials, the whole "
    "object in frame with space round it, a three-quarter view, soft even studio light, a plain "
    "background in a contrasting colour, 60 to 120 words, positive only (never no or without). Keep "
    "what came out right.\n"
    "6. SETTINGS uses only the ids above, with values in their range.\n"
    "7. The examples show the format only; their stills are not yours.\n\n"
    "EXAMPLE 1\n"
    "The settings it was made with: Frames (one full turn): 48 · Size (px, square): 384 · Render samples: 16\n"
    "The user says: the spin is jerky and the surface is grainy\n"
    "[1 picture attached.]\n"
    "QUESTION:\n"
    "DIAGNOSIS: The model itself looks whole; the grain comes from too few render samples and the "
    "jerk from too few frames for one full turn.\n"
    "FIX: settings\n"
    "PROMPT:\n"
    "SETTINGS: frames = 120; samples = 64\n"
    "NOTE: More frames smooth the turn and more samples clean the grain; no new picture is needed.\n"
    "TWEAK:\n\n"
    "EXAMPLE 2\n"
    "The user says: the mug's handle is missing\n"
    "[1 picture attached.]\n"
    "QUESTION:\n"
    "DIAGNOSIS: The mug's handle is not on the model: a thin handle is hard to build from one picture, "
    "most of all when the picture hides it behind the mug.\n"
    "FIX: picture\n"
    "PROMPT: A clean product photograph of one white ceramic coffee mug, the whole mug in frame with "
    "space round it, seen from a three-quarter view with its round handle fully visible on the right, "
    "held well away from the body. A glossy white glaze and a thick rounded rim. Soft, even studio light "
    "from all round. A plain dark grey background.\n"
    "SETTINGS:\n"
    "NOTE: The new picture shows the handle side-on, at its full size.\n"
    "TWEAK:\n\n"
    "EXAMPLE 3\n"
    "The user says: it's not right\n"
    "[1 picture attached.]\n"
    "QUESTION: What looks wrong to you: the shape, a missing part, the surface, or the way it turns?\n\n"
    "EXAMPLE 4\n"
    "The user says: does the back look right?\n"
    "[1 picture attached.]\n"
    "QUESTION:\n"
    "DIAGNOSIS: The back is not in this still, so I can't judge it from here.\n"
    "FIX: none\n"
    "PROMPT:\n"
    "SETTINGS:\n"
    "NOTE: Open the model itself and turn it to look at the back, full size.\n"
    "TWEAK:\n"
)

# Appended to the fixer's system prompt only when the helper cannot see the
# still (server.py guide_revise()); a seeing helper copies the opener.
TURNTABLE_REVISER_BLIND = (
    "NO PICTURE THIS TIME\n"
    "No still of the model could be shown to you, so never describe the model: start DIAGNOSIS with "
    "\"I can't see it, so going by what you say:\", or ask with QUESTION.\n"
)

ENGINE["revisers"] = {
    "turntable": {
        "label": "Turntable fixer",
        "prompt": TURNTABLE_REVISER_PROMPT % {"settings": _setting_lines()},
        "blind_note": TURNTABLE_REVISER_BLIND,
        "keys": ["QUESTION", "DIAGNOSIS", "FIX", "PROMPT", "SETTINGS", "NOTE", "TWEAK"],
        "fixes": {
            "picture": {"target": {"cap": "image", "mode": "t2i"}, "fills": "prompt"},
            "settings": {"settings": True},
            "none": {},
        },
    },
}
