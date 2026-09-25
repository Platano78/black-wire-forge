"""Engine pack: 3D (TRELLIS2 image-to-mesh, MIT).

Ported from an earlier, frozen ComfyUI graph builder (not included here)
(``build_mesh3d``). That file is the single authored source of truth for
this graph -- logic and comments are copied verbatim; only the model
filenames change, from hardcoded literals to role lookups in ``m`` (the
lane's discovered files), and the Save3DAdvanced ``filename_prefix`` changes
from ``owui/mesh3d`` to ``blackwire/MESH3D`` (R7).

New cap "3d" -- its output is a .glb, not a picture (R1). Reuses the
existing "birefnet_model" role declared by engines/cleanup.py: TRELLIS2's
own first stage IS a background removal, the same model and pool, so it is
resolved once and read here via the shared ``m`` dict rather than
re-declared.
"""
import re


def mesh_graph(p, m):
    """Build a Trellis2 image-to-mesh 3D graph.

    ~30 nodes. Keeps both VAELoader nodes, UnwrapMesh (30), Save3DAdvanced (27),
    and GetMeshInfo. Drops SaveImage nodes 28 and 29.

    Parameters
    ----------
    image_filename : str
        The input image to generate a 3D mesh from.
    seed : int or None
        Random seed for the first KSampler. If None, a fresh seed is chosen.
    """
    image_filename = p["image_filename"]
    seed = p["seed"]
    return {
        "1": {
            "inputs": {"image": image_filename},
            "class_type": "LoadImage",
        },
        "2": {
            "inputs": {"bg_removal_name": m["birefnet_model"]},
            "class_type": "LoadBackgroundRemovalModel",
        },
        "3": {
            "inputs": {"bg_removal_model": ["2", 0], "image": ["1", 0]},
            "class_type": "RemoveBackground",
        },
        "4": {
            "inputs": {
                "images": ["1", 0],
                "masks": ["3", 0],
                "width": 1024,
                "height": 1024,
                "pad_factor": 1.0,
                "grow_mask": 0,
                "background": "#000000",
            },
            "class_type": "ImageCropToMask",
        },
        "5": {
            "inputs": {"clip_name": m["trellis_clip_vision"]},
            "class_type": "CLIPVisionLoader",
        },
        "6": {
            "inputs": {"clip_vision_model": ["5", 0], "image": ["4", 0]},
            "class_type": "Trellis2Conditioning",
        },
        "7": {
            "inputs": {"batch_size": 1},
            "class_type": "EmptyTrellis2LatentStructure",
        },
        "8": {
            "inputs": {
                "unet_name": m["trellis_unet"],
                "weight_dtype": "default",
            },
            "class_type": "UNETLoader",
        },
        "9": {
            "inputs": {"model": ["8", 0], "shift": 5.0},
            "class_type": "ModelSamplingSD3",
        },
        "10": {
            "inputs": {"model": ["9", 0], "cfg": 1.0, "start_percent": 0.0, "end_percent": 0.667},
            "class_type": "CFGOverride",
        },
        "11": {
            "inputs": {"model": ["10", 0], "multiplier": 0.7},
            "class_type": "RescaleCFG",
        },
        "12": {
            "inputs": {
                "model": ["11", 0],
                "seed": seed,
                "steps": 12,
                "cfg": 7.5,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "positive": ["6", 0],
                "negative": ["6", 1],
                "latent_image": ["7", 0],
            },
            "class_type": "KSampler",
        },
        "13": {
            "inputs": {"vae_name": m["trellis_shape_vae"]},
            "class_type": "VAELoader",
        },
        "14": {
            "inputs": {"samples": ["12", 0], "vae": ["13", 0], "resolution": "32"},
            "class_type": "VaeDecodeStructureTrellis2",
        },
        "15": {
            "inputs": {"positive": ["6", 0], "negative": ["6", 1], "voxel": ["14", 0]},
            "class_type": "Trellis2ShapeStage",
        },
        "16": {
            "inputs": {
                "model": ["11", 0],
                "seed": 42,
                "steps": 12,
                "cfg": 7.5,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "positive": ["15", 0],
                "negative": ["15", 1],
                "latent_image": ["15", 2],
            },
            "class_type": "KSampler",
        },
        "17": {
            "inputs": {"samples": ["16", 0], "vae": ["13", 0]},
            "class_type": "VaeDecodeShapeTrellis",
        },
        "18": {
            "inputs": {"mesh": ["17", 0]},
            "class_type": "GetMeshInfo",
        },
        "19": {
            "inputs": {"positive": ["6", 0], "negative": ["6", 1], "shape_latent": ["16", 0]},
            "class_type": "Trellis2TextureStage",
        },
        "20": {
            "inputs": {
                "model": ["11", 0],
                "seed": 42,
                "steps": 20,
                "cfg": 7.5,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "positive": ["19", 0],
                "negative": ["19", 1],
                "latent_image": ["19", 2],
            },
            "class_type": "KSampler",
        },
        "21": {
            "inputs": {"vae_name": m["trellis_texture_vae"]},
            "class_type": "VAELoader",
        },
        "22": {
            "inputs": {
                "samples": ["20", 0],
                "vae": ["21", 0],
                "shape_subdivides": ["17", 1],
            },
            "class_type": "VaeDecodeTextureTrellis",
        },
        "30": {
            "inputs": {
                "mesh": ["17", 0],
                "segmenter": "pec",
                "resolution": 1024,
                "padding": 1,
                "weld_distance": 0.0,
            },
            "class_type": "UnwrapMesh",
        },
        "23": {
            "inputs": {"mesh": ["30", 0], "voxel_colors": ["22", 0], "texture_size": 1024},
            "class_type": "BakeTextureFromVoxel",
        },
        "24": {
            "inputs": {"mesh": ["30", 0], "base_color": ["23", 0]},
            "class_type": "ApplyTextureToMesh",
        },
        "25": {
            "inputs": {"mesh": ["24", 0], "crease_angle": 60.0},
            "class_type": "MeshSmoothNormals",
        },
        "26": {
            "inputs": {"mesh": ["25", 0]},
            "class_type": "MeshToFile3D",
        },
        "27": {
            "inputs": {
                "model_3d": ["26", 0],
                "filename_prefix": "blackwire/MESH3D",
                "viewport_state": {},
                "width": 1024,
                "height": 1024,
            },
            "class_type": "Save3DAdvanced",
        },
    }


