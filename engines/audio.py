"""Engine pack: audio (ACE-Step 1.5 song, MiniMax-Music3, Stable Audio Open
sfx, YuE2-3B from-scratch and cover).

Ported from an earlier, frozen ComfyUI graph builder (not included here)
(``build_music``, ``build_sfx``, ``build_song``, ``build_yue2``,
``build_yue2_cover``, plus the shared ``AUDIO_FORMATS``/``AUDIO_EXT``/
``AUDIO_CONTENT_TYPE``/``audio_save_inputs``/``attach_mastering`` helpers).
That file is the single authored source of truth for these graphs (DESIGN
decision 5) -- logic and comments are copied verbatim; only the model
filenames change, from hardcoded literals to role lookups in ``m`` (the
lane's discovered files), and the SaveAudioAdvanced ``filename_prefix``
changes from ``owui/...`` to ``blackwire/...`` (R7 -- the only deliberate
byte difference, matching the other packs' convention).
"""
import random

# --- audio output format -----------------------------------------------------
# SaveAudioAdvanced's `format` is a COMFY_DYNAMICCOMBO_V3: picking a key brings
# its own sub-inputs, and the sub-input is addressed by the DOTTED name
# "format.quality" as a SIBLING key in the graph. Measured 2026-09-11 against the
# live node — the three obvious encodings (nested dict, {key: {...}}, [key, {...}])
# all PASS validation and then die at execution with "missing 1 required
# positional argument: 'format'". Validation passing is not the gate here.
AUDIO_FORMATS = {
    "flac": None,                              # lossless, no quality knob
    "mp3": ("V0", ["V0", "128k", "320k"]),     # V0 measured ~301 kbps VBR
    "opus": ("128k", ["64k", "96k", "128k", "192k", "320k"]),
}
AUDIO_EXT = {"flac": ".flac", "mp3": ".mp3", "opus": ".opus"}
AUDIO_CONTENT_TYPE = {"flac": "audio/flac", "mp3": "audio/mpeg", "opus": "audio/opus"}


def audio_save_inputs(filename_prefix, audio_ref, fmt="flac", quality=None):
    """-> the `inputs` dict for a SaveAudioAdvanced node in `fmt`.

    An unknown format falls back to flac rather than raising: a bad valve should
    cost you the container format, not the whole generation you just waited for.
    An unknown quality falls back to that format's default for the same reason.
    """
    fmt = (fmt or "flac").lower().strip()
    if fmt not in AUDIO_FORMATS:
        fmt = "flac"
    out = {"audio": audio_ref, "filename_prefix": filename_prefix, "format": fmt}
    spec = AUDIO_FORMATS[fmt]
    if spec is not None:
        default, allowed = spec
        out["format.quality"] = quality if quality in allowed else default
    return out


# --- MiniMax mastering chain --------------------------------------------------
# Generator-agnostic: AUDIO in, AUDIO out, so the same three nodes wire onto
# any of the five audio builders' VAEDecodeAudio output. Chain order is a
# ruling: EQ before dynamics, so the compressor's LUFS/limiter stage sees the
# post-EQ spectrum rather than normalising one that is about to change.
#   decode -> MiniMaxAutoEQAnalyze -> MiniMaxParametricEQ -> MiniMaxMasteringCompressor
# Mastering defaults OFF on every pipe (owner ruling): ACE-Step
# and YuE2 (2026-09-16) were ear-gated on UNMASTERED output, and mastering
# changes the sound — an ear gate can only be moved by another ear gate.
def attach_mastering(graph, audio_ref, *, enabled, target_lufs=-14.0,
                     eq_strength_percent=50.0, eq_target_mode="Reference track",
                     id_prefix="mx"):
    """Insert the MiniMax mastering chain and return the new AUDIO ref.

    Returns `audio_ref` UNCHANGED when disabled, so a disabled chain is not
    merely bypassed — the nodes are absent from the graph entirely, which is
    what keeps a disabled call byte-identical to the graph built before
    mastering existed.
    """
    if not enabled:
        return audio_ref

    analyze_id = "%s_eq_analyze" % id_prefix
    eq_id = "%s_eq" % id_prefix
    master_id = "%s_master" % id_prefix

    graph[analyze_id] = {
        "inputs": {
            "audio": audio_ref,
            "target_mode": eq_target_mode,
            "strength_percent": eq_strength_percent,
            "max_gain_db": 3,
            "max_bands": 6,
            "min_frequency_hz": 40,
            "max_frequency_hz": 16000,
        },
        "class_type": "MiniMaxAutoEQAnalyze",
    }
    graph[eq_id] = {
        "inputs": {
            "audio": audio_ref,
            "eq_settings_json": [analyze_id, 0],
            "bypass": False,
        },
        "class_type": "MiniMaxParametricEQ",
    }
    graph[master_id] = {
        "inputs": {
            "audio": [eq_id, 0],
            "bypass": False,
            "target_lufs": target_lufs,
            "ceiling_dbtp": -1,
            "target_sample_rate": "keep",
            "compressor_enabled": True,
            "threshold_db": -18,
            "ratio": 1.5,
            "knee_db": 6,
            "attack_ms": 20,
            "release_ms": 150,
            "sidechain_hz": 80,
            "detector": "RMS",
            "input_gain_db": 0,
            "max_makeup_db": 18,
            "max_limiter_reduction_db": 6,
            "lookahead_ms": 3,
            "limiter_release_ms": 100,
        },
        "class_type": "MiniMaxMasteringCompressor",
    }
    return [master_id, 0]


def _random_seed():
    return random.randint(0, 2**32 - 1)


def _seed(p):
    return p.get("seed") or _random_seed()


def _master_kwargs(p):
    return dict(
        enabled=bool(p.get("master", False)),
        target_lufs=p.get("master_target_lufs", -14.0),
        eq_strength_percent=p.get("master_eq_strength", 50.0),
        eq_target_mode=p.get("master_eq_mode", "Reference track"),
    )


