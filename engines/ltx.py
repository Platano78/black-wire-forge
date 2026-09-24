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
import random

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
             "hint": "Optional. Omit for pure text-to-video."},
            {"id": "end_image", "label": "Ending picture (morph)", "type": "image",
             "tier": "advanced", "group": "Content", "order": 3,
             "enabled_when": {"field": "start_image", "truthy": True},
             "disabled_reason": "Needs a starting picture. A morph needs both endpoints.",
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
