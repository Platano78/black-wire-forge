"""Engine pack: LTX-2.5 (text/image to video with joint audio, long single-take
looping, and a single talking-head piece).

Ported from an earlier, frozen ComfyUI graph builder (not included here) --
``build_ltx_video``, ``build_ltx_video_looping``, plus the module's ``LTX25_*``
constants and ``_random_seed`` -- and one more frozen script from the same
source, for ``talking_head_prompt``.
Logic and comments are copied verbatim; only the model filenames change, from
hardcoded literals to role lookups in ``m`` (the lane's discovered files), and
the SaveVideo ``filename_prefix`` changes from ``video/...`` to
``blackwire/...`` (matching the audio pack's own R7 convention -- the only
deliberate byte difference).

R3 (owner ruling): ``plan_talking_head`` plans a CHAIN of pieces -- multi-job
orchestration needing sequence machinery that does not exist yet (slice C3).
This pack ports ONE piece only: ``talking`` mode renders a single talking-head
clip from a face picture and a line of dialogue, built on the same
``_ltx_graph`` as ``ltx`` (i2v, joint audio) with the prompt composed by
``talking_head_prompt``. Chaining pieces together is deferred to C3, not
built here.
"""
import math
import random
import re

# -- constants, copied verbatim from graph_builders.py:1120-1129 -----------
LTX25_STAGE1_SIGMAS = "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"
LTX25_STAGE2_SIGMAS = "0.85, 0.7250, 0.4219, 0.0"
LTX25_NEGATIVE = "pc game, console game, video game, cartoon, childish, ugly"


def _random_seed() -> int:
    return random.randint(0, 2**32 - 1)


# -- talking-head prompt, copied verbatim from forge_video.py:1645-1666 ----
def talking_head_prompt(line: str, look: str = "") -> str:
    """Compose the LTX prompt for one talking-head piece.

    The shape is the one that worked: name the spoken words explicitly in quotes,
    then ask for synced mouth movement and a clean close-mic voice. Without the
    lip-sync clause the model is happy to generate the audio over a still face.
    """
    # `look` is whatever the user wrote OUTSIDE the quotes, so it is often an
    # instruction ("make her say ...") rather than a description. Using it as the
    # grammatical subject produced "make her say with warm lighting looks directly
    # into the camera". It rides at the END as a shot note instead, where a stray
    # imperative is harmless.
    line = " ".join((line or "").split())
    shot = " ".join((look or "").split())
    tail = f" Shot: {shot}." if shot else ""
    if not line:
        return ("The person in the image looks directly into the camera, listening quietly. "
                "Subtle head motion, natural blinking. Quiet room tone, no speech, no music."
                + tail)
    return ('The person in the image looks directly into the camera and speaks. '
            f'They say clearly: "{line}" '
            "Their mouth moves in sync with the words, natural lip movement, subtle head "
            "motion, natural blinking. Clean studio voice, close microphone, quiet room "
            "tone, no music." + tail)


# -- ltx / talking (build_ltx_video) ----------------------------------------
# Source: build_ltx_video, graph_builders.py:1132-1465.


