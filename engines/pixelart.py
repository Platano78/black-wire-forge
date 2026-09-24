"""Engine pack: Pixel Art (sprites), rebuilt on Qwen-Image 2.1.

Owner ruling, the internal component-inventory notes: Qwen-Image 2.1 is the ONLY
picture generator now -- the old route (SDXL + pixel-art-xl LoRA + Canny
ControlNet, an earlier, frozen ComfyUI graph builder's build_pixelart)
is retired. Only the FIRST stage was ever a model; the rest is
our internal sprite script (quantise.py), a deterministic median-cut palette lock
-> Bayer dither -> nearest-neighbour downscale (a recorded internal decision: "a
deterministic transform, not a model call, so it never drifts"), vendored
here as engines/_quantise.py and run as this mode's `post` step (see
engines/__init__.py's pack contract).

New route: Qwen 2.1 generate or edit -> BiRefNet background removal ->
quantise. Generation and background removal are ONE ComfyUI graph (this
pack's own graphs); quantise runs AFTER the lane finishes, on the core's own
side, because it is not a ComfyUI node.

Why BiRefNet, not Qwen's own removal: the cleanup pack's BiRefNet cutout is
already wired and ~10x faster (4.7s vs 50s, measured
2026-09-21) -- pixel art is an iterative, many-seeds workflow where that gap
compounds. This pack shares "birefnet_model" by name with engines/cleanup.py
rather than loading a second checkpoint.

Shape control: the frozen SDXL builder branched on an optional mask filename
(Canny ControlNet, absent = a plain SDXL render). Qwen 2.1 has no ControlNet
on the rig (verified against a live GET /object_info, 2026-09-22 -- only the
retired pixel-sdxl checkpoint's ControlNet exists). This pack keeps the same
two-branch shape -- an optional `shape_image` -- but the held branch drives
Qwen 2.1's OWN edit mode instead: the uploaded silhouette becomes the edit
reference, with an instruction to keep its exact outline.

MEASURED 2026-09-22, 3 seeds (1001/2002/3003), same dagger mask as the SDXL
route's own gate (an internal proof mask, not included here),
IoU = the alpha of the background-removed 1024x1024 render against that mask,
binarised, via our internal pixel gate's silhouette_iou:
  - shape_image supplied (edit route):  IoU 0.9042 / 0.9166 / 0.9461, median 0.9166
  - no shape_image (plain t2i route):   IoU 0.5657 / 0.5731 / 0.6177, median 0.5731
The held route comes within 0.037 of the retired SDXL/Canny route's 0.953 and is
shippable as a "hold this shape" mode. The free route does NOT hold a shape --
it ships as what it is, a free render, never claimed as parity with either
other route.
"""
import os
import tempfile

from . import _quantise
from .qwen_image import qwen_edit_graph, qwen_t2i_graph


def pixelart_graph(p, m):
    """Qwen 2.1 (reusing qwen_image's own graph builders, not a copy of them)
    plus a background-removal chain appended in place of its SaveImage --
    the RGBA result is this mode's PRIMARY output, and the pack's `post`
    entry (below) is what turns it into a sprite.
    """
    shape = p.get("shape_image")
    if shape:
        # The user's own prompt describes WHAT to draw; this wraps it into the
        # instruction the edit route was measured with (see the module
        # docstring's IoU table) -- the user never has to word the "hold this
        # outline" part themselves.
        instruction = ("Fill the exact black silhouette shape in this image with %s. "
                        "Keep the silhouette's exact outer edges unchanged. Replace the "
                        "white area with a plain white background. Do not change the "
                        "shape's outline." % p["prompt"])
        base = qwen_edit_graph(dict(p, prompt=instruction, ref_images=[shape]), m)
    else:
        base = qwen_t2i_graph(p, m)
    del base["10"]   # the qwen graph's own SaveImage -- replaced below
    base["20"] = {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": m["birefnet_model"]}}
    base["21"] = {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["20", 0], "image": ["9", 0]}}
    base["22"] = {"class_type": "InvertMask", "inputs": {"mask": ["21", 0]}}
    base["23"] = {"class_type": "JoinImageWithAlpha", "inputs": {"image": ["9", 0], "alpha": ["22", 0]}}
    base["24"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": "blackwire/PIXELART", "images": ["23", 0]}}
    return base


def _post_quantise(input_bytes, filename, args):
    """The core's own post-render step (see engines/__init__.py's `post` key):
    palette-lock, dither, downscale. `_quantise.quantise_and_dither` is a
    vendored CLI script that works on file paths, not bytes -- write the
    render to a temp file, read the sprite back, clean up either way. The
    same background-removed render supplies both the palette's subject
    pixels (its RGB) and the sprite's transparency (its own alpha channel,
    `alpha_path=src`).
    """
    size = int(args.get("pixel_size", 64))
    colors = int(args.get("pixel_colors", 8))
    dither = float(args.get("pixel_dither", 0.0))
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, filename or "render.png")
        with open(src, "wb") as f:
            f.write(input_bytes)
        out = os.path.join(td, "sprite.png")
        _quantise.quantise_and_dither(src, out, target_size=size, n_colors=colors,
                                       dither_strength=dither, alpha_path=src)
        with open(out, "rb") as f:
            data = f.read()
    return data, "sprite_%dx%d.png" % (size, size)