# ── the Object guide's source-picture writer (engines/__init__.py "writers") ──
# This mode takes a picture, not words, so its writer writes for the PICTURE
# room's text-to-picture mode (its `target`): the 3D step cuts the object out
# of that one picture and builds the model from it, so the picture has to
# show one whole object from a three-quarter view in soft, even light. Rules
# from guides/object/knowledge.md (source picture, NOT RIGHT? table).
_NEGATION_RE = re.compile(r"\b(no|not|without|avoid|don't|never)\b", re.IGNORECASE)
_VIEW_RE = re.compile(r"three[- ]quarter|3/4", re.IGNORECASE)


def source_picture_check(values, request):
    """The source-picture writer's check on its own drafts (its target is
    another mode, so never at Make time). -> plain problem sentences."""
    prompt = values.get("prompt") or ""
    low = prompt.lower()
    problems = []
    if not _VIEW_RE.search(prompt):
        problems.append("The prompt names no three-quarter view: a 3D model is built from this one "
                        "picture, and a three-quarter view shows two sides of the object at once.")
    if "whole" not in low and "entire" not in low:
        problems.append("The prompt does not ask for the whole object in frame: a cropped part never "
                        "reaches the model.")
    if "background" not in low:
        problems.append("The prompt names no background: ask for a plain background in a colour that "
                        "stands apart from the object, so the cutout is clean.")
    found = sorted({m.group(1).lower() for m in _NEGATION_RE.finditer(prompt)})
    if found:
        problems.append("The prompt says %s: the picture model tends to draw what is named. Keep it "
                        "positive and put what to leave out in Things to avoid." % ", ".join('"%s"' % w for w in found))
    return problems


SOURCE_PICTURE_PROMPT = (
    "You write the prompt for a picture that will be turned into a 3D model. The prompt goes to a "
    "text-to-picture model. Then the 3D step cuts the object out of that ONE picture and builds the "
    "model from it: whatever the picture does not show, the model can only guess.\n\n"
    "Reply in plain text, no JSON, no markdown, no commentary, in EXACTLY one of these two shapes.\n\n"
    "To ask:\n"
    "QUESTION: <one short question>\n"
    "OPTIONS: <2 to 5 short choices separated by |>\n\n"
    "To write:\n"
    "NEGATIVE: <things to leave out of the picture, comma-separated, without the word no>\n"
    "NOTE: <the part most likely to come out wrong in 3D and what to watch for, or the choices you made>\n"
    "PROMPT: <the finished prompt, one paragraph>\n"
    "PROMPT comes last. Write nothing after it.\n\n"
    "RULES FOR THE PROMPT\n"
    "1. ONE object, named plainly, and nothing else in the picture: no hands holding it, no stand, no "
    "second object touching it.\n"
    "2. The WHOLE object in frame, with space all round it: say \"the whole <object> in frame\".\n"
    "3. A three-quarter view, named: it shows the front and one side at once. Never a flat front view or "
    "a close-up.\n"
    "4. Soft, even studio light from all round: name it. Strong light paints its shadows onto the "
    "model's surface.\n"
    "5. A plain background in a colour that stands apart from the object (light grey for a dark object, "
    "dark grey for a white one).\n"
    "6. Name the object's colours and materials. A real product photo look (\"a clean product "
    "photograph\") unless the user asks for a style.\n"
    "7. Thin parts (a handle, a strap, a cable, an antenna, legs) are shown clearly at their full "
    "length, held away from the body, and named in NOTE as the thing to watch. Glass, mirrors and "
    "chrome come out badly: say so in NOTE, and describe a matte version unless the user insists.\n"
    "8. Positive only in PROMPT: never no, not, without, avoid or never. Hard shadows, reflections, "
    "text and other objects go in NEGATIVE.\n"
    "9. Keep every detail the user gave. 60 to 120 words.\n\n"
    "WHEN TO ASK\n"
    "Ask ONE question, only when the request does not say what the object is (\"my character\", "
    "\"something for my game\"): offer 3-4 objects that fit. Otherwise write, choosing anything left "
    "open yourself and naming it in NOTE.\n"
    "The room line in [brackets] is the form as it is now; it is not the request.\n\n"
    "EXAMPLE 1\n"
    "Request: a leather work boot\n"
    "NEGATIVE: hard shadows, reflections, text, other objects\n"
    "NOTE: The laces are thin: if they come out fused or missing, try the boot with its laces tucked in.\n"
    "PROMPT: A clean product photograph of one worn brown leather work boot, the whole boot in frame "
    "from the toe to the top of the shaft with space all round it, seen from a three-quarter view that "
    "shows the toe and the outer side. Dark brown oiled leather, a thick black rubber sole with a deep "
    "tread, tan laces tied in a neat bow. Soft, even studio light from all round. A plain light grey "
    "background.\n\n"
    "EXAMPLE 2\n"
    "Request: my character\n"
    "QUESTION: What is your character?\n"
    "OPTIONS: A knight in armour | A small robot | A wizard | A cartoon animal\n"
)