# ── music (MiniMax-Music3) ─────────────────────────────────────────────────
# Source: build_music, graph_builders.py:261-378.


def music_graph(p, m):
    """Build a MiniMax Music 3 ambient music graph.

    Parameters
    ----------
    caption : str
        Positive prompt / description. 🔴 The vendor ships a PROMPT CONTRACT for
        this field — a three-section Structured Caption (Global Metadata / Vocal
        Details / Arrangement, ~250-450 words). Measured 2026-09-21: the same
        seed and lyrics with a bare caption truncated mid-phrase, and with the
        Structured Caption ended cleanly. See
        the vendor's caption-rewriter documentation.
    lyrics : str
        Words to be sung, with [Section] tags. Empty means instrumental.
        🔴 This was hardcoded to "" until 2026-09-21, which made a full-song
        vocal model behave like an ambience generator. The vendor documents
        Music3 as a five-minute song model with expressive vocals.
    seconds : float
        Duration in seconds. Default 30.0 (model's coherent span).
    seed : int or None
        Random seed. If None, a fresh seed is chosen.
    master : bool
        Insert the MiniMax mastering chain (EQ + compressor) before save.
        Off by default. See `attach_mastering`.
    """
    caption = p["caption"]
    lyrics = p.get("lyrics", "")
    seconds = p.get("seconds", 30.0)
    audio_format = p.get("audio_format", "mp3")
    audio_quality = p.get("audio_quality")
    seed = _seed(p)

    graph = {
        "1": {
            "inputs": {
                "unet_name": m["music3_unet"],
                "weight_dtype": "default",
            },
            "class_type": "UNETLoader",
        },
        "2": {
            "inputs": {
                "clip_name": m["music3_clip"],
                "type": "minimax",
            },
            "class_type": "CLIPLoader",
        },
        "3": {
            "inputs": {
                "vae_name": m["music3_vae"],
            },
            "class_type": "VAELoader",
        },
        "4": {
            "inputs": {
                "clip": ["2", 0],
                "caption": caption,
                "lyrics": lyrics,
                "seed": seed,
                "max_duration": seconds,
                "cfg_scale": 1.7,
                "top_k": 50,
            },
            "class_type": "MiniMaxMusic3TextEncode",
        },
        "5": {
            "inputs": {
                "conditioning": ["4", 0],
            },
            "class_type": "ConditioningZeroOut",
        },
        "6": {
            "inputs": {
                "seconds": seconds,
                "batch_size": 1,
            },
            "class_type": "EmptyMiniMaxMusic3LatentAudio",
        },
        "7": {
            "inputs": {
                "model": ["1", 0],
                "seed": seed,
                "steps": 30,
                "cfg": 1.7,
                "sampler_name": "euler",
                "scheduler": "simple",
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["6", 0],
                "denoise": 1.0,
            },
            "class_type": "KSampler",
        },
        "8": {
            "inputs": {
                "samples": ["7", 0],
                "vae": ["3", 0],
            },
            "class_type": "VAEDecodeAudio",
        },
        "9": {
            "inputs": audio_save_inputs("blackwire/MUSIC", ["8", 0],
                                       audio_format, audio_quality),
            "class_type": "SaveAudioAdvanced",
        },
    }
    ref = attach_mastering(graph, ["8", 0], **_master_kwargs(p))
    graph["9"]["inputs"] = audio_save_inputs("blackwire/MUSIC", ref, audio_format, audio_quality)
    return graph


# ── sfx (Stable Audio Open) ─────────────────────────────────────────────────
# Source: build_sfx, graph_builders.py:386-490.


def sfx_graph(p, m):
    """Build a Stable Audio Open sound-effects graph.

    Uses the SEPARATE t5-base CLIPLoader — NOT the checkpoint's bundled CLIP.

    Parameters
    ----------
    prompt : str
        Positive text prompt.
    negative : str
        Negative text prompt.
    seconds : float
        Duration in seconds. Default 2.0 (one-shot foley).
    seed : int or None
        Random seed. If None, a fresh seed is chosen.
    master : bool
        Insert the MiniMax mastering chain (EQ + compressor) before save.
        Off by default. See `attach_mastering`.
    """
    prompt = p["prompt"]
    negative = p.get("negative", "")
    seconds = p.get("seconds", 2.0)
    audio_format = p.get("audio_format", "mp3")
    audio_quality = p.get("audio_quality")
    seed = _seed(p)

    graph = {
        "1": {
            "inputs": {
                "ckpt_name": m["sao_ckpt"],
            },
            "class_type": "CheckpointLoaderSimple",
        },
        "2": {
            "inputs": {
                "clip_name": m["sao_clip"],
                "type": "stable_audio",
                "device": "default",
            },
            "class_type": "CLIPLoader",
        },
        "3": {
            "inputs": {
                "clip": ["2", 0],
                "text": prompt,
            },
            "class_type": "CLIPTextEncode",
        },
        "4": {
            "inputs": {
                "clip": ["2", 0],
                "text": negative,
            },
            "class_type": "CLIPTextEncode",
        },
        "5": {
            "inputs": {
                "seconds": seconds,
                "batch_size": 1,
            },
            "class_type": "EmptyLatentAudio",
        },
        "6": {
            "inputs": {
                "model": ["1", 0],
                "seed": seed,
                "steps": 50,
                "cfg": 4.98,
                "sampler_name": "dpmpp_3m_sde_gpu",
                "scheduler": "exponential",
                "positive": ["3", 0],
                "negative": ["4", 0],
                "latent_image": ["5", 0],
                "denoise": 1.0,
            },
            "class_type": "KSampler",
        },
        "7": {
            "inputs": {
                "samples": ["6", 0],
                "vae": ["1", 2],
            },
            "class_type": "VAEDecodeAudio",
        },
        "8": {
            "inputs": audio_save_inputs("blackwire/SFX", ["7", 0],
                                       audio_format, audio_quality),
            "class_type": "SaveAudioAdvanced",
        },
    }
    ref = attach_mastering(graph, ["7", 0], **_master_kwargs(p))
    graph["8"]["inputs"] = audio_save_inputs("blackwire/SFX", ref, audio_format, audio_quality)
    return graph


