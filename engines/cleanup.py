"""Engine pack: cleanup (BiRefNet cutout, Real-ESRGAN 4x upscale).

Ported from an earlier, frozen ComfyUI graph builder (not included here)
(``build_cutout``, ``build_upscale``). That file is the single authored
source of truth for these graphs -- logic and comments are copied verbatim;
only the model filenames change, from hardcoded literals to role lookups in
``m`` (the lane's discovered files), and the SaveImage ``filename_prefix``
changes from ``owui/...`` to ``blackwire/...`` (R7 -- the only deliberate
byte difference, matching the other packs' convention).

Cutout and upscale are TOOLS applied to a picture, not generators (owner
ruling, the internal component-inventory notes): they share the "image" cap with
qwen_image.py rather than getting their own. This pack deliberately does
not declare cap_word/cap_order -- engines.cap_word()/cap_order() return the
first pack that actually DECLARES one for a cap, so qwen-image's "picture"/1
wins regardless of pack id sort order.
"""


def cutout_graph(p, m):
    """Build a background-removal (cutout) graph.

    The InvertMask node is MANDATORY — without it the alpha comes out inverted.

    Parameters
    ----------
    image_filename : str
        The image file to remove the background from.
    """
    image_filename = p["image_filename"]
    return {
        "1": {
            "inputs": {
                "image": image_filename,
            },
            "class_type": "LoadImage",
        },
        "2": {
            "inputs": {
                "bg_removal_name": m["birefnet_model"],
            },
            "class_type": "LoadBackgroundRemovalModel",
        },
        "3": {
            "inputs": {
                "bg_removal_model": ["2", 0],
                "image": ["1", 0],
            },
            "class_type": "RemoveBackground",
        },
        "4": {
            "inputs": {
                "image": ["1", 0],
                "alpha": ["6", 0],
            },
            "class_type": "JoinImageWithAlpha",
        },
        "5": {
            "inputs": {
                "filename_prefix": "blackwire/CUTOUT",
                "images": ["4", 0],
            },
            "class_type": "SaveImage",
        },
        "6": {
            "inputs": {
                "mask": ["3", 0],
            },
            "class_type": "InvertMask",
        },
    }


def upscale_graph(p, m):
    """Build a 4x upscaling graph with RealESRGAN.

    Parameters
    ----------
    image_filename : str
        The image file to upscale.
    """
    image_filename = p["image_filename"]
    return {
        "1": {
            "inputs": {
                "image": image_filename,
            },
            "class_type": "LoadImage",
        },
        "2": {
            "inputs": {
                "model_name": m["upscale_model"],
            },
            "class_type": "UpscaleModelLoader",
        },
        "3": {
            "inputs": {
                "upscale_model": ["2", 0],
                "image": ["1", 0],
            },
            "class_type": "ImageUpscaleWithModel",
        },
        "4": {
            "inputs": {
                "filename_prefix": "blackwire/UPSCALE",
                "images": ["3", 0],
            },
            "class_type": "SaveImage",
        },
    }


ENGINE = {
    "id": "cleanup",
    "cap": "image",
    "roles": {
        # Only one file is on offer for either loader today -- the rule still
        # names the node's OWN pool (never a literal filename, R4), so a
        # future second bg-removal or upscale checkpoint sorts correctly
        # instead of silently colliding.
        "birefnet_model": ("bg_removal", {"all": []}),
        "upscale_model": ("upscale_model", {"all": []}),
    },
    "primary": {"cutout": "birefnet_model", "upscale": "upscale_model"},
    "cap_from": ["cutout", "upscale"],
    "provides": {
        "cutout": ["birefnet_model"],
        "upscale": ["upscale_model"],
    },
    "words": {
        "birefnet_model": "the background removal model",
        "upscale_model": "the upscaling model",
    },
    "graphs": {
        "cutout": cutout_graph,
        "upscale": upscale_graph,
    },
    # Always "" -- describe() is a cap-wide HEADLINE ("what generator is this
    # lane's picture cap"), and these are tools applied to a picture, not a
    # generator identity. Never shadows qwen-image's describe() for cap
    # "image", regardless of pack discovery order.
    "describe": lambda models: "",
    "mode_words": {
        "cutout": "Remove the background, fast",
        "upscale": "Make a picture 4 times bigger",
    },
    "mode_rooms": {"cutout": "cleanup", "upscale": "cleanup"},
    "mode_notes": {
        "cutout": "about 10 times faster than the picture editor's own background removal, for bulk",  # source: engines/cleanup.py:167 (cutout preset note)
        "upscale": "wrap-aware, so edges that wrap around (tiles) stay matched",  # source: engines/cleanup.py:173 (upscale preset note), our internal component notes
    },
    # R2: both modes take a picture and no prompt -- the input picker comes
    # first, tier primary, exactly as the new page renders a promptless mode.
    "fields": {
        "cutout": [
            {"id": "image_filename", "label": "Picture", "type": "image",
             "tier": "primary", "group": "Content", "order": 1},
        ],
        "upscale": [
            {"id": "image_filename", "label": "Picture", "type": "image",
             "tier": "primary", "group": "Content", "order": 1},
        ],
    },
    "presets": {
        "cutout": [
            {"id": "default", "label": "Default",
             "note": "About 10x faster than the picture editor's own background removal "
                     "(4.7s vs 50s, measured), meant for bulk cutouts.",
             "values": {}},
        ],
        "upscale": [
            {"id": "default", "label": "Default",
             "note": "The app's own production default: RealESRGAN x4plus, wrap-aware.",
             "values": {}},
        ],
    },
    # R6: one sensible setting each, no invented speed/quality axis -- neither
    # builder exposes a step count or a second checkpoint to trade against.
    "quality": {
        "cutout": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "the only setting there is",
             "values": {}},
        ],
        "upscale": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "the only setting there is",
             "values": {}},
        ],
    },
    # H2: "Try this" -- both modes work on a picture the user supplies, so
    # there is nothing left to fill once that picture is added.
    "examples": {
        "cutout": [
            {"id": "cutout-try", "label": "Remove a background", "recipe": "default",
             "quality": "standard", "values": {},
             "why": "cuts a picture's background out, fast",
             "needs": "picture"},
        ],
        "upscale": [
            {"id": "upscale-try", "label": "Make it bigger", "recipe": "default",
             "quality": "standard", "values": {},
             "why": "makes a picture four times bigger",
             "needs": "picture"},
        ],
    },
    "licence": [
        {
            # VERIFIED against the HF API license tag, ZhengPeng7/BiRefNet:
            # "license:mit".
            "name": "MIT",
            "shippable": True,
            "attribution": "BiRefNet",
            "modes": ["cutout"],
        },
        {
            # VERIFIED against the GitHub API license field for
            # xinntao/Real-ESRGAN (the upstream source of the x4plus
            # checkpoint this pack loads): BSD-3-Clause.
            "name": "BSD-3-Clause",
            "shippable": True,
            "attribution": "Real-ESRGAN by Xintao Wang et al.",
            "modes": ["upscale"],
        },
    ],
}