def _ltx_graph(p, m, filename_prefix):
    """Build an LTX-2.5 text/image-to-video graph with joint (not dubbed) audio.

    Stage 1 makes an empty video latent and an empty audio latent, concatenates
    them, and samples them JOINTLY. Stage 2 upscales the video latent 2x and
    refines. Both stages run euler_ancestral through LTXVDualCFGGuider at
    CFG 1/1 on a fixed distilled sigma schedule -- the distilled checkpoint is
    trained for that and other values degrade it.

    Parameters (from ``p``, defaults matching the frozen builder's own signature)
    ----------
    width, height : int
        Stage-1 latent size; must be divisible by 32. With ``two_stage`` the
        delivered video is 2x this. Defaults 1024x576.
    length : int
        Frame count. Must satisfy ``length % 8 == 1``. Default 97.
    two_stage : bool
        False runs Stage 1 only -- lower quality, roughly half the VRAM.
    context_length : int or None
        None samples the whole clip in one pass. An int instead samples in
        overlapping temporal windows of that many real frames, so peak VRAM is
        set by the WINDOW, not by the clip. Must satisfy ``context_length % 8
        == 1``.
    audio : bool
        True generates picture and sound jointly. False drops the whole audio
        branch, which is the ONLY way past 993 frames -- LTXVEmptyLatentAudio
        caps at 1000 frames and rejects more at submit.
    start_image : str or None
        Filename already uploaded to ComfyUI's input dir. Conditions generation
        on it as the first frame (image-to-video). None is plain text-to-video.
    image_strength : float
        Stage-1 conditioning strength. 0.7 is the official i2v template's value;
        stage 2 re-conditions at 1.0, as the template does.
    end_image : str or None
        Filename already uploaded to ComfyUI's input dir. When set, enters FLF2V
        (guided morph) mode: both `start_image` and `end_image` are installed as
        chained `LTXVAddGuide` anchors (frame_idx 0 and -1) at the SAME strength
        (`image_strength`). Requires `start_image`. Incompatible with `two_stage`.
    """
    prompt = p["prompt"]
    width = p.get("width", 1024)
    height = p.get("height", 576)
    length = p.get("length", 97)
    fps = p.get("fps", 24)
    seed = p.get("seed")
    negative = p.get("negative", LTX25_NEGATIVE)
    two_stage = p.get("two_stage", True)
    context_length = p.get("context_length")
    context_overlap = p.get("context_overlap", 40)
    context_schedule = p.get("context_schedule", "standard_uniform")
    closed_loop = p.get("closed_loop", False)
    audio = p.get("audio", True)
    start_image = p.get("start_image")
    image_strength = p.get("image_strength", 0.7)
    img_compression = p.get("img_compression", 18)
    end_image = p.get("end_image")

    if width % 32 or height % 32:
        raise ValueError(f"width and height must be divisible by 32, got {width}x{height}")
    if length % 8 != 1:
        raise ValueError(f"length must satisfy length % 8 == 1, got {length}")
    if context_length is not None and context_length % 8 != 1:
        raise ValueError(
            f"context_length must satisfy context_length % 8 == 1, got {context_length}")
    if context_length is not None and context_overlap >= context_length:
        raise ValueError(
            f"context_overlap ({context_overlap}) must be smaller than "
            f"context_length ({context_length})")
    if start_image is not None and not (0.0 <= image_strength <= 1.0):
        raise ValueError(f"image_strength must be in [0,1], got {image_strength}")
    if end_image is not None and start_image is None:
        raise ValueError("end_image requires start_image -- a morph needs both endpoints")
    if end_image is not None and two_stage:
        raise ValueError(
            "end_image (FLF2V morph) is incompatible with two_stage: (a) stage 2's "
            "LTXVImgToVideoInplace (node 349) re-stamps the start image, reintroducing "
            "the duplicated-subject defect this mode fixes; (b) measured on this rig, "
            "1024x576 two_stage times out (>360s, peak 15883 MiB) while single-stage "
            "completes in 163.7s. Pass two_stage=False.")
    if audio and length > 993:
        raise ValueError(
            f"length {length} exceeds the joint-audio ceiling: LTXVEmptyLatentAudio "
            f"caps at 1000 frames, so 993 is the largest value that is also 8n+1. "
            f"Pass audio=False for anything longer.")
    if seed is None:
        seed = _random_seed()

    # With a start image, stage 1 samples a latent already conditioned on it, and
    # the official i2v template re-conditions again at stage 2 (strength 1.0)
    # rather than trusting the 2x upscale to carry the frame through.
    # FLF2V (end_image set) replaces that mechanism entirely: the conditioned
    # latent comes chained off the second LTXVAddGuide instead.
    # The slot travels with the source: LTXVAddGuide returns
    # ['CONDITIONING','CONDITIONING','LATENT'], so its latent is slot 2, unlike
    # LTXVImgToVideoInplace/EmptyLTXVLatentVideo which return LATENT only (slot 0).
    if end_image:
        _s1_video_src, _s1_video_slot = "flf_guide_b", 2
    elif start_image:
        _s1_video_src, _s1_video_slot = "357", 0
    else:
        _s1_video_src, _s1_video_slot = "356", 0

    g = {
        # -- loaders --
        "384": {
            "inputs": {"unet_name": m["ltx_transformer"]},
            "class_type": "UnetLoaderGGUF",
        },
        "385": {
            "inputs": {"vae_name": m["ltx_vae_video"]},
            "class_type": "VAELoader",
        },

        "387": {
            "inputs": {"clip_name": m["ltx_clip"], "type": "ltxv"},
            "class_type": "CLIPLoader",
        },
        # -- conditioning --
        "364": {
            "inputs": {"text": prompt, "clip": ["387", 0]},
            "class_type": "CLIPTextEncode",
        },
        "373": {
            "inputs": {"text": negative, "clip": ["387", 0]},
            "class_type": "CLIPTextEncode",
        },
        "365": {
            "inputs": {
                "positive": ["364", 0],
                "negative": ["373", 0],
                "frame_rate": float(fps),
            },
            "class_type": "LTXVConditioning",
        },
        # -- stage 1: empty video + empty audio, concatenated, sampled jointly --
        "356": {
            "inputs": {
                "width": width,
                "height": height,
                "length": length,
                "batch_size": 1,
            },
            "class_type": "EmptyLTXVLatentVideo",
        },

        "339": {"inputs": {"noise_seed": seed}, "class_type": "RandomNoise"},
        "388": {
            "inputs": {
                "model": ["384", 0],
                "positive": ["365", 0],
                "negative": ["365", 1],
                "video_cfg": 1.0,
                "audio_cfg": 1.0,
            },
            "class_type": "LTXVDualCFGGuider",
        },
        "352": {
            "inputs": {"sampler_name": "euler_ancestral"},
            "class_type": "KSamplerSelect",
        },
        "404": {
            "inputs": {"sigmas": LTX25_STAGE1_SIGMAS},
            "class_type": "ManualSigmas",
        },
        "344": {
            "inputs": {
                "noise": ["339", 0],
                "guider": ["388", 0],
                "sampler": ["352", 0],
                "sigmas": ["404", 0],
                "latent_image": ["377", 0] if audio else [_s1_video_src, _s1_video_slot],
            },
            "class_type": "SamplerCustomAdvanced",
        },
    }

    if audio:
        g["386"] = {"inputs": {"vae_name": m["ltx_vae_audio"]}, "class_type": "VAELoader"}
        g["366"] = {
            "inputs": {
                "frames_number": length,
                "frame_rate": float(fps),
                "batch_size": 1,
                "audio_vae": ["386", 0],
            },
            "class_type": "LTXVEmptyLatentAudio",
        }
        g["377"] = {
            "inputs": {"video_latent": [_s1_video_src, _s1_video_slot], "audio_latent": ["366", 0]},
            "class_type": "LTXVConcatAVLatent",
        }
        g["367"] = {"inputs": {"av_latent": ["344", 0]}, "class_type": "LTXVSeparateAVLatent"}

    # video latent off stage 1, whether or not audio rode along with it
    s1_video = ("367", 0) if audio else ("344", 0)

    if end_image:
        # FLF2V: both endpoints installed as chained LTXVAddGuide anchors, resized
        # to the latent's exact dims first (an unresized source against a
        # mismatched-ratio latent is the anchor-centre-crop-amputates-the-face
        # defect). This REPLACES the plain i2v LTXVImgToVideoInplace mechanism.
        g["flf_load_a"] = {"inputs": {"image": start_image}, "class_type": "LoadImage"}
        g["flf_scale_a"] = {
            "inputs": {
                "image": ["flf_load_a", 0],
                "upscale_method": "area",
                "width": width,
                "height": height,
                "crop": "center",
            },
            "class_type": "ImageScale",
        }
        g["flf_pre_a"] = {"inputs": {"image": ["flf_scale_a", 0], "img_compression": img_compression},
                          "class_type": "LTXVPreprocess"}
        g["flf_guide_a"] = {
            "inputs": {
                "positive": ["365", 0],
                "negative": ["365", 1],
                "vae": ["385", 0],
                "latent": ["356", 0],
                "image": ["flf_pre_a", 0],
                "frame_idx": 0,
                "strength": image_strength,
            },
            "class_type": "LTXVAddGuide",
        }
        g["flf_load_b"] = {"inputs": {"image": end_image}, "class_type": "LoadImage"}
        g["flf_scale_b"] = {
            "inputs": {
                "image": ["flf_load_b", 0],
                "upscale_method": "area",
                "width": width,
                "height": height,
                "crop": "center",
            },
            "class_type": "ImageScale",
        }
        g["flf_pre_b"] = {"inputs": {"image": ["flf_scale_b", 0], "img_compression": img_compression},
                          "class_type": "LTXVPreprocess"}
        g["flf_guide_b"] = {
            "inputs": {
                "positive": ["flf_guide_a", 0],
                "negative": ["flf_guide_a", 1],
                "vae": ["385", 0],
                "latent": ["flf_guide_a", 2],
                "image": ["flf_pre_b", 0],
                "frame_idx": -1,
                "strength": image_strength,
            },
            "class_type": "LTXVAddGuide",
        }
        # the guider samples off the chained (guide_b) conditioning, not the raw
        # LTXVConditioning output -- that is what anchors the temporal guides.
        g["388"]["inputs"]["positive"] = ["flf_guide_b", 0]
        g["388"]["inputs"]["negative"] = ["flf_guide_b", 1]
    elif start_image:
        g["300"] = {"inputs": {"image": start_image}, "class_type": "LoadImage"}
        g["350"] = {"inputs": {"image": ["300", 0], "img_compression": img_compression},
                    "class_type": "LTXVPreprocess"}
        g["357"] = {
            "inputs": {
                "vae": ["385", 0],
                "image": ["350", 0],
                "latent": ["356", 0],
                "strength": image_strength,
                "bypass": False,
            },
            "class_type": "LTXVImgToVideoInplace",
        }

    if two_stage:
        # -- stage 2: 2x spatial upscale of the video latent, then refine --
        g["371"] = {
            "inputs": {"model_name": m["ltx_upscaler"]},
            "class_type": "LatentUpscaleModelLoader",
        }
        g["348"] = {
            "inputs": {
                "samples": list(s1_video),
                "upscale_model": ["371", 0],
                "vae": ["385", 0],
            },
            "class_type": "LTXVLatentUpsampler",
        }
        _s2_video_src = "348"
        if start_image:
            g["349"] = {
                "inputs": {
                    "vae": ["385", 0],
                    "image": ["350", 0],
                    "latent": ["348", 0],
                    "strength": 1.0,
                    "bypass": False,
                },
                "class_type": "LTXVImgToVideoInplace",
            }
            _s2_video_src = "349"
        if audio:
            g["340"] = {
                "inputs": {"video_latent": [_s2_video_src, 0], "audio_latent": ["367", 1]},
                "class_type": "LTXVConcatAVLatent",
            }
        g["338"] = {"inputs": {"noise_seed": seed}, "class_type": "RandomNoise"}
        g["391"] = {
            "inputs": {
                "model": ["384", 0],
                "positive": ["365", 0],
                "negative": ["365", 1],
                "video_cfg": 1.0,
                "audio_cfg": 1.0,
            },
            "class_type": "LTXVDualCFGGuider",
        }
        g["341"] = {
            "inputs": {"sampler_name": "euler_ancestral"},
            "class_type": "KSamplerSelect",
        }
        g["395"] = {
            "inputs": {"sigmas": LTX25_STAGE2_SIGMAS},
            "class_type": "ManualSigmas",
        }
        g["368"] = {
            "inputs": {
                "noise": ["338", 0],
                "guider": ["391", 0],
                "sampler": ["341", 0],
                "sigmas": ["395", 0],
                "latent_image": ["340" if audio else _s2_video_src, 0],
            },
            "class_type": "SamplerCustomAdvanced",
        }
        if audio:
            g["369"] = {"inputs": {"av_latent": ["368", 0]},
                        "class_type": "LTXVSeparateAVLatent"}
        video_out = ("369", 0) if audio else ("368", 0)
        audio_out = ("369", 1) if audio else None
    else:
        video_out = s1_video
        audio_out = ("367", 1) if audio else None

    if end_image:
        # Crops the guide frames back off the sampled latent before decode, so
        # the anchor frames themselves never reach the delivered video.
        g["flf_crop"] = {
            "inputs": {
                "positive": ["flf_guide_b", 0],
                "negative": ["flf_guide_b", 1],
                "latent": list(video_out),
            },
            "class_type": "LTXVCropGuides",
        }
        video_out = ("flf_crop", 2)

    # -- decode + mux --
    g["374"] = {
        "inputs": {
            "samples": list(video_out),
            "vae": ["385", 0],
            "tile_size": 512,
            "overlap": 64,
            "temporal_size": 64,
            "temporal_overlap": 16,
        },
        "class_type": "VAEDecodeTiled",
    }
    if audio:
        g["358"] = {
            "inputs": {"samples": list(audio_out), "audio_vae": ["386", 0]},
            "class_type": "LTXVAudioVAEDecode",
        }
    if context_length is not None:
        # Wraps the MODEL so sampling runs in overlapping temporal windows.
        # Both guiders take the wrapped model; nothing else in the graph moves.
        g["394"] = {
            "inputs": {
                "model": ["384", 0],
                "context_length": context_length,
                "context_overlap": context_overlap,
                "context_schedule": context_schedule,
                "context_stride": 1,
                "closed_loop": closed_loop,
                "fuse_method": "pyramid",
                "freenoise": True,
                "retain_first_frame": False,
                "split_conds_to_windows": False,
            },
            "class_type": "LTXVContextWindows",
        }
        for guider in ("388", "391"):
            if guider in g:
                g[guider]["inputs"]["model"] = ["394", 0]

    g["370"] = {
        "inputs": {"images": ["374", 0], "fps": float(fps)},
        "class_type": "CreateVideo",
    }
    if audio:
        g["370"]["inputs"]["audio"] = ["358", 0]
    g["75"] = {
        "inputs": {
            "video": ["370", 0],
            "filename_prefix": filename_prefix,
            "format": "auto",
            "codec": "auto",
        },
        "class_type": "SaveVideo",
    }
    return g


