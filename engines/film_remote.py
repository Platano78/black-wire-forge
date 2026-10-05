"""Remote Film5080 and Film9700 video API process pack.

Opt-in: the pack exists only when FILM5080_BASE or FILM9700_BASE is set
(docs/REMOTE-BACKENDS.md). It invokes your own Film generation services with
a local stdlib client; no backend credentials are sent to the browser or
stored in jobs, and the token only ever goes to the configured address.
"""
import os

_TIMEOUT_S = 21600


def _plan(env, model, values, name, seed_max=None):
    base_var, token_var, token_default = env
    prompt = str(values.get("prompt") or "A cinematic video").strip()
    seed = int(values.get("seed", 42))
    if seed_max is not None:
        seed %= seed_max + 1
    duration = int(values.get("duration", 10))
    image = values.get("image")
    argv = [
        "{bin:python}", "{pack}/film_remote_client.py",
        "--base", os.environ.get(base_var, ""),
        "--base-setting", base_var,
        "--token-file", os.environ.get(token_var, token_default),
        "--token-setting", token_var,
        "--model", model,
        "--prompt", prompt,
        "--seed", str(seed),
        "--duration", str(duration),
        "--deadline-s", str(_TIMEOUT_S - 120),
        "--output", "{job}/%s.mp4" % name,
    ]
    if image:
        argv += ["--image", "{in:image}"]
    return {
        "steps": [{"argv": argv, "timeout_s": _TIMEOUT_S}],
        "outputs": ["%s.mp4" % name],
        "progress": r"PROGRESS (\d+)/(\d+)",
        "fail_marker": "ERROR: ",   # the client's one-line fatal reason (_remote_http.main_guard)
    }


# (base address variable, token file variable, default token file) per mode.
_ENV = {
    "film5080": ("FILM5080_BASE", "FILM5080_TOKEN_FILE", "/run/secrets/film5080-token"),
    "film9700": ("FILM9700_BASE", "FILM9700_TOKEN_FILE", "/run/secrets/film9700-token"),
}


def film5080(values, models):
    # Whether the service is up is the client's first step (one short probe
    # with a plain sentence), never the graph build's: building a plan must
    # not wait on the network.
    return _plan(_ENV["film5080"], "MiniMax-H3-RYZN9-Preview", values,
                 "film5080", seed_max=2147483647)


def film9700(values, models):
    return _plan(_ENV["film9700"], "Wan2.2-Video-Uncensored-R9700", values, "film9700")


def _needs_base(mode):
    var = _ENV[mode][0]
    return lambda: (None if os.environ.get(var) else
                    "Set %s to the address of your Film service to use this mode." % var)


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
    "enabled": lambda: any(os.environ.get(env[0]) for env in _ENV.values()),
    "mode_deps": {mode: _needs_base(mode) for mode in _ENV},
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
