"""YuE2 RTX 5080 and R9700 LAN API process pack.

Opt-in: the pack exists only when YUE2_5080_BASE or YUE_R9700_BASE is set
(docs/REMOTE-BACKENDS.md). The token only ever goes to the configured address.
"""
import os

_TIMEOUT_S = 7200

# mode -> (base address variable, token file variable, default token file)
_ENV = {
    "yue2.yue2-bf16-5080": ("YUE2_5080_BASE", "YUE2_5080_TOKEN_FILE", "/run/secrets/yue2-5080-token"),
    "yue.int8-r9700": ("YUE_R9700_BASE", "YUE_R9700_TOKEN_FILE", "/run/secrets/yue-r9700-token"),
}


def _plan(mode, model, values, name):
    base_var, token_var, token_default = _ENV[mode]
    style = str(values.get("style") or "instrumental soundtrack").strip()
    lyrics = str(values.get("lyrics") or "[Instrumental]").strip()
    seed = int(values.get("seed", 7))
    return {
        "steps": [{"argv": [
            "{bin:python}", "{pack}/music_remote_client.py",
            "--base", os.environ.get(base_var, ""),
            "--base-setting", base_var,
            "--token-file", os.environ.get(token_var, token_default),
            "--token-setting", token_var,
            "--model", model,
            "--style", style,
            "--lyrics", lyrics,
            "--seed", str(seed),
            "--deadline-s", str(_TIMEOUT_S - 120),
            "--output", "{job}/%s.mp3" % name,
        ], "timeout_s": _TIMEOUT_S}],
        "outputs": ["%s.mp3" % name],
        "progress": r"PROGRESS (\d+)/(\d+)",
        "fail_marker": "ERROR: ",   # the client's one-line fatal reason (_remote_http.main_guard)
    }


def yue2_5080(values, models):
    return _plan("yue2.yue2-bf16-5080", "yue2-bf16-5080", values, "yue2-5080")


def yue_r9700(values, models):
    return _plan("yue.int8-r9700", "yue2-int8-r9700", values, "yue-r9700")


def _needs_base(mode):
    var = _ENV[mode][0]
    return lambda: (None if os.environ.get(var) else
                    "Set %s to the address of your YuE service to use this mode." % var)


_FIELDS = [
    {"id": "style", "label": "Style", "type": "textarea", "default": "",
     "tier": "primary", "group": "Song", "order": 1,
     "hint": "Genre, mood, instruments, vocals, tempo and production style."},
    {"id": "lyrics", "label": "Lyrics", "type": "textarea", "default": "[Instrumental]",
     "tier": "primary", "group": "Song", "order": 2,
     "hint": "Use [Verse], [Chorus], [Bridge] and [Outro], or [Instrumental]."},
]

ENGINE = {
    "id": "music_remote",
    "enabled": lambda: any(os.environ.get(env[0]) for env in _ENV.values()),
    "mode_deps": {mode: _needs_base(mode) for mode in _ENV},
    "cap": "audio",
    "lane_kind": "process",
    "bins": {"python": "python3"},
    "provides": {"yue2.yue2-bf16-5080": ["python"], "yue.int8-r9700": ["python"]},
    "words": {"python": "Python 3"},
    "graphs": {"yue2.yue2-bf16-5080": yue2_5080, "yue.int8-r9700": yue_r9700},
    "describe": lambda models: "YuE2 RTX 5080 + R9700 LAN APIs" if models.get("python") else "",
    "mode_words": {
        "yue2.yue2-bf16-5080": "RTX 5080 BF16 song generation",
        "yue.int8-r9700": "R9700 INT8 song generation",
    },
    "mode_rooms": {"yue2.yue2-bf16-5080": "music", "yue.int8-r9700": "music"},
    "mode_notes": {
        "yue2.yue2-bf16-5080": "Complete YuE2 song generation on the RTX 5080 BF16 backend.",  # source: verified private RTX 5080 backend contract
        "yue.int8-r9700": "Complete YuE2 song generation on the AMD Radeon AI PRO R9700 INT8 backend.",  # source: verified private R9700 backend contract
    },
    "prompt_guides": {
        "yue2.yue2-bf16-5080": "Describe genre, mood, instruments, vocals, tempo and production style.",  # source: _FIELDS style hint
        "yue.int8-r9700": "Describe genre, mood, instruments, vocals, tempo and production style.",  # source: _FIELDS style hint
    },
    "fields": {"yue2.yue2-bf16-5080": _FIELDS, "yue.int8-r9700": _FIELDS},
    "presets": {
        "yue2.yue2-bf16-5080": [{"id": "instrumental", "label": "Instrumental", "values": {"style": "cinematic instrumental soundtrack", "lyrics": "[Instrumental]"}}],
        "yue.int8-r9700": [{"id": "instrumental", "label": "Instrumental", "values": {"style": "cinematic instrumental soundtrack", "lyrics": "[Instrumental]"}}],
    },
    "quality": {
        "yue2.yue2-bf16-5080": [{"id": "standard", "label": "Standard", "default": True, "values": {}}],
        "yue.int8-r9700": [{"id": "standard", "label": "Standard", "default": True, "values": {}}],
    },
    "examples": {
        "yue2.yue2-bf16-5080": [{"id": "forge-theme", "label": "Dark forge theme", "recipe": "instrumental", "quality": "standard", "values": {"style": "dark industrial cinematic instrumental, heavy drums and distorted bass", "lyrics": "[Instrumental]"}, "why": "a complete instrumental song", "needs": None}],
        "yue.int8-r9700": [{"id": "forge-theme", "label": "Dark forge theme", "recipe": "instrumental", "quality": "standard", "values": {"style": "dark industrial cinematic instrumental, heavy drums and distorted bass", "lyrics": "[Instrumental]"}, "why": "a complete instrumental song", "needs": None}],
    },
    "licence": {
        "name": "Backend model licences apply",
        "summary": "Use is subject to the selected backend model's licence; commercial rights are not verified.",
        "shippable": False,
        "attribution": "Generated by the selected private YuE2 backend.",
    },
}