def _check_window_overlap(window, overlap):
    """B2: LTXVContextWindows (and LTXVLoopingSampler, the same shape under
    different kwarg names) both require overlap < window, and the field
    defaults do not enforce that against whatever window a person actually
    picks -- context_overlap defaults to 40, but context_length's own
    ui_range starts at 9 (our internal LTX research notes measure the WINDOW
    size itself, up to ~121 frames / 41s; no measured overlap-to-window
    RATIO exists to
    derive a scaled default from). So this is a plain refusal, not a silent
    clamp -- inventing a ratio with no citation would be a guess, not a
    measurement, and every other preset/default in this pack is one.

    Called BEFORE `_ltx_graph`'s own frozen-equivalent check (R2 verbatim,
    never edited for this) so the message a person actually sees names what
    they set on the page, not raw Python kwargs -- B1 now surfaces this
    straight to them as a plain 400.
    """
    if window is not None and overlap is not None and overlap >= window:
        raise ValueError("The overlap must be shorter than the window.")


def ltx_graph(p, m):
    _check_window_overlap(p.get("context_length"), p.get("context_overlap", 40))
    return _ltx_graph(p, m, "blackwire/LTX")


def talking_graph(p, m):
    """One talking-head piece: a face picture + a line of dialogue -> one clip.

    Builds on the same i2v/joint-audio graph as ``ltx_graph`` -- `face` becomes
    `start_image`, and the prompt is composed by ``talking_head_prompt`` instead
    of taken verbatim from the user. Chaining several pieces into a longer
    script (the frozen ``plan_talking_head``/``generate_video_chain`` machinery)
    is deferred to C3 -- see the module docstring.
    """
    inner = dict(p)
    inner["prompt"] = talking_head_prompt(p.get("line", ""), p.get("look", ""))
    inner["start_image"] = p.get("face")
    inner["audio"] = True
    return _ltx_graph(inner, m, "blackwire/TALKING")


# -- ltx_loop (build_ltx_video_looping) --------------------------------------
# Source: build_ltx_video_looping, graph_builders.py:1563-1682.


def _ltx_loop_graph(p, m):
    """Long video via LTXVLoopingSampler (Lightricks/ComfyUI-LTXVideo), video-only.

    A different mechanism from ``context_length`` on ``ltx_graph``. Context
    windows wrap the MODEL and window the sampling of one latent; this node owns
    the sampling loop itself, takes the VAE, and carries autoregressive
    conditioning between tiles (``temporal_overlap_cond_strength``), AdaIn
    normalisation against drift (``adain_factor``), and optional per-tile prompts.

    Audio is not wired: the node predates LTX-2.5's AV-concatenated latents and no
    shipped example exercises it on 2.5.
    """
    prompt = p["prompt"]
    width = p.get("width", 768)
    height = p.get("height", 512)
    length = p.get("length", 993)
    fps = p.get("fps", 24)
    seed = p.get("seed")
    negative = p.get("negative", LTX25_NEGATIVE)
    temporal_tile_size = p.get("temporal_tile_size", 80)
    temporal_overlap = p.get("temporal_overlap", 24)
    temporal_overlap_cond_strength = p.get("temporal_overlap_cond_strength", 0.5)
    adain_factor = p.get("adain_factor", 0.0)

    if width % 32 or height % 32:
        raise ValueError(f"width and height must be divisible by 32, got {width}x{height}")
    if length % 8 != 1:
        raise ValueError(f"length must satisfy length % 8 == 1, got {length}")
    if temporal_overlap >= temporal_tile_size:
        raise ValueError(
            f"temporal_overlap ({temporal_overlap}) must be smaller than "
            f"temporal_tile_size ({temporal_tile_size})")
    if seed is None:
        seed = _random_seed()

    return {
        "384": {"inputs": {"unet_name": m["ltx_transformer"]}, "class_type": "UnetLoaderGGUF"},
        "385": {"inputs": {"vae_name": m["ltx_vae_video"]}, "class_type": "VAELoader"},
        "387": {
            "inputs": {"clip_name": m["ltx_clip"], "type": "ltxv"},
            "class_type": "CLIPLoader",
        },
        "364": {"inputs": {"text": prompt, "clip": ["387", 0]}, "class_type": "CLIPTextEncode"},
        "373": {"inputs": {"text": negative, "clip": ["387", 0]}, "class_type": "CLIPTextEncode"},
        "365": {
            "inputs": {
                "positive": ["364", 0],
                "negative": ["373", 0],
                "frame_rate": float(fps),
            },
            "class_type": "LTXVConditioning",
        },
        "356": {
            "inputs": {"width": width, "height": height, "length": length, "batch_size": 1},
            "class_type": "EmptyLTXVLatentVideo",
        },
        "339": {"inputs": {"noise_seed": seed}, "class_type": "RandomNoise"},
        "352": {"inputs": {"sampler_name": "euler_ancestral"}, "class_type": "KSamplerSelect"},
        "404": {"inputs": {"sigmas": LTX25_STAGE1_SIGMAS}, "class_type": "ManualSigmas"},
        # core CFGGuider at 1.0 -- the distilled checkpoint's trained setting
        "390": {
            "inputs": {
                "model": ["384", 0],
                "positive": ["365", 0],
                "negative": ["365", 1],
                "cfg": 1.0,
            },
            "class_type": "CFGGuider",
        },
        "500": {
            "inputs": {
                "model": ["384", 0],
                "vae": ["385", 0],
                "noise": ["339", 0],
                "sampler": ["352", 0],
                "sigmas": ["404", 0],
                "guider": ["390", 0],
                "latents": ["356", 0],
                "temporal_tile_size": temporal_tile_size,
                "temporal_overlap": temporal_overlap,
                "guiding_strength": 1.0,
                "temporal_overlap_cond_strength": temporal_overlap_cond_strength,
                "cond_image_strength": 1.0,
                "horizontal_tiles": 1,
                "vertical_tiles": 1,
                "spatial_overlap": 1,
                "adain_factor": adain_factor,
            },
            "class_type": "LTXVLoopingSampler",
        },
        "374": {
            "inputs": {
                "samples": ["500", 0],
                "vae": ["385", 0],
                "tile_size": 512,
                "overlap": 64,
                "temporal_size": 64,
                "temporal_overlap": 16,
            },
            "class_type": "VAEDecodeTiled",
        },
        "370": {
            "inputs": {"images": ["374", 0], "fps": float(fps)},
            "class_type": "CreateVideo",
        },
        "75": {
            "inputs": {
                "video": ["370", 0],
                "filename_prefix": "blackwire/LTX_LOOP",
                "format": "auto",
                "codec": "auto",
            },
            "class_type": "SaveVideo",
        },
    }