# ── song (ACE-Step 1.5) ──────────────────────────────────────────────────────
# Source: build_song, graph_builders.py:961-1101.


def song_graph(p, m):
    """Build an ACE-Step 1.5 song graph (vs. music_graph's MiniMax ambience beds).

    Uses DualCLIPLoader with BOTH ace_step_1.5 encoders (type="ace") — a
    single CLIPLoader is not enough for this model.

    Parameters
    ----------
    tags : str
        Style/genre description (the positive prompt).
    lyrics : str
        Lyrics with [Section] tags. Empty string == instrumental (ACE ships
        an instrumentals template for this — it is a supported mode, not a
        missing input).
    bpm : int
        Beats per minute. Default 138.
    duration : float
        Duration in seconds. Default 120.0.
    keyscale : str
        Musical key/scale, e.g. "A minor". Must be a value from the live
        TextEncodeAceStepAudio1.5 keyscale enum.
    timesignature : str
        One of "2", "3", "4", "6". Default "4".
    language : str
        Lyrics language code. Default "en".
    seed : int or None
        Random seed. If None, a fresh seed is chosen.
    master : bool
        Insert the MiniMax mastering chain (EQ + compressor) before save.
        Off by default. See `attach_mastering`.
    """
    tags = p["tags"]
    lyrics = p.get("lyrics", "")
    audio_format = p.get("audio_format", "mp3")
    audio_quality = p.get("audio_quality")
    bpm = p.get("bpm", 138)
    duration = p.get("duration", 120.0)
    keyscale = p.get("keyscale", "A minor")
    timesignature = p.get("timesignature", "4")
    language = p.get("language", "en")
    seed = _seed(p)

    graph = {
        "104": {
            "inputs": {
                "unet_name": m["ace_unet"],
                "weight_dtype": "default",
            },
            "class_type": "UNETLoader",
        },
        "105": {
            "inputs": {
                "clip_name1": m["ace_clip1"],
                "clip_name2": m["ace_clip2"],
                "type": "ace",
                "device": "default",
            },
            "class_type": "DualCLIPLoader",
        },
        "106": {
            "inputs": {
                "vae_name": m["ace_vae"],
            },
            "class_type": "VAELoader",
        },
        "94": {
            "inputs": {
                "clip": ["105", 0],
                "tags": tags,
                "lyrics": lyrics,
                "seed": seed,
                "bpm": bpm,
                "duration": duration,
                "timesignature": timesignature,
                "language": language,
                "keyscale": keyscale,
                "generate_audio_codes": True,
                "cfg_scale": 2.0,
                "temperature": 0.85,
                "top_p": 0.9,
                "top_k": 0,
                "min_p": 0.0,
            },
            "class_type": "TextEncodeAceStepAudio1.5",
        },
        "47": {
            "inputs": {
                "conditioning": ["94", 0],
            },
            "class_type": "ConditioningZeroOut",
        },
        "98": {
            "inputs": {
                "seconds": duration,
                "batch_size": 1,
            },
            "class_type": "EmptyAceStep1.5LatentAudio",
        },
        "78": {
            "inputs": {
                "model": ["104", 0],
                "shift": 3.0,
            },
            "class_type": "ModelSamplingAuraFlow",
        },
        "3": {
            "inputs": {
                "model": ["78", 0],
                "seed": seed,
                "steps": 8,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "positive": ["94", 0],
                "negative": ["47", 0],
                "latent_image": ["98", 0],
            },
            "class_type": "KSampler",
        },
        "18": {
            "inputs": {
                "samples": ["3", 0],
                "vae": ["106", 0],
            },
            "class_type": "VAEDecodeAudio",
        },
        "107": {
            "inputs": audio_save_inputs("blackwire/SONG", ["18", 0],
                                       audio_format, audio_quality),
            "class_type": "SaveAudioAdvanced",
        },
    }
    ref = attach_mastering(graph, ["18", 0], **_master_kwargs(p))
    graph["107"]["inputs"] = audio_save_inputs("blackwire/SONG", ref, audio_format, audio_quality)
    return graph


# ── yue2 (YuE2-3B, from scratch) ─────────────────────────────────────────────
# Source: build_yue2, graph_builders.py:1708-1864.
#
# "yue2_seconds" below is straight off the blueprint: a PreviewAny reading
# YuE2GenerateMusic's `seconds` output (slot 1), so the caller can read the
# actual rendered length out of /history and detect a take that hit
# `max_duration` and got cut off. yue2_truncated exists in the node's own
# conditioning metadata (yue2.py:269) but is never returned over the API
# (nodes_yue2.py:73) — this is the only way to observe it from here.