def _deps_reason():
    """P1: numpy/Pillow are not stdlib and are the CORE's own dependency
    (the `post` step below runs on the core's side, not a lane) -- when
    they are not importable this mode cannot run on ANY lane, which is
    what makes this a `mode_deps` check rather than a per-lane model role."""
    return None if _quantise.DEPS_OK else (
        "Pixel art needs the numpy and Pillow Python packages: python3 -m venv .venv && "
        ".venv/bin/python -m pip install -r requirements.txt")


ENGINE = {
    "id": "pixelart",
    "cap": "image",
    # Deliberately NOT declared -- this pack is not the cap's primary engine
    # (qwen-image is; see engines/__init__.py's cap_word()/cap_order() "the
    # first pack that actually DECLARES one wins" rule, same discipline as
    # engines/cleanup.py).
    "roles": {
        "qwen_unet": ("unet", {"all": ["qwen_image"], "none": ["minimax", "vae"], "prefer": ["2.1"]}),
        "qwen_clip": ("clip", {"all": ["qwen3vl"], "none": ["minimax"], "prefer": ["8b"]}),
        "qwen_vae": ("vae", {"all": ["qwen_image", "vae"], "none": ["minimax"], "prefer": ["2.1"]}),
        "birefnet_model": ("bg_removal", {"all": []}),
    },
    "primary": {"pixelart": "qwen_unet"},
    "provides": {
        "pixelart": ["qwen_unet", "qwen_clip", "qwen_vae", "birefnet_model"],
    },
    "words": {
        "qwen_unet": "the picture model",
        "qwen_clip": "its text encoder",
        "qwen_vae": "its image decoder",
        "birefnet_model": "the background removal model",
    },
    "graphs": {
        "pixelart": pixelart_graph,
    },
    "post": {
        "pixelart": _post_quantise,
    },
    "mode_deps": {
        "pixelart": _deps_reason,
    },
    # Always "" -- same reasoning as engines/cleanup.py's describe(): this is
    # a recipe on top of the picture generator, not the cap's headline.
    "describe": lambda models: "",
    "mode_words": {
        "pixelart": "A pixel art sprite",
    },
    "mode_rooms": {"pixelart": "pixelart"},
    "mode_notes": {
        "pixelart": "the palette is locked by a fixed step after the picture, so it never drifts",  # source: engines/pixelart.py:9 (module docstring)
    },
    # L5: how a prompt must be written, drawn only from this pack's own
    # docstring -- never a new claim.
    "prompt_guides": {
        "pixelart": "Describe the sprite or subject; pixelation and palette are "  # source: engines/pixelart.py:9-10 (module docstring)
                    "applied afterward as a fixed step, so keep this about the subject, not the pixel style.",
    },
    "fields": {
        "pixelart": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "shape_image", "label": "Shape to hold", "type": "image",
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "Optional. A black shape on a white background. The sprite is drawn "
                     "to fill that exact outline instead of a free pose."},
            {"id": "negative", "label": "Things to avoid", "type": "text", "default": "",
             "tier": "advanced", "group": "Content", "order": 3},
            {"id": "width", "label": "Width", "type": "int", "default": 1024,
             "tier": "advanced", "group": "Size", "order": 1,
             "units": "px", "range": [256, 2048], "ui_range": [512, 1536],
             "hint": "Only used for a free render (no shape uploaded)."},
            {"id": "height", "label": "Height", "type": "int", "default": 1024,
             "tier": "advanced", "group": "Size", "order": 2,
             "units": "px", "range": [256, 2048], "ui_range": [512, 1536],
             "hint": "Only used for a free render (no shape uploaded)."},
            {"id": "resolution", "label": "Working resolution", "type": "int", "default": 1024,
             "tier": "advanced", "group": "Quality", "order": 1,
             "units": "px", "range": [512, 2048], "ui_range": [768, 1280]},
            {"id": "steps", "label": "Steps", "type": "int", "default": 20,
             "tier": "advanced", "group": "Quality", "order": 2,
             "units": "steps", "range": [1, 80], "ui_range": [15, 30]},
            {"id": "cfg", "label": "Guidance strength", "type": "number", "default": 2.5,
             "tier": "advanced", "group": "Quality", "order": 3,
             "units": "strength", "range": [1, 10], "ui_range": [1.5, 4]},
            {"id": "pixel_size", "label": "Sprite size", "type": "int", "default": 64,
             "tier": "primary", "group": "Sprite", "order": 1,
             "units": "px", "range": [16, 256], "ui_range": [32, 128],
             "hint": "The final square size after the colours are locked down."},
            {"id": "pixel_colors", "label": "Colours", "type": "int", "default": 8,
             "tier": "advanced", "group": "Sprite", "order": 2,
             "units": "colours", "range": [2, 32], "ui_range": [4, 16]},
            {"id": "pixel_dither", "label": "Dither", "type": "number", "default": 0.0,
             "tier": "advanced", "group": "Sprite", "order": 3,
             "units": "strength", "range": [0, 1], "ui_range": [0, 0.3],
             "hint": "0 keeps flat, crisp colour edges. Raising it adds a fine dotted "
                     "texture; a hard metal surface usually looks better with it off."},
        ],
    },
    "presets": {
        "pixelart": [
            {"id": "default", "label": "Default", "note":
             "The colour lock and downscale settings vendored from our own "
             "sprite script, unchanged. Measured 2026-09-22.",
             "values": {"pixel_size": 64, "pixel_colors": 8, "pixel_dither": 0.0}},
            {"id": "richer-palette", "label": "Richer palette", "note":
             "More colours for a busier subject than the 8-colour default was tuned for.",
             "values": {"pixel_colors": 16}},
        ],
    },
    # R2: the same four-step ladder as the picture generator this pack rides
    # on (engines/qwen_image.py) -- no new tier invented, same measured steps.
    "quality": {
        "pixelart": [
            {"id": "draft", "label": "Draft", "why": "fastest, a bit rough",
             "values": {"steps": 12}},
            {"id": "standard", "label": "Standard", "why": "best balance",
             "values": {"steps": 20}, "default": True},
            {"id": "high", "label": "High", "why": "more detail",
             "values": {"steps": 30}},
            {"id": "max", "label": "Max", "why": "slowest, most detail",
             "values": {"steps": 40}},
        ],
    },
    # H2: "Try this" -- shape_image is optional (a free render), so no input
    # is required first.
    "examples": {
        "pixelart": [
            {"id": "pixelart-try", "label": "A pixel art sprite", "recipe": "default",
             "quality": "standard",
             "values": {"prompt": "a small knight in blue armor holding a sword"},
             "why": "a pixel art sprite from words alone", "needs": None},
        ],
    },
    "licence": {
        "name": "Qwen Research License",
        "shippable": False,
        "attribution": "Qwen-Image 2.1 by Alibaba Qwen team",
    },
}