def ltx_loop_graph(p, m):
    """Thin wrapper: B2's window/overlap pre-check, then the frozen-equivalent
    body above unchanged. Same bug shape as ltx_graph's context_length/
    context_overlap -- temporal_overlap defaults to 24, temporal_tile_size's
    own ui_range starts at 24 (see _check_window_overlap), so a small tile
    size alone can trip the same LTXVLoopingSampler requirement."""
    _check_window_overlap(p.get("temporal_tile_size", 80), p.get("temporal_overlap", 24))
    return _ltx_loop_graph(p, m)


# ── writers and a fixer (the Motion guide's skills; engines/__init__.py "writers"/"revisers") ──
# Rules restated from this pack's own field hints and docstrings above and
# guides/motion/knowledge.md (LTX's published prompt contract: one paragraph,
# present tense, 1-2 actions, no token weights; the talking-head line sizing).

LTX_FPS = 24

# Talking Head line sizing (guides/motion/knowledge.md "Sizing the line to the
# clip's length"): about 2.2 spoken words a second, plus a 15% cushion, then
# up to the next length the model takes (8n+1 frames). Never shorter than the
# field's 97-frame default, the one length measured on this hardware.
TALKING_WORDS_PER_SECOND = 2.2
TALKING_CUSHION = 1.15
TALKING_MIN_FRAMES = 97
TALKING_MAX_FRAMES = 361    # the field's own ui_range top: "About 4 to 15 seconds"

_WORD_RE = re.compile(r"[A-Za-z0-9À-ɏ']+")
# "(dog:1.3)", "((snow))", "[wind]": token weights and brackets the model reads as words.
_WEIGHT_RE = re.compile(r"\([^()]*:\s*\d+(?:\.\d+)?\s*\)|\(\([^()]*\)\)|\[[^\[\]]*\]")


def spoken_words(line):
    """The number of spoken words in a line (a dash or an emoji is not one)."""
    return len(_WORD_RE.findall(line or ""))


def frames_up(frames):
    """The smallest length the model takes (8n+1) that is at least `frames`."""
    frames = max(1, int(math.ceil(frames)))
    return 8 * int(math.ceil((frames - 1) / 8.0)) + 1


def talking_frames(words, fps=LTX_FPS):
    """The Talking Head length for a line of `words` spoken words."""
    seconds = words / TALKING_WORDS_PER_SECOND * TALKING_CUSHION
    return max(TALKING_MIN_FRAMES, frames_up(seconds * fps))


def talking_max_words(fps=LTX_FPS):
    """The most words one clip of TALKING_MAX_FRAMES holds."""
    n = 0
    while talking_frames(n + 1, fps) <= TALKING_MAX_FRAMES:
        n += 1
    return n