def yue2_graph(p, m):
    """Build a YuE2-3B song graph (vs. song_graph's ACE-Step 1.5).

    One `CheckpointLoaderSimple` supplies MODEL, CLIP and VAE — YuE2 does not
    need ACE-Step's separate UNETLoader + DualCLIPLoader + VAELoader.

    Parameters
    ----------
    style : str
        Style/genre prompt. YuE2's equivalent of ACE-Step's `tags`.
    lyrics : str
        Lyrics with [Section] tags.
    max_duration : float
        Upper bound in seconds. Default 300.0 — matches the pipe's
        `yue2_max_duration` valve, raised 120→180→300 on measured truncation
        (2026-09-16). 🔴 NOT the rendered length — YuE2GenerateMusic
        returns the actual `seconds` it decided on, and that output (not this
        value) feeds EmptyYuE2LatentAudio. Upstream: "Automatically reduced for
        long prompts; generation can stop earlier."
    mode : str
        "full" (melody + chords) or "melody" (melody only, recommended for
        covers). Ignored by YuE2GenerateMusic when the ABC input is empty —
        the node falls back to "off" itself.
    plan : bool
        True wires the symbolic plan (YuE2GenerateABC -> the `abc` input).
        False passes an empty ABC, which is upstream's `cot="off"`.
    seed : int or None
        One seed drives ABC, music-token generation and the sampler, exactly
        as the blueprint's single SeedNode fans out to all three.
    master : bool
        Insert the MiniMax mastering chain (EQ + compressor) before save.
        Off by default. See `attach_mastering`.

    The checkpoint role (m["yue2_ckpt"]) resolves to the BF16 build, not the
    int8 repackage, on measured evidence (2026-09-16, rig 5080, identical
    prompt + seed): int8 sampled ~21-22.5 tok/s / KSampler 2.62 it/s / peak
    7,728 MiB / RTF 1.917; bf16 sampled ~111-122 tok/s / KSampler 5.84 it/s /
    peak 9,860 MiB / RTF 0.521 (n=3). The int8 build is ~3.7x SLOWER, because
    ComfyUI logs `manual cast: torch.bfloat16` for it — the weights are
    stored int8 but dequantized per operation, with no native int8 matmul on
    this path. It buys 2.1 GB of VRAM we do not need on a 16 GB card and
    charges 3.7x in time for it, so the role rule excludes it.
    """
    style = p["style"]
    lyrics = p.get("lyrics", "")
    audio_format = p.get("audio_format", "mp3")
    audio_quality = p.get("audio_quality")
    max_duration = p.get("max_duration", 300.0)
    mode = p.get("mode", "full")
    plan = p.get("plan", True)
    seed = _seed(p)
    ckpt = m["yue2_ckpt"]

    graph = {
        "15": {
            "inputs": {
                "ckpt_name": ckpt,
            },
            "class_type": "CheckpointLoaderSimple",
        },
        "25": {
            "inputs": {
                "clip": ["15", 1],
                "style": style,
                "lyrics": lyrics,
                "abc": ["24", 0] if plan else "",
                "seed": seed,
                "mode": mode,
                "max_duration": max_duration,
                "temperature": 1.0,
                "top_p": 0.95,
                "top_k": 100,
                "repetition_penalty": 1.2,
            },
            "class_type": "YuE2GenerateMusic",
        },
        "18": {
            "inputs": {
                "conditioning": ["25", 0],
            },
            "class_type": "ConditioningZeroOut",
        },
        "5": {
            "inputs": {
                # 🔴 the model's chosen length, not `max_duration`
                "seconds": ["25", 1],
                "batch_size": 1,
            },
            "class_type": "EmptyYuE2LatentAudio",
        },
        "8": {
            "inputs": {
                "model": ["15", 0],
                "seed": seed,
                "steps": 32,
                "cfg": 1.0,
                "sampler_name": "dpm_2",
                "scheduler": "sgm_uniform",
                "denoise": 1.0,
                "positive": ["25", 0],
                "negative": ["18", 0],
                "latent_image": ["5", 0],
            },
            "class_type": "KSampler",
        },
        "9": {
            "inputs": {
                "samples": ["8", 0],
                "vae": ["15", 2],
            },
            "class_type": "VAEDecodeAudio",
        },
        "40": {
            "inputs": audio_save_inputs("blackwire/YUE2", ["9", 0],
                                        audio_format, audio_quality),
            "class_type": "SaveAudioAdvanced",
        },
        "yue2_seconds": {
            "inputs": {"source": ["25", 1]},
            "class_type": "PreviewAny",
        },
    }

    if plan:
        graph["24"] = {
            "inputs": {
                "clip": ["15", 1],
                "style": style,
                "lyrics": lyrics,
                "seed": seed,
                "mode": mode,
                "max_abc_tokens": 8192,
                "temperature": 0.7,
                "top_p": 0.9,
                "top_k": 30,
                "repetition_penalty": 1.005,
                "penalty_window": 100,
            },
            "class_type": "YuE2GenerateABC",
        }

    ref = attach_mastering(graph, ["9", 0], **_master_kwargs(p))
    graph["40"]["inputs"] = audio_save_inputs("blackwire/YUE2", ref, audio_format, audio_quality)
    return graph


# ── cover (YuE2-3B cover) ────────────────────────────────────────────────────
# Source: build_yue2_cover, graph_builders.py:1877-1999. Unlike yue2_graph,
# the symbolic score comes from the SOURCE AUDIO via SheetSage2AudioToABC
# ("55") rather than YuE2GenerateABC — a cover has no separate ABC-generation
# step, so there is no `plan` toggle.


