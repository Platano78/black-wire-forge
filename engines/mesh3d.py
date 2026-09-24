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