def _grid_problems(values, request):
    """Length and size the graph refuses (8n+1 frames, multiples of 32).
    Only while the writer drafts (its request carries the topic): at Make
    time the graph's own refusal stands, since confirming cannot help."""
    if "topic" not in (request or {}):
        return []
    out = []
    length = values.get("length")
    if isinstance(length, int) and length % 8 != 1:
        out.append("Length is %d frames, but this model only takes 8n+1 frames (%d or %d)."
                   % (length, frames_up(length) - 8, frames_up(length)))
    for fid, label in (("width", "Width"), ("height", "Height")):
        v = values.get(fid)
        if isinstance(v, int) and v % 32:
            out.append("%s is %d, but it must be a multiple of 32 (%d or %d)." % (label, v, v // 32 * 32, v // 32 * 32 + 32))
    return out


def _weight_problems(text):
    found = _WEIGHT_RE.findall(text or "")
    if not found:
        return []
    return ["The prompt has token weights or brackets (%s): this model reads them as words. Write plain "
            "sentences instead." % ", ".join(found[:3])]


def shot_check(values, request):
    """The ltx / ltx_loop writer's check, also the Make-time guard: token
    weights in the prompt, and (while drafting) the length/size grid."""
    return _weight_problems(values.get("prompt")) + _grid_problems(values, request)


_LOOK_SPEECH_RE = re.compile(r"\b(say|says|saying|said|speak|speaks|speaking|tell|tells|telling)\b", re.IGNORECASE)


# "10 seconds", "6s", "97 frames", "1 minute": a length the user stated.
# Quoted text is the words to be spoken, never a length.
_QUOTED_RE = re.compile(r'"[^"]*"|\u201c[^\u201d]*\u201d')
_STATED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(frames?|seconds?|secs?|s|minutes?|mins?)\b", re.IGNORECASE)


def stated_frames(text, fps=LTX_FPS):
    """The length a user stated in `text`, as frames the model takes
    (8n+1), or None. Units: frames, seconds, minutes."""
    m = _STATED_RE.search(_QUOTED_RE.sub(" ", text or ""))
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2).lower()
    if unit.startswith("f"):
        return frames_up(n)
    return frames_up(n * fps * (60 if unit.startswith("m") else 1))


def talking_derive(values, request):
    """The Talking Head length. The brain never writes it (a small brain
    counts words unreliably): a length the user stated in the request or an
    answer wins, else the sizing formula on the written line."""
    fps = values.get("fps") or LTX_FPS
    req = request or {}
    text = " ".join([req.get("topic") or "", req.get("answer") or ""]
                    + [a.get("a", "") for a in req.get("answers") or []])
    stated = stated_frames(text, fps)
    if stated is not None and 9 <= stated <= 993:
        return {"length": stated}
    words = spoken_words(values.get("line"))
    return {"length": talking_frames(words, fps)} if words else {}


def talking_check(values, request):
    """The Talking Head writer's check, also the Make-time guard: the line
    must fit the clip, the shot note must not carry speech, and (while
    drafting) the length must be one the model takes."""
    out = _grid_problems(values, request)
    fps = values.get("fps") or LTX_FPS
    line = values.get("line") or ""
    words = spoken_words(line)
    length = values.get("length")
    if words and isinstance(length, int):
        need = talking_frames(words, fps)
        if need > TALKING_MAX_FRAMES and length < need:
            out.append("The line is %d words and needs about %.0f seconds, longer than one clip holds (%d frames, "
                       "about %.0f seconds, fits about %d words). Shorten it, or split it across two clips."
                       % (words, need / float(fps), TALKING_MAX_FRAMES, TALKING_MAX_FRAMES / float(fps),
                          talking_max_words(fps)))
        elif length < need:
            out.append("The line is %d words and needs about %.1f seconds, but Length is %d frames (%.1f seconds), "
                       "so it would be cut off. Set Length to %d frames."
                       % (words, need / float(fps), length, length / float(fps), need))

    look = values.get("look") or ""
    if _LOOK_SPEECH_RE.search(look) or '"' in look:
        out.append("The shot note (%s) is about lighting and framing; the spoken words go in the line." % look)
    return out


def _shot_writer_prompt(sound, max_frames):
    """The ltx (sound) / ltx_loop (silent long take) writer's system prompt."""
    table = ", ".join("%d s: %d" % (s, frames_up(s * LTX_FPS)) for s in (2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 41)
                      if frames_up(s * LTX_FPS) <= max_frames)
    what = ("one video shot WITH SOUND: picture and sound are made together from your words"
            if sound else "one long continuous take with NO SOUND at all")
    return (
        "You write the prompt for a video model that makes %s, from a short request. The model follows a "
        "shot described the way a camera operator reads a shot list, not a poem.\n\n"
        "Reply in plain text, no JSON, no markdown, no commentary, in EXACTLY one of these two shapes.\n\n"
        "To ask:\n"
        "QUESTION: <one short question>\n"
        "OPTIONS: <2 to 4 short choices separated by |>\n\n"
        "To write:\n"
        "LENGTH: <NONE, unless the request itself gives a number of seconds: then frames from the table below>\n"
        "WIDTH: <NONE, unless the request gives an exact size in pixels: then a multiple of 32>\n"
        "HEIGHT: <NONE, unless the request gives an exact size in pixels: then a multiple of 32>\n"
        "NOTE: <only when you chose something the user did not say, such as the camera: name each choice>\n"
        "PROMPT: <the finished prompt, one paragraph>\n"
        "PROMPT comes last. Write nothing after it.\n\n"
        "RULES FOR THE PROMPT\n"
        "1. ONE paragraph in the present tense, in this order: the subject and how it looks; what it does; "
        "the camera; the setting; the light%s.\n"
        "2. At most TWO actions. A third action is silently dropped by the model.\n"
        "3. The camera: ONE move or a still camera, in plain words (\"the camera slowly pushes in\", \"the "
        "camera follows from the side\", \"a still, wide shot\").\n"
        "4. Show feelings by posture, gesture and face (\"shoulders slumped, eyes down\"), never by labels "
        "(\"sad\").\n"
        "5. ONE light source (\"low sun from the left\", \"a single desk lamp\").\n"
        "6. Every person or animal you name gets something to do and a place in the frame. Name no one the "
        "user did not ask for.\n"
        "7. NEVER write token weights, brackets, tag lists or quality words (\"(dog:1.3)\", \"[snow]\", "
        "\"masterpiece\", \"4k\").\n"
        "8. Keep every subject, name, colour and detail the user gave.\n"
        "%s"
        "\nLENGTH stays NONE when the request gives no number of seconds: the form keeps its own length%s. "
        "LENGTH is in frames at 24 a second, and the model only takes these (seconds: frames): %s. For "
        "another duration: seconds x 24, then up to the next number in the table's pattern (one more than a "
        "multiple of 8). At most %d frames.\n"
        "The picture is widescreen. If the user asks for a tall or square video, keep the prompt as it is, "
        "leave WIDTH and HEIGHT at NONE, and say in NOTE that a tall or square frame comes out with distorted "
        "motion on this model.\n\n"
        "WHEN TO ASK\n"
        "Most requests need no question: write. Ask ONE question only in the cases below, when neither the "
        "request nor an answer already says it. Ask the first of these that applies:\n"
        "a. ACTION: ONLY when nothing happens in the request at all (\"a lighthouse\", \"a city street\"). "
        "Offer 3-4 things that could happen. Any verb (burning, rolling, runs, pours) is an action: do not "
        "ask this.\n"
        "b. CAMERA: the request has an action but says nothing about the camera. Offer 2-4 camera choices "
        "that suit it, such as follow it, hold wide, push in slowly.\n"
        "Never ask for more detail about an action the request already gives. Once the user has answered a "
        "question, write; choose anything still open yourself and name it in NOTE.\n"
        "The room line in [brackets] is the form as it is now; it is not the request.\n\n"
        "EXAMPLE 1\n"
        "Request: a paper boat on a stream\n"
        "QUESTION: What should the boat do?\n"
        "OPTIONS: Drift slowly downstream | Spin in an eddy | Tip over a small fall\n\n"
        "EXAMPLE 2\n"
        "Request: a paper boat on a stream\n"
        "The user answered: Drift slowly downstream\n"
        "LENGTH: NONE\n"
        "WIDTH: NONE\n"
        "HEIGHT: NONE\n"
        "NOTE: I chose a still camera low at the water's edge.\n"
        "PROMPT: %s\n\n"
        "EXAMPLE 3\n"
        "Request: 6 seconds of an old man feeding pigeons on a park bench, the camera slowly pushes in\n"
        "LENGTH: %d\n"
        "WIDTH: NONE\n"
        "HEIGHT: NONE\n"
        "PROMPT: %s\n"
    ) % (what,
         ", and the sound: what is heard, with any spoken words in quotation marks" if sound else
         ". This take has no sound, so write nothing about sound",
         "" if sound else "9. It is one long unbroken take: steady, continuous motion suits it; no cuts and no "
         "scene changes.\n",
         "" if sound else " (a long take by default)", table, max_frames,
         ("A small white paper boat drifts slowly downstream on a calm, clear stream, turning gently as it "
          "goes. A still camera sits low at the water's edge. Smooth pebbles and green reeds line the banks, "
          "lit by soft afternoon sun from the left." + (" Water trickles and burbles softly, a bird calls "
                                                        "in the distance." if sound else "")),
         frames_up(6 * LTX_FPS),
         ("An old man in a grey wool coat sits on a wooden park bench and tosses breadcrumbs to a cluster of "
          "pigeons at his feet, smiling as they peck. The camera slowly pushes in toward him. Autumn trees "
          "stand behind the bench, lit by low morning sun from the right." + (" Pigeons coo and flutter, "
                                                                               "leaves rustle, distant traffic "
                                                                               "hums." if sound else "")))


LTX_WRITER_PROMPT = _shot_writer_prompt(True, 993)
LTX_LOOP_WRITER_PROMPT = _shot_writer_prompt(False, 16289)


TALKING_WRITER_PROMPT = (
    "You write the words for a talking head: one face picture that looks into the camera and says one line, "
    "with its voice, in one short clip. You write the line it says and a short shot note; the app sizes the "
    "clip to the line.\n\n"
    "Reply in plain text, no JSON, no markdown, no commentary, in EXACTLY one of these two shapes.\n\n"
    "To ask:\n"
    "QUESTION: <one short question>\n"
    "OPTIONS: <2 to 4 short choices separated by |>\n\n"
    "To write:\n"
    "LOOK: <a short shot note on the light and framing, at most 8 words>\n"
    "NOTE: <only when you chose something the user did not say, such as the tone: name each choice>\n"
    "LINE: <the exact words the face says, nothing else>\n"
    "LINE comes last. Write nothing after it.\n\n"
    "RULES\n"
    "1. LINE is only the spoken words: no quotation marks around it, no name, no stage directions.\n"
    "2. When the request gives the exact words (in quotes, or \"say ...\"), LINE is those words, unchanged.\n"
    "3. When the request gives a topic or a situation (\"a lighthouse keeper warning sailors about a storm\"), write the "
    "line yourself, in that speaker's own voice and way of talking, spoken straight to the camera: one or "
    "two sentences, 8 to 20 words. An answer to your question is a direction, never the line itself.\n"
    "4. One clip holds at most %d spoken words, about %d seconds.\n"
    "5. LOOK is the light and framing only (\"warm lamp light, close-up\"), never what is said and never "
    "an instruction.\n"
    "6. LINE: NONE makes a quiet listening shot with no speech. Write that only when the user asks for "
    "silence or listening.\n"
    "7. Never write the clip's length: the app sets it from the line, or from a length the user gives.\n\n"
    "WHEN TO ASK\n"
    "Ask ONE question, only when its answer changes the line and neither the request nor an answer says it. "
    "Ask the first of these that applies:\n"
    "a. WHAT: the request gives no topic at all (\"make him talk\"). Offer 3-4 topics. A situation, who "
    "is talking to whom about what, is enough: do not ask, write.\n"
    "b. TONE: the request gives a topic but names no speaker, character or mood at all (\"say something "
    "about rainy days\"). Offer 3-4 deliveries, such as warm, deadpan, excited, stern. A named speaker (a "
    "cowboy, a coach, a grandmother) already sets the voice: do not ask, write.\n"
    "c. TOO LONG: the user's exact words run past %d words, several long sentences. QUESTION: Those words "
    "are longer than one clip holds. Shorten them, or split them into two clips? OPTIONS: Shorten it | "
    "Split it into two clips\n"
    "Once the user has answered, write; choose anything still open yourself and name it in NOTE. For "
    "\"Split it into two clips\", LINE is the first part, and NOTE gives the rest for a second clip. When "
    "you shorten the user's own words, say so in NOTE.\n"
    "The room line in [brackets] is the form as it is now; it is not the request.\n\n"
    "EXAMPLE 1\n"
    "Request: make her talk\n"
    "QUESTION: What should she talk about?\n"
    "OPTIONS: Welcome viewers to the channel | Announce a sale | Tell a short joke\n\n"
    "EXAMPLE 2\n"
    "Request: a tired barista telling the queue the espresso machine is broken\n"
    "LOOK: warm cafe light, close-up\n"
    "LINE: Sorry, folks, the espresso machine just died. Tea, anyone? It's on the house.\n\n"
    "EXAMPLE 3\n"
    "Request: a 6 second clip where he says \"Thanks for watching, see you next week.\"\n"
    "LOOK: soft studio light, head and shoulders\n"
    "NOTE: I chose soft studio light.\n"
    "LINE: Thanks for watching, see you next week.\n"
) % (talking_max_words(), TALKING_MAX_FRAMES // LTX_FPS, talking_max_words())


# The clip fixer (P3c). It sees ONE still from the middle of the clip, never
# the clip: guides/motion/knowledge.md "What the room cannot judge" (camera
# movement between stills is an ABSOLUTE deferral: a softer rule failed on a
# real render).
LTX_REVISER_PROMPT = (
    "You fix a video clip that came out wrong. It was made by a video model from the prompt shown to you, "
    "and the user says what is wrong with it. When a picture is attached, it is ONE still frame from the "
    "middle of the clip, not the clip. Reply in EXACTLY this line format, every key on ONE line. No JSON, "
    "no markdown, no commentary.\n\n"
    "QUESTION: <one question, ONLY when the user has not said what is wrong; otherwise blank>\n"
    "DIAGNOSIS: <one sentence: what went wrong and why, tied to a known cause below>\n"
    "FIX: reroll\n"
    "PROMPT: <the whole revised prompt>\n"
    "NOTE: <one sentence on what you changed, or blank>\n"
    "TWEAK: <at most one setting to change, by its name in the form, as a statement; or blank>\n\n"
    "RULES\n"
    "1. ASK OR FIX. When the user says what is wrong, fix it and ask nothing. When they only say it is off "
    "and name nothing (\"it's not right\", \"I don't like it\"), reply with ONLY the QUESTION line, asking "
    "what looks or sounds wrong, and nothing else: never guess a cause.\n"
    "2. A STILL IS NOT THE CLIP. From the still you may describe only what is in that one frame: who and "
    "what is in it, where, the framing, the light. You can NEVER tell from it how anything moved, how the "
    "camera moved, how fast, the sound, the lip sync or the timing: never say yes or no to any of those. "
    "When the complaint is about one of them, start DIAGNOSIS with \"I can't judge motion or sound from one "
    "still, so going by what you say:\" and fix it from the user's words.\n"
    "3. Never describe what you were not shown.\n"
    "4. KNOWN CAUSES on this model. Name the one that fits:\n"
    "   a. MORE THAN TWO ACTIONS: the model drops the extras. Fix: keep the one or two that matter.\n"
    "   b. A person or animal with nothing to do: it is dropped, merged into someone else, or frozen. Fix: "
    "give each one an action and a place in the frame; take out anyone not wanted.\n"
    "   c. No camera stated, so the model picked one. Fix: state ONE camera move, or a still camera.\n"
    "   d. Feelings named as labels (\"sad\", \"angry\"): nothing visible happens. Fix: posture, gesture, "
    "face.\n"
    "   e. Mixed or unstated light. Fix: one light source.\n"
    "   f. A square or tall starting picture: the picture is widescreen, so motion comes out distorted. Fix: "
    "say in TWEAK to use a widescreen starting picture.\n"
    "   g. Token weights, brackets or tag lists: read as words. Fix: plain sentences.\n"
    "5. FIX is always reroll: a clip is made again from the revised prompt, never edited in place.\n"
    "6. THE REVISED PROMPT keeps everything that came out right and changes only what the complaint needs: "
    "one paragraph, present tense, subject, action, camera, setting, light, and the sound when the prompt "
    "had sound.\n"
    "7. The examples show the format only; their clips are not the one attached.\n\n"
    "EXAMPLE 1\n"
    "The prompt that made it: A chef chops onions, flips a pancake, pours wine, waves at the camera and "
    "laughs in a busy kitchen.\n"
    "The user says: he never flips the pancake or pours the wine\n"
    "[1 picture attached.]\n"
    "QUESTION:\n"
    "DIAGNOSIS: I can't judge motion or sound from one still, so going by what you say: the prompt asks for "
    "five actions and the model keeps only one or two, so the rest were dropped.\n"
    "FIX: reroll\n"
    "PROMPT: A chef in a white jacket flips a pancake high out of a pan and catches it, then grins at the "
    "camera. A still, waist-high shot. A busy steel kitchen, lit by bright overhead light. The pan sizzles, "
    "plates clatter in the background.\n"
    "NOTE: Kept two actions, the flip and the grin, and stated the camera and light.\n"
    "TWEAK:\n\n"
    "EXAMPLE 2\n"
    "The prompt that made it: A red kite dances above a windy beach.\n"
    "The user says: it's not right\n"
    "[1 picture attached.]\n"
    "QUESTION: What looks or sounds wrong: the kite, how it moves, the camera, or the sound?\n"
)


# Appended to the clip fixer's system prompt only when the helper cannot see
# the still (server.py guide_revise()); a seeing helper copies the opener.
LTX_REVISER_BLIND = (
    "NO PICTURE THIS TIME\n"
    "No still from the clip could be shown to you, so never describe it: start DIAGNOSIS with \"I can't "
    "see the clip, so going by what you say:\" and fix it from the user's words.\n"
)


def _describe(models):
    return "LTX-2.5" if models.get("ltx_transformer") else ""


ENGINE = {
    "id": "ltx",
    "cap": "video",
    # Same cap_order as minimax-h3 (the other video pack) -- describe()/
    # cap_order()/missing_words(cap) resolve to whichever pack's id sorts
    # first among video packs, so the number must agree either way rather
    # than depend on which one wins.
    "cap_order": 2,
    "roles": {
        "ltx_transformer": ("unet", {"all": ["ltx", "gguf"]}),
        "ltx_clip": ("clip", {"all": ["ltx"]}),
        "ltx_vae_video": ("vae", {"all": ["ltx"], "none": ["audio"], "prefer": ["video"]}),
        "ltx_vae_audio": ("vae", {"all": ["ltx", "audio"]}),
        # NEW POOL -- "latent_upscaler" is not yet in server.py's POOL_NODES.
        # See the port report: needs [("LatentUpscaleModelLoader", "model_name")].
        "ltx_upscaler": ("latent_upscaler", {"all": ["ltx"], "prefer": ["spatial"]}),
    },
    "primary": {"ltx": "ltx_transformer", "ltx_loop": "ltx_transformer", "talking": "ltx_transformer"},
    "cap_from": ["ltx", "ltx_loop", "talking"],
    "provides": {
        # two_stage defaults True and audio defaults True on both modes, so the
        # baseline ability needs the upscaler and the audio VAE too.
        "ltx": ["ltx_transformer", "ltx_clip", "ltx_vae_video", "ltx_vae_audio", "ltx_upscaler"],
        # LTXVLoopingSampler is video-only (see ltx_loop_graph's docstring) and
        # never runs two_stage, so it needs neither the audio VAE nor the upscaler.
        "ltx_loop": ["ltx_transformer", "ltx_clip", "ltx_vae_video"],
        "talking": ["ltx_transformer", "ltx_clip", "ltx_vae_video", "ltx_vae_audio", "ltx_upscaler"],
    },
    "words": {
        "ltx_transformer": "the LTX video model",
        "ltx_clip": "the LTX text encoder",
        "ltx_vae_video": "the LTX video decoder",
        "ltx_vae_audio": "the LTX audio decoder",
        "ltx_upscaler": "the LTX upscaler",
    },
    "graphs": {
        "ltx": ltx_graph,
        "ltx_loop": ltx_loop_graph,
        "talking": talking_graph,
    },
    "describe": _describe,
    "mode_words": {
        "ltx": "A video from text or a picture, with sound",
        "ltx_loop": "One long continuous take (no sound)",
        "talking": "A face that speaks one line",
    },
    "mode_rooms": {"ltx": "video", "ltx_loop": "video", "talking": "talking"},
    "mode_notes": {
        "ltx": "starts from words or a picture; holds one take to about 41 seconds when windowed",  # source: our internal component notes, engines/ltx.py:878 (preset note)
        "ltx_loop": "the longest length by default, picture only",  # source: engines/ltx.py:884 (preset note), engines/ltx.py:675 (video-only comment)
        "talking": "about a minute once warm on this hardware",  # source: engines/ltx.py:891 (talking preset note)
    },
    # L5: how a prompt must be written for each mode, drawn only from this
    # pack's own field hints -- never a new claim.
    "prompt_guides": {
        "ltx": "16:9 only, keep it to 1-2 actions (vendor prompt contract); "  # source: engines/ltx.py:712-715 (prompt field hint)
               "a square image produces distorted, weird motion.",
        "ltx_loop": "16:9 only, 1-2 actions max (vendor prompt contract); this mode has no sound.",  # source: engines/ltx.py:795-797 (prompt field hint)
        "talking": "The line the face will speak; leave it empty for a quiet listening shot instead of speech.",  # source: engines/ltx.py:835-836 (line field hint)
    },
    # P3c: the Motion guide's writing skills (engines/__init__.py "writers").
    "writers": {
        "ltx": {"label": "Shot writer", "prompt": LTX_WRITER_PROMPT,
                "keys": {"LENGTH": "length", "WIDTH": "width", "HEIGHT": "height", "PROMPT": "prompt"},
                "multiline": "PROMPT", "none_token": "NONE", "check": shot_check},
        "ltx_loop": {"label": "Long take writer", "prompt": LTX_LOOP_WRITER_PROMPT,
                     "keys": {"LENGTH": "length", "WIDTH": "width", "HEIGHT": "height", "PROMPT": "prompt"},
                     "multiline": "PROMPT", "none_token": "NONE", "check": shot_check},
        "talking": {"label": "Line writer", "prompt": TALKING_WRITER_PROMPT,
                    "keys": {"LOOK": "look", "LINE": "line"},
                    "multiline": "LINE", "none_token": "NONE", "derive": talking_derive,
                    "check": talking_check},
    },
    # P3c: "Not right? Tell the guide" on a finished clip (engines/__init__.py
    # "revisers"). A clip is never edited in place, so reroll is the only fix.
    "revisers": {
        mode: {"label": "Clip fixer", "prompt": LTX_REVISER_PROMPT, "blind_note": LTX_REVISER_BLIND,
               "keys": ["QUESTION", "DIAGNOSIS", "FIX", "PROMPT", "NOTE", "TWEAK"],
               "fills": "prompt", "edit_mode": None, "fixes": ["reroll"]}
        for mode in ("ltx", "ltx_loop")
    },
    # R4: field descriptors carry curation (tier/group/order/units/range/
    # ui_range/enabled_when) -- see the pack contract docstring in
    # engines/__init__.py for what each key means. Defaults mirror the frozen
    # builders' own kwarg defaults (R2); the vendor's prompt contract (16:9
    # only, 1-2 actions max) lives in the ltx-video skill and is cited in
    # hints, not invented here.
    "fields": {
        "ltx": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1,
             "hint": "16:9 only. A square image \"produces distorted, weird motion\" "
                     "(vendor prompt contract). Keep it to 1-2 actions."},
            {"id": "start_image", "label": "Starting picture", "type": "image",
             "tier": "primary", "group": "Content", "order": 2,
             "aspect_warning": "Use a picture the shot’s shape: the vendor prompt contract says a square image \"produces distorted, weird motion\".",
             "hint": "Optional. Omit for pure text-to-video."},
            {"id": "end_image", "label": "Ending picture (morph)", "type": "image",
             "tier": "advanced", "group": "Content", "order": 3,
             "enabled_when": {"field": "start_image", "truthy": True},
             "disabled_reason": "Needs a starting picture. A morph needs both endpoints.",
             "aspect_warning": "Use a picture the shot’s shape: the vendor prompt contract says a square image \"produces distorted, weird motion\".",
             "hint": "Turning this on also turns two_stage off automatically is NOT done "
                     "for you. See the sharpen field below."},
            # length % 8 == 1; 97 is the frozen builder's own default (about 4s at 24fps).
            {"id": "length", "label": "Length (frames)", "type": "int", "default": 97,
             "tier": "primary", "group": "Length", "order": 1,
             "units": "frames", "range": [9, 993], "ui_range": [49, 193],
             "hint": "The length snaps to the nearest step the model supports. Capped at "
                     "993 while sound is on (the audio decoder's own ceiling)."},
            {"id": "audio", "label": "Add sound", "type": "checkbox", "default": True,
             "tier": "primary", "group": "Length", "order": 2,
             "hint": "Picture and sound are generated together. Turning this off is the "
                     "only way past the 993-frame length cap."},
            {"id": "width", "label": "Width", "type": "int", "default": 1024,
             "tier": "advanced", "group": "Size", "order": 1,
             "units": "px", "range": [128, 1920], "ui_range": [512, 1536],
             "hint": "Must be a multiple of 32."},
            {"id": "height", "label": "Height", "type": "int", "default": 576,
             "tier": "advanced", "group": "Size", "order": 2,
             "units": "px", "range": [128, 1920], "ui_range": [288, 864],
             "hint": "Must be a multiple of 32."},
            {"id": "two_stage", "label": "Sharpen (two-stage)", "type": "checkbox", "default": True,
             "tier": "advanced", "group": "Quality", "order": 1,
             "enabled_when": {"field": "end_image", "equals": None},
             "disabled_reason": "A morph (ending picture set) must run single-stage. "
                                 "Stage two re-stamps the starting picture and brings back "
                                 "the duplicated-subject defect this mode exists to fix.",
             "hint": "Off runs stage one only, at lower detail and roughly half the VRAM."},
            {"id": "image_strength", "label": "Picture strength", "type": "number", "default": 0.7,
             "tier": "advanced", "group": "Quality", "order": 2,
             "units": "strength", "range": [0.0, 1.0], "ui_range": [0.3, 1.0],
             "enabled_when": {"field": "start_image", "truthy": True},
             "disabled_reason": "Only used when a starting picture is set.",
             "hint": "How closely stage one holds the starting picture."},
            {"id": "negative", "label": "Avoid", "type": "text", "default": LTX25_NEGATIVE,
             "tier": "advanced", "group": "Quality", "order": 3},
            {"id": "img_compression", "label": "Picture compression", "type": "int", "default": 18,
             "tier": "advanced", "group": "Quality", "order": 4,
             "units": "percent", "range": [0, 100], "ui_range": [0, 50],
             "enabled_when": {"field": "start_image", "truthy": True},
             "disabled_reason": "Only used when a starting picture is set.",
             "hint": "How much the starting picture is compressed before conditioning."},
            {"id": "fps", "label": "Frame rate", "type": "int", "default": 24,
             "tier": "advanced", "group": "Length", "order": 4,
             "units": "fps", "range": [1, 60], "ui_range": [12, 30]},
            # Skill-measured: one continuous take holds identity to 41.4s windowed
            # (~37s/s); an un-windowed long clip just grinds (>4900s, never finished
            # at 993 frames). length % 8 == 1 when set.
            {"id": "context_length", "label": "Window size for a long take (frames)",
             "type": "int", "tier": "advanced", "group": "Length", "order": 5,
             "units": "frames", "range": [9, 993], "ui_range": [9, 121],
             "hint": "Leave blank to sample the whole clip in one pass. Set this to sample "
                     "in overlapping windows instead, so a long take does not grind forever. "
                     "Measured up to about 41s this way."},
            {"id": "context_overlap", "label": "Window overlap", "type": "int", "default": 40,
             "tier": "advanced", "group": "Length", "order": 6,
             "units": "frames", "range": [0, 992], "ui_range": [8, 80],
             "enabled_when": {"field": "context_length", "truthy": True},
             "disabled_reason": "Only used when a window size is set.",
             "hint": "How many frames neighbouring windows share. Must be smaller than the window size."},
            {"id": "context_schedule", "label": "Window schedule", "type": "select",
             "default": "standard_uniform",
             "options": ["standard_static", "standard_uniform", "looped_uniform", "batched"],
             "tier": "advanced", "group": "Length", "order": 7,
             "enabled_when": {"field": "context_length", "truthy": True},
             "disabled_reason": "Only used when a window size is set."},
            {"id": "closed_loop", "label": "Loop back to the start", "type": "checkbox",
             "default": False, "tier": "advanced", "group": "Length", "order": 8,
             "enabled_when": {"field": "context_length", "truthy": True},
             "disabled_reason": "Only used when a window size is set (and only on a looped schedule).",
             "hint": "Closes the take so the last frame leads back into the first."},
        ],
        "ltx_loop": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1,
             "hint": "16:9 only, 1-2 actions max (vendor prompt contract). No sound in this mode."},
            {"id": "length", "label": "Length (frames)", "type": "int", "default": 993,
             "tier": "primary", "group": "Length", "order": 1,
             "units": "frames", "range": [9, 16289], "ui_range": [193, 993],
             "hint": "No audio ceiling in this mode, so length can go well past 993 frames."},
            {"id": "width", "label": "Width", "type": "int", "default": 768,
             "tier": "advanced", "group": "Size", "order": 1,
             "units": "px", "range": [128, 1920], "ui_range": [512, 1024]},
            {"id": "height", "label": "Height", "type": "int", "default": 512,
             "tier": "advanced", "group": "Size", "order": 2,
             "units": "px", "range": [128, 1920], "ui_range": [288, 768]},
            {"id": "negative", "label": "Avoid", "type": "text", "default": LTX25_NEGATIVE,
             "tier": "advanced", "group": "Quality", "order": 1},
            {"id": "temporal_tile_size", "label": "Tile size", "type": "int", "default": 80,
             "tier": "advanced", "group": "Continuity", "order": 1,
             "units": "frames", "range": [9, 993], "ui_range": [24, 200],
             "hint": "How many frames are sampled together in one pass."},
            {"id": "temporal_overlap", "label": "Tile overlap", "type": "int", "default": 24,
             "tier": "advanced", "group": "Continuity", "order": 2,
             "units": "frames", "range": [1, 992], "ui_range": [8, 80],
             "hint": "Must be smaller than the tile size."},
            {"id": "temporal_overlap_cond_strength", "label": "Continuity strength",
             "type": "number", "default": 0.5, "tier": "advanced", "group": "Continuity", "order": 3,
             "units": "strength", "range": [0.0, 1.0], "ui_range": [0.0, 1.0],
             "hint": "How strongly one tile carries into the next."},
            {"id": "adain_factor", "label": "Colour drift correction", "type": "number",
             "default": 0.0, "tier": "advanced", "group": "Continuity", "order": 4,
             "units": "factor", "range": [0.0, 1.0], "ui_range": [0.0, 1.0],
             "hint": "Normalises colour/brightness drift across tiles."},
            {"id": "fps", "label": "Frame rate", "type": "int", "default": 24,
             "tier": "advanced", "group": "Length", "order": 2,
             "units": "fps", "range": [1, 60], "ui_range": [12, 30]},
        ],
        "talking": [
            {"id": "face", "label": "Face picture", "type": "image",
             "tier": "primary", "group": "Content", "order": 1,
             "hint": "A portrait. The face is what gets animated."},
            {"id": "line", "label": "Line to speak", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "Leave empty for a quiet listening shot instead of speech."},
            {"id": "look", "label": "Shot note", "type": "text", "default": "",
             "tier": "advanced", "group": "Content", "order": 3,
             "hint": "A short note on lighting or framing, e.g. \"warm lighting\"."},
            {"id": "length", "label": "Length (frames)", "type": "int", "default": 97,
             "tier": "primary", "group": "Length", "order": 1,
             "units": "frames", "range": [9, 993], "ui_range": [49, 361],
             "hint": "About 4 to 15 seconds. Longer than the line takes to say plays out "
                     "as quiet listening afterwards."},
            {"id": "width", "label": "Width", "type": "int", "default": 1024,
             "tier": "advanced", "group": "Size", "order": 1,
             "units": "px", "range": [128, 1920], "ui_range": [512, 1536]},
            {"id": "height", "label": "Height", "type": "int", "default": 576,
             "tier": "advanced", "group": "Size", "order": 2,
             "units": "px", "range": [128, 1920], "ui_range": [288, 864]},
            {"id": "two_stage", "label": "Sharpen (two-stage)", "type": "checkbox", "default": True,
             "tier": "advanced", "group": "Quality", "order": 1,
             "hint": "Off runs stage one only, at lower detail and roughly half the VRAM."},
            {"id": "image_strength", "label": "Picture strength", "type": "number", "default": 0.7,
             "tier": "advanced", "group": "Quality", "order": 2,
             "units": "strength", "range": [0.0, 1.0], "ui_range": [0.3, 1.0]},
            {"id": "img_compression", "label": "Picture compression", "type": "int", "default": 18,
             "tier": "advanced", "group": "Quality", "order": 3,
             "units": "percent", "range": [0, 100], "ui_range": [0, 50],
             "hint": "How much the face picture is compressed before conditioning."},
            {"id": "fps", "label": "Frame rate", "type": "int", "default": 24,
             "tier": "advanced", "group": "Length", "order": 2,
             "units": "fps", "range": [1, 60], "ui_range": [12, 30]},
        ],
    },
    # R4: named parameter sets, same discipline as the other packs -- notes
    # cite real measurements, not a guess.
    "presets": {
        "ltx": [
            {"id": "standard-16x9", "label": "Standard widescreen", "note":
             "1024x576 is the frozen builder's own default, one of the three canonical "
             "multiples-of-32 16:9 sizes the vendor's own templates use.",
             "values": {"width": 1024, "height": 576}},
            # The skill's measured ceiling for a single continuous take before it
            # needs windowing: ~41.4s held identity for all characters; chaining
            # instead lost a character by t~7s. internal LTX research notes.
            {"id": "long-single-take", "label": "Long single take (windowed)", "note":
             "Windowed sampling measured to hold to about 41 seconds of one continuous "
             "take. Chaining separate clips loses a character by about 7 seconds instead.",
             "values": {"length": 993, "context_length": 121}},
        ],
        "ltx_loop": [
            {"id": "default-length", "label": "Full length (default)", "note":
             "993 frames is this mode's own default and the largest length that is "
             "also 8n+1.",
             "values": {"length": 993}},
        ],
        "talking": [
            {"id": "measured-cost", "label": "Measured render time", "note":
             "768x512 at 97 frames is the only setting measured on this hardware: "
             "about a minute once the model is warm, longer on a cold first load.",
             "values": {"width": 768, "height": 512, "length": 97}},
        ],
    },
    # R4: quality tiers, ONLY from evidence -- CAPABILITIES.md's measured
    # LTX-2.5 768x512/97f numbers (112.1s cold, 55-66s warm on the rig,
    # 2026-09-20/21). No steps knob exists here: the distilled checkpoint runs
    # a FIXED 8-point sigma schedule at CFG 1/1 (LTX25_STAGE1_SIGMAS above),
    # so there is nothing to trade against speed the way steps do elsewhere --
    # one tier, not an invented ladder.
    "quality": {
        "ltx": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "the only setting measured on this hardware: about a minute once warm",
             "values": {"width": 768, "height": 512, "length": 97}},
        ],
        # No separate measurement exists for the looping mode specifically --
        # same "no invented tier" discipline as the audio pack's cover mode.
        "ltx_loop": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "no separate speed/quality lever is measured for the long-take mode",
             "values": {}},
        ],
        "talking": [
            {"id": "standard", "label": "Standard", "default": True,
             "why": "the only setting measured on this hardware: about a minute once warm",
             "values": {"width": 768, "height": 512, "length": 97}},
        ],
    },
    # VERIFIED against the HF API license tag (Lightricks/LTX-2.5:
    # license:other, license_name "ltx-2.x-community-license-agreement") and
    # its actual text (github.com/Lightricks/LTX-2/blob/main/LICENSE-2_x,
    # read 2026-09-22): permissive for any use EXCEPT Commercial Entities
    # (>=$10M/yr revenue), who need a separate paid Commercial Use Agreement
    # for anything beyond non-commercial testing/research -- same shape as
    # MiniMax-Music3's revenue-capped licence. shippable is False here for the
    # same reason that one is: this field means "ship with no gate", and the
    # revenue cap is a gate.
    # H2: "Try this" -- ltx's starting picture is optional (pure
    # text-to-video works) and ltx_loop takes no picture at all; talking
    # needs a face picture first.
    "examples": {
        "ltx": [
            {"id": "ltx-try", "label": "A video from words", "recipe": "standard-16x9",
             "quality": "standard",
             "values": {"prompt": "a paper boat drifting down a calm stream, sunlight sparkling on the water"},
             "why": "a short video from words alone, with sound", "needs": None},
        ],
        "ltx_loop": [
            {"id": "ltx-loop-try", "label": "One long continuous take", "recipe": "default-length",
             "quality": "standard",
             "values": {"prompt": "clouds drifting slowly across a bright blue sky"},
             "why": "one long continuous take", "needs": None},
        ],
        "talking": [
            {"id": "talking-try", "label": "A face that speaks", "recipe": "measured-cost",
             "quality": "standard",
             "values": {"line": "Welcome to Black Wire Forge."},
             "why": "a face that speaks one line", "needs": "picture"},
        ],
    },
    "licence": {
        "name": "LTX-2.x Community License Agreement",
        "shippable": False,
        "attribution": "LTX-2.5 by Lightricks",
        "url": "https://github.com/Lightricks/LTX-2/blob/main/LICENSE-2_x",
    },
}