def cover_graph(p, m):
    """Build a YuE2-3B cover graph (vs. yue2_graph's from-scratch song).

    Parameters
    ----------
    source_audio_name : str
        The ComfyUI-side filename of the uploaded source audio (the name
        `upload_image` returns, fed straight into LoadAudio's `audio`).
    style : str
        Style/genre prompt for the new arrangement.
    lyrics : str
        Lyrics with [Section] tags.
    mode : str
        "melody" (default, recommended for covers — keeps the source melody
        while letting the arrangement change) or "full" (also imposes the
        source's harmony).
    seed : int or None
        One seed drives the sampler (SheetSage2AudioToABC's transcription is
        deterministic from the source audio, not seeded).
    master : bool
        Insert the MiniMax mastering chain (EQ + compressor) before save.
        Off by default. See `attach_mastering`.

    See yue2_graph's docstring for the BF16-vs-int8 measurement behind the
    ckpt role rule.
    """
    source_audio_name = p["source_audio_name"]
    style = p["style"]
    lyrics = p.get("lyrics", "")
    audio_format = p.get("audio_format", "mp3")
    audio_quality = p.get("audio_quality")
    max_duration = p.get("max_duration", 180.0)
    mode = p.get("mode", "melody")
    seed = _seed(p)
    ckpt = m["yue2_ckpt"]

    graph = {
        "47": {
            "inputs": {"ckpt_name": ckpt},
            "class_type": "CheckpointLoaderSimple",
        },
        "56": {
            "inputs": {"audio_encoder_name": m["yue2_audio_encoder"]},
            "class_type": "AudioEncoderLoader",
        },
        "60": {
            "inputs": {"audio": source_audio_name},
            "class_type": "LoadAudio",
        },
        "55": {
            "inputs": {
                "audio_encoder": ["56", 0],
                "audio": ["60", 0],
                "mode": mode,
            },
            "class_type": "SheetSage2AudioToABC",
        },
        "48": {
            "inputs": {
                "clip": ["47", 1],
                "style": style,
                "lyrics": lyrics,
                "abc": ["55", 0],
                "seed": seed,
                "mode": mode,
                "max_duration": max_duration,
                "temperature": 1.0,
                "top_p": 0.95,
                "top_k": 100,
                "repetition_penalty": 1.2,
            },
            "class_type": "YuE2GenerateMusic",
        },
        "49": {
            "inputs": {"conditioning": ["48", 0]},
            "class_type": "ConditioningZeroOut",
        },
        "52": {
            "inputs": {
                # 🔴 the model's chosen length, not `max_duration`
                "seconds": ["48", 1],
                "batch_size": 1,
            },
            "class_type": "EmptyYuE2LatentAudio",
        },
        "51": {
            "inputs": {
                "model": ["47", 0],
                "seed": seed,
                "steps": 32,
                "cfg": 1.0,
                "sampler_name": "dpm_2",
                "scheduler": "sgm_uniform",
                "denoise": 1.0,
                "positive": ["48", 0],
                "negative": ["49", 0],
                "latent_image": ["52", 0],
            },
            "class_type": "KSampler",
        },
        "53": {
            "inputs": {"samples": ["51", 0], "vae": ["47", 2]},
            "class_type": "VAEDecodeAudio",
        },
        "58": {
            "inputs": audio_save_inputs("blackwire/COVER", ["53", 0],
                                        audio_format, audio_quality),
            "class_type": "SaveAudioAdvanced",
        },
        "yue2_seconds": {
            "inputs": {"source": ["48", 1]},
            "class_type": "PreviewAny",
        },
    }
    ref = attach_mastering(graph, ["53", 0], **_master_kwargs(p))
    graph["58"]["inputs"] = audio_save_inputs("blackwire/COVER", ref, audio_format, audio_quality)
    return graph


def _describe(models):
    """First engine present, in role-check order -- a HEADLINE, not an inventory.
    With all five present this still reports only "ACE-Step 1.5": that is the
    describe() contract (first pack, first non-empty) working as written, not
    a bug. The full list of what a lane can actually do comes from `able`
    (engines.abilities()), which carries all five mode flags independently --
    the AUDIO tab must read THAT for the real inventory, not this string."""
    for role, label in (("ace_unet", "ACE-Step 1.5"), ("music3_unet", "MiniMax-Music3"),
                        ("sao_ckpt", "Stable Audio Open"), ("yue2_ckpt", "YuE2-3B")):
        if models.get(role):
            return label
    return ""


