"""Remote Qwen-Image-2.1 R9700 API process pack.

Forge calls an existing authenticated image API.
The credential stays in a server-side file and is never returned to the browser.
"""
import os


def qwen_image(values, models):
    prompt = str(values.get("prompt") or "A detailed cinematic image").strip()
    width = int(values.get("width", 1024))
    height = int(values.get("height", 1024))
    steps = int(values.get("steps", 25))
    seed = int(values.get("seed", 42))
    return {
        "steps": [{"argv": [
            "{bin:python}", "{pack}/qwen_remote_client.py",
            "--base", os.environ.get("QWEN_IMAGE_BASE", "http://127.0.0.1:8098"),
            "--token-file", os.environ.get("QWEN_IMAGE_TOKEN_FILE", "/run/secrets/qwen-image21-r9700-token"),
            "--prompt", prompt,
            "--width", str(width),
            "--height", str(height),
            "--steps", str(steps),
            "--seed", str(seed),
            "--output", "{job}/qwen-image-r9700.png",
        ], "timeout_s": 3600}],
        "outputs": ["qwen-image-r9700.png"],
        "progress": r"PROGRESS (\d+)/(\d+)",
    }


def qwen_texture(values, models):
    values = dict(values)
    description = str(values.get("prompt") or "dark forged metal").strip()
    values["prompt"] = (
        "A perfectly seamless, tileable PBR-style material texture of " + description
        + ", orthographic flat surface, uniform lighting, no perspective, no border, no text"
    )
    return qwen_image(values, models)


FIELDS = [
    {"id": "prompt", "label": "Description", "type": "textarea", "default": "",
     "tier": "primary", "group": "Content", "order": 1},
    {"id": "width", "label": "Width", "type": "select", "default": 1024,
     "options": [512, 768, 1024, 1280, 1536, 2048], "tier": "advanced", "group": "Render", "order": 1},
    {"id": "height", "label": "Height", "type": "select", "default": 1024,
     "options": [512, 768, 1024, 1280, 1536, 2048], "tier": "advanced", "group": "Render", "order": 2},
    {"id": "steps", "label": "Steps", "type": "int", "default": 25,
     "units": "steps", "range": [1, 50], "ui_range": [10, 40], "tier": "advanced", "group": "Render", "order": 3},
]


ENGINE = {
    "id": "qwen_remote",
    "cap": "image",
    "lane_kind": "process",
    "bins": {"python": "python3"},
    "provides": {"qwen.image21-r9700": ["python"], "qwen.texture-r9700": ["python"]},
    "words": {"python": "Python 3"},
    "graphs": {"qwen.image21-r9700": qwen_image, "qwen.texture-r9700": qwen_texture},
    "describe": lambda models: "Qwen-Image-2.1 R9700 LAN API" if models.get("python") else "",
    "mode_words": {
        "qwen.image21-r9700": "R9700 picture generation",
        "qwen.texture-r9700": "Seamless Texture · R9700",
    },
    "mode_rooms": {"qwen.image21-r9700": "picture", "qwen.texture-r9700": "textures"},
    "mode_notes": {
        "qwen.image21-r9700": "Creates a new picture on the AMD Radeon AI PRO R9700.",  # source: authenticated PlexAI image service
        "qwen.texture-r9700": "Creates a seamless tileable material texture on the AMD Radeon AI PRO R9700.",  # source: authenticated PlexAI image service
    },
    "prompt_guides": {
        "qwen.image21-r9700": "Describe the picture to create, including subject, setting, lighting and composition.",  # source: authenticated Qwen image service prompt contract
        "qwen.texture-r9700": "Describe the material or surface; seamless tiling and flat lighting are added automatically.",  # source: qwen_texture prompt wrapper above
    },
    "fields": {"qwen.image21-r9700": FIELDS, "qwen.texture-r9700": FIELDS},
    "presets": {
        "qwen.image21-r9700": [
            {"id": "square", "label": "Square 1024", "values": {"width": 1024, "height": 1024, "steps": 25}},
            {"id": "landscape", "label": "Landscape", "values": {"width": 1536, "height": 1024, "steps": 25}},
            {"id": "portrait", "label": "Portrait", "values": {"width": 1024, "height": 1536, "steps": 25}},
        ],
        "qwen.texture-r9700": [
            {"id": "square", "label": "Square 1024", "values": {"width": 1024, "height": 1024, "steps": 25}},
        ],
    },
    "quality": {
        "qwen.image21-r9700": [
            {"id": "standard", "label": "Standard", "default": True, "values": {"steps": 25}},
            {"id": "high", "label": "High", "values": {"steps": 35}},
        ],
        "qwen.texture-r9700": [
            {"id": "standard", "label": "Standard", "default": True, "values": {"steps": 25}},
            {"id": "high", "label": "High", "values": {"steps": 35}},
        ],
    },
    "examples": {
        "qwen.image21-r9700": [{"id": "forge-still", "label": "A forge still life", "recipe": "square", "quality": "standard", "values": {"prompt": "A black iron gear and copper wire coil on a dark workbench, cinematic lighting, no text"}, "why": "a square detailed picture", "needs": None}],
        "qwen.texture-r9700": [{"id": "hammered-iron", "label": "Hammered black iron", "recipe": "square", "quality": "standard", "values": {"prompt": "hammered black iron with subtle scratches"}, "why": "a seamless square surface", "needs": None}],
    },
    "licence": {
        "name": "Backend model licence applies", "shippable": False,
        "summary": "Use is subject to the backend model's licence; commercial rights are not verified.",
        "attribution": "Generated by Qwen-Image-2.1 on the private R9700 backend.",
    },
}