ENGINE = {
    "id": "mesh3d",
    "cap": "3d",
    "cap_word": "3D model",
    "cap_order": 4,
    "roles": {
        "trellis_unet": ("unet", {"all": ["trellis"]}),
        "trellis_shape_vae": ("vae", {"all": ["trellis", "shape"]}),
        "trellis_texture_vae": ("vae", {"all": ["trellis", "texture"]}),
        "trellis_clip_vision": ("clip_vision", {"all": ["trellis"]}),
    },
    "primary": {"mesh": "trellis_unet"},
    "cap_from": ["mesh"],
    "provides": {
        # birefnet_model is declared by engines/cleanup.py's "roles" -- the
        # same model and pool as its cutout mode. Roles are a flat namespace
        # resolved once for every lane; a pack may depend on a role it did
        # not itself declare, as long as exactly one pack owns it.
        "mesh": ["trellis_unet", "trellis_shape_vae", "trellis_texture_vae",
                 "trellis_clip_vision", "birefnet_model"],
    },
    "words": {
        "trellis_unet": "the TRELLIS2 model",
        "trellis_shape_vae": "its shape decoder",
        "trellis_texture_vae": "its texture decoder",
        "trellis_clip_vision": "its image encoder",
        "birefnet_model": "the background removal model",
    },
    "graphs": {
        "mesh": mesh_graph,
    },
    "describe": lambda models: "TRELLIS2" if models.get("trellis_unet") else "",
    "mode_words": {
        "mesh": "A picture, turned into a 3D model",
    },
    "mode_rooms": {"mesh": "3d"},
    "mode_notes": {
        "mesh": "gives a .glb file, not a picture",  # source: engines/mesh3d.py:10 (module docstring)
    },
    # P3d: "Help me write this" writes the source picture, for the Picture
    # room's text-to-picture mode (the `target`); "Use these" goes there.
    "writers": {
        "mesh": {
            "label": "Source picture writer",
            "prompt": SOURCE_PICTURE_PROMPT,
            "target": {"cap": "image", "mode": "t2i"},
            "topic_label": "What should the 3D model be of?",
            "keys": {"NEGATIVE": "negative", "PROMPT": "prompt"},
            "multiline": "PROMPT",
            "none_token": "NONE",
            "check": source_picture_check,
            "make_time": False,
        },
    },
    # R2: a picture and no prompt -- the input picker comes first, tier primary.
    "fields": {
        "mesh": [
            {"id": "image_filename", "label": "Picture", "type": "image",
             "tier": "primary", "group": "Content", "order": 1},
        ],
    },
    "presets": {
        "mesh": [
            {"id": "default", "label": "Default",
             "note": "The app's own production defaults for this pipeline.",
             "values": {}},
        ],
    },
    # R6: one tier -- the frozen builder exposes no step/quality axis to trade.
    "quality": {
        "mesh": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "the only setting there is",
             "values": {}},
        ],
    },
    # H2: "Try this" -- takes a picture the user supplies; nothing else to fill.
    "examples": {
        "mesh": [
            {"id": "mesh-try", "label": "Turn a picture into a 3D model", "recipe": "default",
             "quality": "standard", "values": {},
             "why": "turns a picture into a 3D model you can turn around",
             "needs": "picture"},
        ],
    },
    "licence": {
        # VERIFIED against the HF API license tag, microsoft/TRELLIS.2-4B:
        # "license:mit".
        "name": "MIT",
        "shippable": True,
        "attribution": "TRELLIS2 by Microsoft",
    },
}