ENGINE = {
    "id": "audio",
    "cap": "audio",
    "cap_word": "sound",
    "cap_order": 3,
    "roles": {
        "ace_unet": ("unet", {"all": ["ace_step_1.5"]}),
        "ace_clip1": ("clip", {"all": ["ace_step_1.5", "0.6b"]}),
        "ace_clip2": ("clip", {"all": ["ace_step_1.5", "1.7b"]}),
        "ace_vae": ("vae", {"all": ["ace_step_1.5"]}),
        "music3_unet": ("unet", {"all": ["minimax_music3"]}),
        "music3_clip": ("clip", {"all": ["minimax_music3"]}),
        "music3_vae": ("vae", {"all": ["minimax_music3"]}),
        "sao_ckpt": ("checkpoint", {"all": ["stable-audio-open"]}),
        "sao_clip": ("clip", {"all": ["stable-audio-open"]}),
        "yue2_ckpt": ("checkpoint", {"all": ["yue2"], "none": ["int8"]}),
        "yue2_audio_encoder": ("audio_encoder", {"all": ["sheetsage2"]}),
    },
    "primary": {
        "song": "ace_unet", "music": "music3_unet", "sfx": "sao_ckpt",
        "yue2": "yue2_ckpt", "cover": "yue2_ckpt",
    },
    "cap_from": ["song", "music", "sfx", "yue2", "cover"],
    "provides": {
        "song": ["ace_unet", "ace_clip1", "ace_clip2", "ace_vae"],
        "music": ["music3_unet", "music3_clip", "music3_vae"],
        "sfx": ["sao_ckpt", "sao_clip"],
        "yue2": ["yue2_ckpt"],
        "cover": ["yue2_ckpt", "yue2_audio_encoder"],
    },
    "words": {
        "ace_unet": "the ACE-Step song model",
        "ace_clip1": "its 0.6B text encoder",
        "ace_clip2": "its 1.7B text encoder",
        "ace_vae": "its audio decoder",
        "music3_unet": "the MiniMax-Music3 model",
        "music3_clip": "its text encoder",
        "music3_vae": "its audio decoder",
        "sao_ckpt": "the Stable Audio Open model",
        "sao_clip": "its t5-base text encoder",
        "yue2_ckpt": "the YuE2-3B model",
        "yue2_audio_encoder": "the SheetSage2 audio encoder",
    },
    "graphs": {
        "song": song_graph,
        "music": music_graph,
        "sfx": sfx_graph,
        "yue2": yue2_graph,
        "cover": cover_graph,
    },
    "describe": _describe,
    # -- R1: the pack describes its own form (DESIGN decision 7 / R1). Five
    # modes with wildly different inputs, so the UI renders whatever it is
    # told rather than knowing any of these fields by name. `default` values
    # are copied from the FROZEN graph_builders.py signatures (R2) -- see the
    # per-mode comments below for what each one is measured against.
    #
    # Deliberate exception: `master` (attach_mastering's on/off switch) is NOT
    # declared here as a per-mode field, even though every graph function above
    # accepts it. It behaves identically across all five modes, so the UI (a
    # fixed checkbox, not one rebuilt from this manifest) renders it once
    # instead of five duplicate declarations that would drift independently.
    "mode_words": {
        "song": "A song with words",
        "music": "Background music, with or without singing",
        "sfx": "A short sound effect",
        "yue2": "A song, built from a symbolic music plan",
        "cover": "A cover version of a song you upload",
    },
    "mode_rooms": {
        "song": "music", "music": "music", "yue2": "music",
        "cover": "cover", "sfx": "sfx",
    },
    "mode_notes": {
        "song": "renders quickly, with a clean ending every time",  # source: engines/audio.py:971 (song preset note)
        "music": "clearest vocals and ambience; needs the full length to end cleanly",  # source: our internal component notes, engines/audio.py:1045 (quality why)
        "sfx": "one-shot sounds, measured at 2 to 3 seconds",  # source: engines/audio.py:990, engines/audio.py:993 (sfx preset notes)
        "yue2": "plans the melody first for a more coherent take",  # source: engines/audio.py:999 (yue2 preset note)
        "cover": "keeps the tune of your song while the arrangement changes",  # source: engines/audio.py:1009 (cover preset note)
    },
    # L5: how a prompt must be written for each mode, drawn only from this
    # pack's own docstrings/field hints -- never a new claim.
    "prompt_guides": {
        "song": "Style/genre, mood and instrumentation (the positive prompt). "  # source: engines/audio.py:359-360 (tags docstring)
                "Lyrics go in their own field with [Section] tags; leave lyrics empty for an instrumental. "  # source: engines/audio.py:878-881 (field hints)
                "Return only the comma-separated style/genre tags. Do not write lyrics.",
        "music": "This model needs a three-section Structured Caption (Global "  # source: engines/audio.py:150-156 (music_graph docstring)
                 "Metadata / Vocal Details / Arrangement), about 250-450 words -- a bare caption truncates mid-phrase.",
        "sfx": "A short, one-shot sound description; this mode is tuned for 2-3 second effects.",  # source: engines/audio.py:990,993 (sfx preset notes)
        "yue2": "Style/genre prompt (YuE2's equivalent of ACE-Step's tags). "  # source: engines/audio.py:516-517 (yue2_graph docstring)
                "Lyrics go in their own field with [Section] tags. "
                "Return only the style/genre tags. Do not write lyrics.",
        "cover": "Style/genre prompt for the NEW arrangement -- the tune comes "  # source: engines/audio.py:669 (cover_graph docstring)
                 "from the uploaded source track, not from this text. Lyrics have their own field. "
                 "Return only the style/genre text. Do not write lyrics.",
    },
    # R1: field descriptors carry curation now (tier/group/order/units/range/
    # ui_range/enabled_when), not just content -- see the pack contract
    # docstring in engines/__init__.py for what each key means.
    "fields": {
        "song": [
            {"id": "tags", "label": "Style / genre", "type": "text",
             "tier": "primary", "group": "Content", "order": 1,
             "hint": "The positive prompt, genre, mood, instrumentation."},
            {"id": "lyrics", "label": "Lyrics", "type": "textarea", "default": "",
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "[Section] tags supported. Leave empty for an instrumental. "
                     "This is a supported mode with its own template, not a missing input."},
            # "int" not "number": the frozen builder's `bpm: int = 138` -- a float
            # bpm would drift the graph from the single authored source (R2).
            {"id": "bpm", "label": "BPM", "type": "int", "default": 138,
             "tier": "primary", "group": "Sound", "order": 1,
             "units": "BPM", "range": [40, 220], "ui_range": [70, 160]},
            {"id": "duration", "label": "Duration (seconds)", "type": "number",
             "default": 120.0, "tier": "primary", "group": "Sound", "order": 2,
             "units": "seconds", "range": [5, 300], "ui_range": [60, 180]},
            {"id": "keyscale", "label": "Key / scale", "type": "text", "default": "A minor",
             "tier": "advanced", "group": "Sound", "order": 3,
             "hint": "e.g. \"A minor\". Must be a real musical key."},
            {"id": "timesignature", "label": "Time signature", "type": "select", "default": "4",
             "options": ["2", "3", "4", "6"], "tier": "advanced", "group": "Sound", "order": 4},
            {"id": "language", "label": "Lyrics language", "type": "text", "default": "en",
             "tier": "advanced", "group": "Sound", "order": 5},
        ],
        "music": [
            {"id": "caption", "label": "Description", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1,
             "hint": "This model wants a Structured Caption "
                     "(Global Metadata / Vocal Details / Arrangement). A bare caption "
                     "truncated mid-phrase, the structured form ended cleanly. Measured 2026-09-21."},
            {"id": "lyrics", "label": "Lyrics", "type": "textarea", "default": "",
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "Empty means instrumental ambience; the model can also sing a full song."},
            {"id": "seconds", "label": "Duration (seconds)", "type": "number",
             "default": 30.0, "tier": "primary", "group": "Sound", "order": 1,
             "units": "seconds", "range": [5, 300], "ui_range": [15, 60],
             "hint": "30s is the model's coherent span."},
        ],
        "sfx": [
            {"id": "prompt", "label": "Sound", "type": "text",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "negative", "label": "Avoid", "type": "text", "default": "",
             "tier": "advanced", "group": "Content", "order": 2},
            {"id": "seconds", "label": "Duration (seconds)", "type": "number",
             "default": 2.0, "tier": "primary", "group": "Sound", "order": 1,
             "units": "seconds", "range": [0.5, 30], "ui_range": [1, 5],
             "hint": "2s is a one-shot foley default."},
        ],
        "yue2": [
            {"id": "style", "label": "Style / genre", "type": "text",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "lyrics", "label": "Lyrics", "type": "textarea", "default": "",
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "[Section] tags supported."},
            # Raised 120->180->300 on measured truncation (2026-09-16).
            {"id": "max_duration", "label": "Max duration (seconds)", "type": "number",
             "default": 300.0, "tier": "advanced", "group": "Sound", "order": 1,
             "units": "seconds", "range": [30, 300], "ui_range": [120, 300],
             "hint": "Upper bound, not the rendered length. The model can stop earlier."},
            {"id": "mode", "label": "Mode", "type": "select", "default": "full",
             "options": ["full", "melody"], "tier": "primary", "group": "Sound", "order": 2,
             "hint": "\"melody\" is recommended when you plan to use this as a cover source later.",
             # Real conditional: YuE2GenerateMusic ignores `mode` when the ABC
             # input is empty and falls back to "off" itself (see yue2_graph's
             # docstring) -- so this only does anything when a plan was built.
             "enabled_when": {"field": "plan", "equals": True},
             "disabled_reason": "Only used when planning the melody first -- without a "
                                 "symbolic plan the model decides the arrangement itself "
                                 "and ignores this."},
            {"id": "plan", "label": "Plan the melody first (slower, more coherent)",
             "type": "checkbox", "default": True, "tier": "primary", "group": "Sound", "order": 3},
        ],
        "cover": [
            {"id": "source_audio_name", "label": "Source track", "type": "audio",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "style", "label": "New style / genre", "type": "text",
             "tier": "primary", "group": "Content", "order": 2},
            {"id": "lyrics", "label": "Lyrics", "type": "textarea", "default": "",
             "tier": "primary", "group": "Content", "order": 3,
             "hint": "[Section] tags supported."},
            {"id": "mode", "label": "Mode", "type": "select", "default": "melody",
             "options": ["melody", "full"], "tier": "primary", "group": "Sound", "order": 1,
             "hint": "\"melody\" keeps the source's tune while changing the arrangement; "
                     "\"full\" also imposes the source's harmony."},
        ],
    },
    # R3: named parameter sets, the Krita/Fooocus pole -- a preset absorbs
    # complexity, the visible form shows only the per-generation delta. Each
    # note cites where the setting was measured; none are invented.
    "presets": {
        "song": [
            # Full citation: 150s duration rendered in 52s (RTF 0.35) with a clean
            # ending -- MIT, safe to ship without thought. Measured 2026-09-21 shootout.
            {"id": "full-verse", "label": "Full song (150s)",
             "note": "Renders quickly with a clean ending every time. Measured 2026-09-21.",
             "values": {"duration": 150.0}},
            {"id": "instrumental", "label": "Instrumental",
             "note": "This pipeline ships a dedicated instrumentals template. An empty "
                     "lyrics field is a supported mode, not a missing input.",
             "values": {"lyrics": ""}},
        ],
        "music": [
            {"id": "structured-caption-full", "label": "Full song (Structured Caption)",
             "note": "With the vendor's three-section Structured Caption, 150s renders "
                     "with a clean ending and the clearest lyrics of the pipelines tried. "
                     "A bare caption truncated 2/2 at the same length. Measured 2026-09-21.",
             "values": {"seconds": 150.0}},
            {"id": "ambient-bed", "label": "Ambient bed (default span)",
             "note": "30s is the model's own coherent span for an instrumental bed.",
             "values": {"seconds": 30.0, "lyrics": ""}},
        ],
        "sfx": [
            {"id": "one-shot", "label": "One-shot foley",
             "note": "2s is the default one-shot foley length.",
             "values": {"seconds": 2.0}},
            {"id": "longer-effect", "label": "Longer effect",
             "note": "3s asked returned 3.0s, rendered in 16s. Measured 2026-09-21 "
                     "(a door-slam take).",
             "values": {"seconds": 3.0}},
        ],
        "yue2": [
            {"id": "planned-full", "label": "Plan first, full arrangement",
             "note": "Planning the melody before generating gives a more coherent take; "
                     "\"full\" also imposes harmony from the plan.",
             "values": {"plan": True, "mode": "full", "max_duration": 300.0}},
            {"id": "quick-melody", "label": "Quick sketch, melody only",
             "note": "A fast draft take. Skipping the plan step is faster and "
                     "melody-only is recommended for a later cover source.",
             "values": {"plan": False, "max_duration": 120.0}},
        ],
        "cover": [
            {"id": "keep-melody", "label": "Keep the melody (recommended)",
             "note": "\"melody\" keeps the source's tune while letting the arrangement "
                     "change. The documented default for covers.",
             "values": {"mode": "melody"}},
            {"id": "reimagine", "label": "Reimagine harmony too",
             "note": "\"full\" also imposes the source's harmony onto the new arrangement.",
             "values": {"mode": "full"}},
        ],
    },
    # R1/R2: quality ladders, ONLY from evidence (our internal measurement
    # notes) -- no invented
    # tiers. Where cost genuinely scales with duration, the tiers are
    # duration points; where only one setting is measured to work, there is
    # one tier. See each mode's comment for its source.
    "quality": {
        # ACE-Step's flat-cost window is 60-240s at RTF~0.30 (full-test);
        # below it fixed overhead dominates (20s = RTF 0.72), above it cost
        # grows superlinearly (360s = RTF 0.41, 600s = RTF 0.53). 150s is
        # also the "full-verse" preset's own measured point (52s render,
        # clean ending, MIT). Standard is the sweet-spot middle, not the top.
        "song": [
            {"id": "sketch", "label": "Sketch", "why": "fastest, good for trying an idea",
             "values": {"duration": 60.0}},
            {"id": "standard", "label": "Standard", "default": True,
             "why": "best balance, the measured sweet spot",
             "values": {"duration": 150.0}},
            {"id": "long", "label": "Long take",
             "why": "as long as it stays efficient, longer starts costing much more per second",
             "values": {"duration": 240.0}},
        ],
        # Music3 truncates mid-phrase at both measured short lengths (30s
        # and 90s, 2/2 abrupt endings, full-test-2026-09-21.md) -- no
        # shorter duration is known to end cleanly, so there is exactly one
        # tier rather than a guessed short one. 150s is the one setting
        # that measured a clean ending (structured-caption-full preset).
        "music": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "the only length measured to end cleanly, shorter cuts off mid-phrase",
             "values": {"seconds": 150.0}},
        ],
        # Both points measured directly (full-test / shootout 2026-09-21):
        # the 2s one-shot default and a 3s door-slam take.
        "sfx": [
            {"id": "quick", "label": "Quick", "default": True,
             "why": "a one-shot foley length",
             "values": {"seconds": 2.0}},
            {"id": "longer", "label": "Longer",
             "why": "a bit more room for the sound to play out",
             "values": {"seconds": 3.0}},
        ],
        # Same duration+plan pair as the "quick-melody"/"planned-full"
        # presets above: plan=False stays under the length where truncation
        # was observed (measurements-2026-09-21.md); plan=True + more room
        # is what resolved cleanly at 360s vs cutting off at 90s
        # (full-test-2026-09-21.md).
        "yue2": [
            {"id": "sketch", "label": "Sketch", "why": "fast draft, skips the melody plan",
             "values": {"plan": False, "max_duration": 120.0}},
            {"id": "standard", "label": "Standard", "default": True,
             "why": "plans the melody first and leaves it room to finish cleanly",
             "values": {"plan": True, "max_duration": 300.0}},
        ],
        # No duration/step axis is measured for covers -- the real lever
        # here is the "mode" field above (melody vs full harmony), which is
        # already its own primary control, not a speed/quality trade-off.
        # One tier, no override, so the mode still satisfies "every mode has
        # a ladder" without inventing a fake one.
        "cover": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "no separate speed/quality lever is measured for covers",
             "values": {}},
        ],
    },
    # H2: "Try this" -- every mode but cover takes only text; cover needs a
    # source track uploaded first.
    "examples": {
        "song": [
            {"id": "song-try", "label": "A warm pop song", "recipe": "full-verse",
             "quality": "standard",
             "values": {"tags": "warm acoustic pop, gentle drums, sunny afternoon",
                        "lyrics": "[Verse]\nSunlight on the water,\neasy days go by.\n"
                                  "[Chorus]\nHold on to this feeling,\nunder an open sky."},
             "why": "a full song with a clean ending", "needs": None},
        ],
        "music": [
            {"id": "music-try", "label": "Background music with vocals", "recipe": "structured-caption-full",
             "quality": "standard",
             "values": {"caption": "Global Metadata: warm ambient pop. Vocal Details: soft, "
                                    "breathy female vocals. Arrangement: gentle piano intro "
                                    "building to a full band chorus."},
             "why": "background music with clear vocals", "needs": None},
        ],
        "sfx": [
            {"id": "sfx-try", "label": "A short sound", "recipe": "one-shot", "quality": "quick",
             "values": {"prompt": "a wooden door creaking open"},
             "why": "a short one-shot sound", "needs": None},
        ],
        "yue2": [
            {"id": "yue2-try", "label": "A planned song", "recipe": "planned-full", "quality": "standard",
             "values": {"style": "uplifting orchestral folk",
                        "lyrics": "[Verse]\nWe walked along the shoreline,\nchasing the fading light."},
             "why": "a song planned from its melody first", "needs": None},
        ],
        "cover": [
            {"id": "cover-try", "label": "A new take on a song", "recipe": "keep-melody", "quality": "standard",
             "values": {"style": "slow acoustic ballad"},
             "why": "a new take on a song you already have, same tune", "needs": "sound"},
        ],
    },
    "licence": [
        {
            # VERIFIED against the HF API license tag on both ACE-Step/Ace-Step1.5
            # and ACE-Step/acestep-v15-xl-turbo (the checkpoint this pack loads):
            # both report "license:mit". DESIGN.md's "MIT (ACE-Step)" was the doc
            # this disconfirms the "Apache-2.0" first guess with, not a source.
            "name": "MIT",
            "shippable": True,
            "attribution": "ACE-Step 1.5 by ACE Studio and StepFun",
            "modes": ["song"],
        },
        {
            # VERIFIED against MiniMaxAI/MiniMax-Music3/LICENSE on HF: a
            # "MiniMax-Music3 COMMUNITY LICENSE" (permissive below $20M/yr
            # revenue, gated above it) that REQUIRES prominently displaying
            # "MiniMax-Music3" on any commercial UI -- clause 3.1, the same
            # obligation open since 2026-09-09. shippable is False
            # here despite the permissive-under-a-cap terms: this field means
            # "ship with no gate", and the revenue cap is a gate.
            "name": "MiniMax-Music3 Community License",
            "shippable": False,
            "attribution": "MiniMax-Music3",
            "modes": ["music"],
        },
        {
            "name": "Stability AI Community License",
            "shippable": False,
            "attribution": "Powered by Stability AI",
            "modes": ["sfx"],
        },
        {
            "name": "CC BY-NC 4.0",
            "shippable": False,
            "attribution": "YuE2-3B — non-commercial use only",
            "modes": ["yue2", "cover"],
        },
    ],
}
