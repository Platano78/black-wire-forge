"""Remote Film5080 and Film9700 video API process pack.

The Forge remains on the Proxmox guest. This pack invokes the two existing
LAN generation APIs with a local stdlib client; no backend credentials are
sent to the browser or stored in jobs.
"""
import os
import urllib.request


def _plan(base, token_file, model, values, name, seed_max=None):
    prompt = str(values.get("prompt") or "A cinematic video").strip()
    seed = int(values.get("seed", 42))
    if seed_max is not None:
        seed %= seed_max + 1
    duration = int(values.get("duration", 10))
    image = values.get("image")
    argv = [
        "{bin:python}", "{pack}/film_remote_client.py",
        "--base", base,
        "--token-file", token_file,
        "--model", model,
        "--prompt", prompt,
        "--seed", str(seed),
        "--duration", str(duration),
        "--output", "{job}/%s.mp4" % name,
    ]
    if image:
        argv += ["--image", "{in:image}"]
    return {
        "steps": [{"argv": argv, "timeout_s": 21600}],
        "outputs": ["%s.mp4" % name],
        "progress": r"PROGRESS (\d+)/(\d+)",
    }


def _alive(base, token_file):
    """True when the backend answers /health. Any failure means not available."""
    token = ""
    try:
        if token_file and os.path.isfile(token_file):
            with open(token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
        req = urllib.request.Request(
            base.rstrip("/") + "/health",
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def film5080(values, models):
    base = os.environ.get("FILM5080_BASE", "http://127.0.0.1:8094")
    token_file = os.environ.get("FILM5080_TOKEN_FILE", "/run/secrets/film5080-token")
    # ponytail: the owner picks the GPU in the Video room, so a dead 5080 is
    # refused at submit (one 5s probe) instead of silently rerouted to the
    # R9700, whose Wan audio rejects speech minutes later.
    if not _alive(base, token_file):
        raise ValueError("The RTX 5080 video backend is offline right now. "
                         "Start it, or pick R9700 (no speech or lip-sync).")
    return _plan(base, token_file, "MiniMax-H3-RYZN9-Preview", values,
                 "film5080", seed_max=2147483647)


def film9700(values, models):
    return _plan(
        os.environ.get("FILM9700_BASE", "http://127.0.0.1:8093"),
        os.environ.get("FILM9700_TOKEN_FILE", "/run/secrets/film9700-token"),
        "Wan2.2-Video-Uncensored-R9700", values, "film9700")


_FIELDS = [
    {"id": "prompt", "label": "Scene and action", "type": "textarea", "default": "",
     "tier": "primary", "group": "Content", "order": 1,
     "hint": "Describe the shot, action, sound and any spoken words."},
    {"id": "image", "label": "Starting image (optional)", "type": "image",
     "tier": "primary", "group": "Content", "order": 2},
    {"id": "duration", "label": "Duration", "type": "select", "default": 10,
     "options": [10], "tier": "advanced", "group": "Render", "order": 1,
     "units": "seconds", "hint": "10 seconds (supported by both Film backends)."},
]

ENGINE = {
    "id": "film_remote",
    "cap": "video",
    "lane_kind": "process",
    "bins": {"python": "python3"},
    "provides": {"film5080": ["python"], "film9700": ["python"]},
    "words": {"python": "Python 3"},
    "graphs": {"film5080": film5080, "film9700": film9700},
    "describe": lambda models: "Film5080 + Film9700 LAN APIs" if models.get("python") else "",
    "mode_words": {
        "film5080": "RTX 5080 video with native audio",
        "film9700": "R9700 video with scene audio",
    },
    "mode_rooms": {"film5080": "video", "film9700": "video"},
    "mode_notes": {
        "film5080": "Creates a video from words or a starting picture, with native speech and audio.",  # source: verified private RTX 5080 backend contract
        "film9700": "Creates a fast video with validated scene audio.",  # source: verified private R9700 backend contract
    },
    "prompt_guides": {
        "film5080": "Describe the shot, action, sound and any spoken words.",  # source: _FIELDS prompt hint
        "film9700": "Describe the shot, action, sound and any spoken words.",  # source: _FIELDS prompt hint
    },
    "fields": {"film5080": _FIELDS, "film9700": _FIELDS},
    "presets": {
        "film5080": [{"id": "default", "label": "10 second clip", "values": {"duration": 10}}],
        "film9700": [{"id": "default", "label": "10 second Wan clip", "values": {"duration": 10}}],
    },
    "quality": {
        "film5080": [{"id": "standard", "label": "Standard", "default": True, "values": {"duration": 10}}],
        "film9700": [{"id": "fast", "label": "Fast", "default": True, "values": {"duration": 10}}],
    },
    "examples": {
        "film5080": [{"id": "forge-coil", "label": "A glowing wire coil", "recipe": "default", "quality": "standard", "values": {"prompt": "A copper wire coil glows on a black iron workbench, locked camera, no text"}, "why": "a short locked-camera shot with native audio", "needs": None}],
        "film9700": [{"id": "forge-gear", "label": "A turning iron gear", "recipe": "default", "quality": "fast", "values": {"prompt": "A black iron gear turns slowly on a dark workbench, locked camera, no text"}, "why": "a short locked-camera shot with scene audio", "needs": None}],
    },
    "licence": {
        "name": "Backend model licences apply",
        "summary": "Use is subject to the selected backend model's licence; commercial rights are not verified.",
        "shippable": False,
        "attribution": "Generated by the selected private Film backend.",
    },
}
