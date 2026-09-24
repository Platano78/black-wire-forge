"""Engine pack: MiniMax-H3 (first/last-to-video, reference-to-video, turbo).

Copied from server.py's pre-extraction ROLE_RULES/ROLE_POOL entries,
abilities()/missing_for() labels, describe_video(), and the h3_*_graph
builders. Logic is verbatim; only the lane->models indirection is replaced
by the contract's direct ``models`` dict.

Note on the text encoder: server.py requires h3_clip_nvfp4 OR h3_clip_int8,
but the contract's ``provides`` can only list roles that must ALL resolve,
so this pack lists h3_clip_nvfp4 (the role server.py's own missing_for
treats as "the H3 text encoder" in the golden rig). A rig with only the int8
clip would report fl2va/ref2v missing here, whereas server.py would not.
"""
from . import unet_loader, quant_words


def _describe(models):
    q = quant_words(models.get("h3_unet_fl2va") or models.get("h3_unet_ref2va"))
    return "MiniMax-H3" + (" (%s)" % q if q else "")


def h3_fl2va_graph(p, m):
    """The 'cheers' recipe: fl2va unet + NVFP4 AWQ encoder + MiniMaxH3ImageToVideo,
    res_multistep/simple, 20 steps, no LoRA. first_frame/last_frame optional, and
    with neither wired it is pure text-to-video (which is what cheers was)."""
    clip = p.get("encoder") or m.get("h3_clip_nvfp4") or m["h3_clip_int8"]
    g = {
        "6": unet_loader(m["h3_unet_fl2va"]),
        "13": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "minimax", "device": "default"}},
        "11": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_video"]}},
        "24": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_audio"]}},
        "104": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["13", 0], "vae": ["11", 0], "prompt": p["prompt"],
            "width": p["width"], "height": p["height"], "length": p["length"]}},
        "16": {"class_type": "BasicGuider", "inputs": {"model": ["6", 0], "conditioning": ["104", 0]}},
        "17": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "BasicScheduler", "inputs": {
            "model": ["6", 0], "scheduler": "simple", "steps": p["steps"], "denoise": 1.0}},
        "15": {"class_type": "RandomNoise", "inputs": {"noise_seed": p["seed"]}},
        "14": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["15", 0], "guider": ["16", 0], "sampler": ["17", 0],
            "sigmas": ["9", 0], "latent_image": ["104", 1]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["11", 0]}},
        "23": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["14", 0], "vae": ["24", 0]}},
        "91": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "audio": ["23", 0], "fps": 24.0}},
        "92": {"class_type": "SaveVideo", "inputs": {
            "video": ["91", 0], "filename_prefix": "blackwire/VID", "format": "auto", "codec": "auto"}},
    }
    if p.get("turbo_lora"):
        # Model-only LoRA: the H3 CLIP is separate, so only the UNET gets patched.
        g["7"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["6", 0], "lora_name": m["h3_turbo_lora"], "strength_model": 1.0}}
        g["16"]["inputs"]["model"] = ["7", 0]
        g["9"]["inputs"]["model"] = ["7", 0]
    if p.get("first_frame"):
        g["200"] = {"class_type": "LoadImage", "inputs": {"image": p["first_frame"]}}
        g["104"]["inputs"]["first_frame"] = ["200", 0]
    if p.get("last_frame"):
        g["201"] = {"class_type": "LoadImage", "inputs": {"image": p["last_frame"]}}
        g["104"]["inputs"]["last_frame"] = ["201", 0]
    return g


def h3_continue_graph(p, m):
    """The 'continue' recipe: same fl2va unet + NVFP4 AWQ encoder +
    MiniMaxH3ImageToVideo as h3_fl2va_graph, but with NO starting picture --
    instead the previous shot's harvested clip (frames + audio) is fed
    through MiniMaxH3MotionContext (route C, L2's frames+audio arm --
    our internal l2-continuity-2026-09-23 measurement notes, shot2_C) so
    the new shot's motion and
    sound carry on from where the previous one left off, instead of
    freezing/restarting at the cut. MotionContextTrim then drops the pinned
    context frames both the base recipe and the base MiniMaxH3ImageToVideo
    latent are still built from (the trim, not this graph, is what delivers
    a shot 22 frames shorter than requested -- L2's own finding). Like
    fl2va's own optional first_frame/last_frame, `prev_video` is optional at
    THIS level -- server.py's cable resolver (resolve_slot_cables/K3) is
    what actually refuses a shot whose jack is plugged but unresolvable; a
    shot with no cable at all (e.g. the first of a chain) just renders
    without the Motion-Context wiring, plain text-to-video."""
    clip = p.get("encoder") or m.get("h3_clip_nvfp4") or m["h3_clip_int8"]
    g = {
        "6": unet_loader(m["h3_unet_fl2va"]),
        "13": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "minimax", "device": "default"}},
        "11": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_video"]}},
        "24": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_audio"]}},
        "104": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["13", 0], "vae": ["11", 0], "prompt": p["prompt"],
            "width": p["width"], "height": p["height"], "length": p["length"]}},
        "16": {"class_type": "BasicGuider", "inputs": {"model": ["6", 0], "conditioning": ["104", 0]}},
        "17": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "BasicScheduler", "inputs": {
            "model": ["6", 0], "scheduler": "simple", "steps": p["steps"], "denoise": 1.0}},
        "15": {"class_type": "RandomNoise", "inputs": {"noise_seed": p["seed"]}},
        "14": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["15", 0], "guider": ["16", 0], "sampler": ["17", 0],
            "sigmas": ["9", 0], "latent_image": ["104", 1]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["11", 0]}},
        "23": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["14", 0], "vae": ["24", 0]}},
        "91": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "audio": ["23", 0], "fps": 24.0}},
        "92": {"class_type": "SaveVideo", "inputs": {
            "video": ["91", 0], "filename_prefix": "blackwire/CONT", "format": "auto", "codec": "auto"}},
    }
    if p.get("turbo_lora"):
        g["7"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["6", 0], "lora_name": m["h3_turbo_lora"], "strength_model": 1.0}}
        g["16"]["inputs"]["model"] = ["7", 0]
        g["9"]["inputs"]["model"] = ["7", 0]
    if p.get("prev_video"):
        g["220"] = {"class_type": "LoadVideo", "inputs": {"file": p["prev_video"]}}
        g["221"] = {"class_type": "GetVideoComponents", "inputs": {"video": ["220", 0]}}
        g["222"] = {"class_type": "MiniMaxH3MotionContext", "inputs": {
            "conditioning": ["104", 0], "vae": ["11", 0], "latent": ["104", 1],
            "context_length": "22", "audio_context_length": 24,
            "context_frames": ["221", 0], "context_audio": ["221", 1], "audio_vae": ["24", 0]}}
        g["16"]["inputs"]["conditioning"] = ["222", 0]
        g["223"] = {"class_type": "MiniMaxH3MotionContextTrim", "inputs": {
            "images": ["10", 0], "trim_frames": ["222", 1], "audio": ["23", 0],
            "fps": 24.0, "match_tail": True}}
        g["91"]["inputs"]["images"] = ["223", 0]
        g["91"]["inputs"]["audio"] = ["223", 1]
    return g


def h3_ref2va_graph(p, m):
    """ref2va: up to 9 reference images + up to 3 reference videos (frames at 24fps,
    2-15s each) with the first video's audio carried through. INT8 encoder by default.
    Audio is NEVER fed in as TTS -- H3 speaks the dialogue in the prompt itself."""
    clip = p.get("encoder") or m.get("h3_clip_int8") or m["h3_clip_nvfp4"]
    g = {
        "159": unet_loader(m["h3_unet_ref2va"]),
        "160": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "minimax", "device": "default"}},
        "162": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_video"]}},
        "163": {"class_type": "VAELoader", "inputs": {"vae_name": m["h3_vae_audio"]}},
        "164": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "clip": ["160", 0], "vae": ["162", 0], "audio_vae": ["163", 0], "prompt": p["prompt"],
            "width": p["width"], "height": p["height"], "length": p["length"],
            "ref_image_size": p.get("ref_image_size", "match")}},
        "166": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "167": {"class_type": "BasicScheduler", "inputs": {
            "model": ["159", 0], "scheduler": "simple", "steps": p["steps"], "denoise": 1.0}},
        "168": {"class_type": "BasicGuider", "inputs": {"model": ["159", 0], "conditioning": ["164", 0]}},
        "169": {"class_type": "RandomNoise", "inputs": {"noise_seed": p["seed"]}},
        "177": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["169", 0], "guider": ["168", 0], "sampler": ["166", 0],
            "sigmas": ["167", 0], "latent_image": ["164", 1]}},
        "180": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["177", 0], "vae": ["163", 0]}},
        "181": {"class_type": "VAEDecode", "inputs": {"samples": ["177", 0], "vae": ["162", 0]}},
        "182": {"class_type": "CreateVideo", "inputs": {"fps": 24, "bit_depth": 8,
                                                        "images": ["181", 0], "audio": ["180", 0]}},
        "184": {"class_type": "SaveVideo", "inputs": {
            "filename_prefix": "blackwire/REF", "format": "auto", "codec": "auto", "video": ["182", 0]}},
    }
    for i, name in enumerate((p.get("ref_images") or [])[:9]):
        nid = str(400 + i)
        g[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g["164"]["inputs"]["ref_images.ref_image_%d" % i] = [nid, 0]
    for i, name in enumerate((p.get("ref_videos") or [])[:3]):
        vid, cid = str(300 + i * 2), str(301 + i * 2)
        g[vid] = {"class_type": "LoadVideo", "inputs": {"file": name}}
        g[cid] = {"class_type": "GetVideoComponents", "inputs": {"video": [vid, 0]}}
        g["164"]["inputs"]["ref_videos.ref_video_%d" % i] = [cid, 0]
        if i == 0 and p.get("keep_audio", True):
            g["164"]["inputs"]["ref_video_audios.ref_video_audio_0"] = [cid, 1]
    return g


ENGINE = {
    "id": "minimax-h3",
    "cap": "video",
    "cap_order": 2,
    # server.py's api_generate has hand-tuned logic for fl2va/ref2v (frame-
    # grid snapping, turbo-LoRA derivation from step count, ref image/video
    # wiring) that this pack's generic field declarations do not fully
    # capture -- keep it on the legacy path rather than the generic one
    # every other pack now uses.
    "legacy_dispatch": True,
    "roles": {
        "h3_unet_fl2va": ("unet", {"all": ["minimax_h3"], "none": ["ref2v"], "prefer": ["fl2v", "t2v"]}),
        "h3_unet_ref2va": ("unet", {"all": ["minimax_h3", "ref2v"]}),
        "h3_clip_nvfp4": ("clip", {"all": ["qwen3vl", "minimax_h3"], "prefer": ["nvfp4", "awq"]}),
        "h3_clip_int8": ("clip", {"all": ["qwen3vl", "minimax_h3"], "prefer": ["int8"]}),
        "h3_vae_video": ("vae", {"all": ["minimax_h3"], "none": ["audio"], "prefer": ["video"]}),
        "h3_vae_audio": ("vae", {"all": ["minimax_h3", "audio"]}),
        "h3_turbo_lora": ("lora", {"all": ["minimax_h3"], "any": ["turbo", "4step", "lightx2v"],
                                   "prefer": ["comfy", "fl2v"]}),
    },
    "primary": {"fl2va": "h3_unet_fl2va", "ref2v": "h3_unet_ref2va"},
    "cap_from": ["fl2va", "ref2v"],
    "provides": {
        "fl2va": ["h3_unet_fl2va", "h3_vae_video", "h3_vae_audio", ["h3_clip_nvfp4", "h3_clip_int8"]],
        "ref2v": ["h3_unet_ref2va", "h3_vae_video", "h3_vae_audio", ["h3_clip_nvfp4", "h3_clip_int8"]],
        "turbo": ["h3_turbo_lora"],
        # C3.4b: the continue graph is the fl2va unet + MotionContext nodes
        # from the installed NikoDemon80/ComfyUI-H3-Motion-Context pack (no
        # extra model file of its own) -- same models fl2va needs, no more.
        "continue": ["h3_unet_fl2va", "h3_vae_video", "h3_vae_audio", ["h3_clip_nvfp4", "h3_clip_int8"]],
    },
    "words": {
        "h3_unet_fl2va": "the H3 video model",
        "h3_unet_ref2va": "the H3 reference model",
        "h3_clip_nvfp4": "the H3 text encoder",
        "h3_clip_int8": "the H3 text encoder",
        "h3_vae_video": "the H3 video decoder",
        "h3_vae_audio": "the H3 audio decoder",
        "h3_turbo_lora": "the speed pack",
    },
    "graphs": {
        "fl2va": h3_fl2va_graph,
        "ref2v": h3_ref2va_graph,
        "continue": h3_continue_graph,
    },
    "describe": _describe,
    "mode_words": {
        "fl2va": "A video from a starting picture (and text)",
        "ref2v": "A video that copies people or clips you upload",
        "continue": "A video that continues the shot before it",
    },
    "mode_rooms": {"fl2va": "video", "ref2v": "video", "continue": "video"},
    "mode_notes": {
        "fl2va": "animates your picture, with sound, up to about 15 seconds",  # source: our internal component notes, engines/minimax_h3.py:244 (preset note)
        "ref2v": "puts the people or clips you upload into a new scene, with sound",  # source: our internal component notes
        "continue": "continues from an earlier shot, carrying its motion and sound across the cut",  # source: the internal continue-feature commission doc K6 (the ruling names this exact substring)
    },
    # L5: how a prompt must be written for each mode, drawn only from this
    # pack's own docstrings/field hints -- never a new claim.
    "prompt_guides": {
        "fl2va": "Describe the scene and action (positive prompt only); a starting "  # source: engines/minimax_h3.py:23-26 (h3_fl2va_graph docstring), :171 (first_frame hint)
                 "and/or ending picture is optional -- omit both for pure text-to-video.",
        "ref2v": "Describe the scene; H3 speaks any dialogue directly in this text "  # source: engines/minimax_h3.py:67-68 (h3_ref2va_graph docstring)
                 "-- audio is never fed in as TTS.",
        "continue": "Describe the SAME shot carrying on -- the same subject, the same "  # source: our internal l2-continuity-2026-09-23 measurement notes, section "Live-app finding"
                    "camera move and the same framing as the shot before it, never a "
                    "new angle or composition: a new composition overrides the ~0.9 s "
                    "of carried motion. Example (H3's structured format): "
                    "integrated_multimodal_description: \"[Shot 1] Live-action, cinematic, "
                    "a red vintage sports car keeps driving from left to right along a "
                    "coastal road at the same steady speed in bright afternoon sun. The "
                    "camera tracks alongside the car at the same speed, keeping it "
                    "centred in frame.\" overall_soundscape: \"The car's engine hums at a "
                    "steady pitch, tyres roll on warm asphalt, wind rushes past.\" "
                    "non_diegetic_music: \"None.\" No starting picture is needed -- the "
                    "shot before it carries the visual and audio continuation. (See "
                    "our internal l2-continuity-2026-09-23 measurement notes, \"Live-app "
                    "finding\".)",
    },
    # R4: describes the surface server.py's kind=="video" branch already
    # sends (lines ~1488-1552) -- defaults/ranges taken from there, not
    # invented. `length` is FRAMES (H3's 17n+5 grid via snap_frames), not
    # seconds -- the seconds shown to the user is length/24.
    "fields": {
        "fl2va": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "first_frame", "label": "Starting picture", "type": "image",
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "Optional. Omit both frames for pure text-to-video."},
            {"id": "last_frame", "label": "Ending picture", "type": "image",
             "tier": "advanced", "group": "Content", "order": 3},
            # Frame grid is 17n+5; 362 = 15.1s trained max, 124 = ~5s min.
            {"id": "length", "label": "Length (frames)", "type": "int", "default": 362,
             "tier": "primary", "group": "Length", "order": 1,
             "units": "frames", "range": [124, 362], "ui_range": [124, 362],
             "hint": "About 5 to 15 seconds. The length snaps to the nearest step the model supports."},
            {"id": "width", "label": "Width", "type": "int", "default": 960,
             "tier": "advanced", "group": "Size", "order": 1,
             "units": "px", "range": [128, 1920], "ui_range": [512, 1280],
             "hint": "Snapped to a multiple of 32."},
            {"id": "height", "label": "Height", "type": "int", "default": 544,
             "tier": "advanced", "group": "Size", "order": 2,
             "units": "px", "range": [128, 1920], "ui_range": [288, 720],
             "hint": "Snapped to a multiple of 32."},
            {"id": "steps", "label": "Steps", "type": "int", "default": 20,
             "tier": "advanced", "group": "Quality", "order": 1,
             "units": "steps", "range": [1, 80], "ui_range": [8, 30]},
            # A 4-step distillation LoRA -- fights the sampling schedule and smooths
            # detail away above 8 steps, which is why it force-disables itself there.
            {"id": "turbo_lora", "label": "Fast mode (turbo)", "type": "checkbox",
             "tier": "advanced", "group": "Quality", "order": 2,
             "hint": "A faster mode that only works at low detail settings. "
                     "Turns itself off above 8 steps."},
            # Defaults to the NVFP4 AWQ encoder here; ref2v below defaults to INT8.
            {"id": "encoder", "label": "Text encoder override", "type": "text",
             "tier": "advanced", "group": "Quality", "order": 3,
             "hint": "Leave blank to use the default."},
        ],
        # C3.4b K5: same base recipe as fl2va, but the "video" field is a
        # jack (server.py's slot_jacks/_op_patch), not an upload -- its
        # value only ever arrives already patched in from the shot before
        # it on the timeline, never typed or chosen here.
        "continue": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "prev_video", "label": "Previous shot", "type": "video",
             "tier": "primary", "group": "Content", "order": 2,
             "hint": "Plug in the shot before this one on the timeline."},
            # Frame grid is 17n+5; 362 = 15.1s trained max, 124 = ~5s min.
            # The delivered clip is 22 frames shorter than this: the join
            # keeps the previous shot's last frames, then trims them back
            # off the end once the new ones are made (L2's own finding).
            {"id": "length", "label": "Length (frames)", "type": "int", "default": 362,
             "tier": "primary", "group": "Length", "order": 1,
             "units": "frames", "range": [124, 362], "ui_range": [124, 362],
             "hint": "About 5 to 15 seconds. The delivered shot comes out 22 "
                     "frames (about 0.9s) shorter than this."},
            {"id": "width", "label": "Width", "type": "int", "default": 960,
             "tier": "advanced", "group": "Size", "order": 1,
             "units": "px", "range": [128, 1920], "ui_range": [512, 1280],
             "hint": "Snapped to a multiple of 32."},
            {"id": "height", "label": "Height", "type": "int", "default": 544,
             "tier": "advanced", "group": "Size", "order": 2,
             "units": "px", "range": [128, 1920], "ui_range": [288, 720],
             "hint": "Snapped to a multiple of 32."},
            {"id": "steps", "label": "Steps", "type": "int", "default": 20,
             "tier": "advanced", "group": "Quality", "order": 1,
             "units": "steps", "range": [1, 80], "ui_range": [8, 30]},
            {"id": "turbo_lora", "label": "Fast mode (turbo)", "type": "checkbox",
             "tier": "advanced", "group": "Quality", "order": 2,
             "hint": "A faster mode that only works at low detail settings. "
                     "Turns itself off above 8 steps."},
            {"id": "encoder", "label": "Text encoder override", "type": "text",
             "tier": "advanced", "group": "Quality", "order": 3,
             "hint": "Leave blank to use the default."},
        ],
        "ref2v": [
            {"id": "prompt", "label": "Prompt", "type": "textarea",
             "tier": "primary", "group": "Content", "order": 1},
            {"id": "ref_images", "label": "Reference pictures", "type": "image_list",
             "tier": "primary", "group": "Content", "order": 2, "max": 9,
             "hint": "Up to 9. At least one picture or clip is required."},
            {"id": "ref_videos", "label": "Reference clips", "type": "video_list",
             "tier": "primary", "group": "Content", "order": 3,
             "hint": "Up to 3, 2-15s each at 24fps. The first clip's audio can be kept."},
            # Frame grid is 17n+5; 362 = 15.1s trained max, 124 = ~5s min.
            {"id": "length", "label": "Length (frames)", "type": "int", "default": 362,
             "tier": "primary", "group": "Length", "order": 1,
             "units": "frames", "range": [124, 362], "ui_range": [124, 362],
             "hint": "About 5 to 15 seconds. The length snaps to the nearest step the model supports."},
            {"id": "width", "label": "Width", "type": "int", "default": 960,
             "tier": "advanced", "group": "Size", "order": 1,
             "units": "px", "range": [128, 1920], "ui_range": [512, 1280]},
            {"id": "height", "label": "Height", "type": "int", "default": 544,
             "tier": "advanced", "group": "Size", "order": 2,
             "units": "px", "range": [128, 1920], "ui_range": [288, 720]},
            {"id": "steps", "label": "Steps", "type": "int", "default": 20,
             "tier": "advanced", "group": "Quality", "order": 1,
             "units": "steps", "range": [1, 80], "ui_range": [8, 30]},
            {"id": "keep_audio", "label": "Keep audio from first reference clip",
             "type": "checkbox", "default": True,
             "tier": "advanced", "group": "Quality", "order": 2},
            # Live node schema (GET /object_info/MiniMaxH3ReferenceToVideo, queried
            # 2026-09-22): options are exactly ["match", "max"], default "match".
            # "match" scales each reference down-only to the generation's pixel
            # area; "max" uses the reference pipeline's 2048px short edge for best
            # identity fidelity. Matches the "max-reference" preset's measured
            # 161.7s vs 90.2s (1.8x), not benchmarked further than that.
            {"id": "ref_image_size", "label": "Reference image sizing", "type": "select",
             "default": "match", "options": ["match", "max"],
             "tier": "advanced", "group": "Quality", "order": 3,
             "hint": "Max reads faces much better and is slower (measured 1.8x)."},
            # Defaults to the INT8 encoder here (fl2va above defaults to NVFP4).
            {"id": "encoder", "label": "Text encoder override", "type": "text",
             "tier": "advanced", "group": "Quality", "order": 4,
             "hint": "Leave blank to use the default."},
        ],
    },
    # R3: named parameter sets, same discipline as engines/audio.py's presets
    # -- notes cite real measurements, not a guess.
    "presets": {
        "fl2va": [
            {"id": "default-length", "label": "Standard length (default)", "note":
             "362 frames (about 15.1s) is the trained max and this field's own default.",
             "values": {"length": 362}},
            # Full citation: 8 steps with the speed pack measured +2.30 dB over the
            # 4-step version (5/5 seeds, paired t=12.08) and removed a 1.04-1.10x
            # push-in drift entirely (1.000x at 8 steps). Deployed end-to-end:
            # +5.0 dB and no drift vs the prior broken state. Measured 2026-09-21,
            # internal measurement notes.
            {"id": "turbo-8step", "label": "Fast (8-step, measured)", "note":
             "8 steps with the speed pack measured a clear quality gain over the "
             "4-step version and removed a small drift entirely. Measured 2026-09-21.",
             "values": {"steps": 8, "turbo_lora": True}},
        ],
        "ref2v": [
            {"id": "default-length", "label": "Standard length (node default)", "note":
             "124 frames is this mode's own default and the floor of its trained "
             "range (about 124-362).",
             "values": {"length": 124}},
            # Full citation: ref_image_size="max" measured 161.7s vs 90.2s render
            # (1.8x, not the tooltip's "several times") but reads referenced faces
            # far better -- "match" rendered a character near-profile and generic.
            # No 8-step speed pack exists for this path (only a 4-step one), so
            # that win does not transfer here. Measured,
            # internal measurement notes.
            {"id": "max-reference", "label": "Best face fidelity (measured)", "note":
             "Reads referenced faces far better than the default, and is slower "
             "(measured 1.8x). The default rendered a character near-profile and generic.",
             "values": {"ref_image_size": "max"}},
        ],
        # C3.4b: not a dedicated A/B for continue specifically -- every arm
        # of the L2 spike (including this one) rendered at 8 steps with the
        # speed pack; no separate measurement exists yet for this mode
        # alone. Our internal l2-continuity-2026-09-23 measurement notes.
        "continue": [
            {"id": "default-length", "label": "Standard length (default)", "note":
             "362 frames (about 15.1s) is the trained max and this field's own default.",
             "values": {"length": 362}},
            {"id": "turbo-8step", "label": "Fast (8-step, spike setting)", "note":
             "8 steps with the speed pack is what every test render of this mode used.",
             "values": {"steps": 8, "turbo_lora": True}},
        ],
    },
    # R2: ported verbatim from the old page's VID_Q / VID_Q_WHY constants
    # (index.html, since replaced) -- same six step counts, same "why" text,
    # Best still the default. The 4/6/8-step rows carry "requires": "turbo"
    # and set turbo_lora explicitly (not just steps<=8) so their `values`
    # produce exactly what api_generate's video branch already derives --
    # see its comment at the `turbo = ...` line. unavailable_reason is the
    # old page's own applySpeedPack() sentence.
    #
    # RULING (owner, after C2a review): ref2v does NOT get the three fast
    # rows. h3_ref2va_graph (above) never reads p["turbo_lora"] -- there is
    # no speed pack wired for this path -- so a "Fast" tier here would run
    # 4-8 steps with no distillation LoRA, exactly the mush the old page's
    # applySpeedPack() switched those rows off to prevent. A 4-step
    # `minimax_h3_ref2v_turbo_4step` exists upstream; wiring it in is a
    # separate change, not this one. ref2v keeps Middle/Good/Best only.
    "quality": {
        "fl2va": [
            {"id": "fast", "label": "Fast", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, quickest, good for trying an idea",
             "values": {"steps": 4, "turbo_lora": True}},
            {"id": "fast-plus", "label": "Fast+", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, a little more detail",
             "values": {"steps": 6, "turbo_lora": True}},
            {"id": "fast-best", "label": "Fast best", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, best of the quick ones",
             "values": {"steps": 8, "turbo_lora": True}},
            {"id": "middle", "label": "Middle",
             "why": "runs clean, in between, can look soft",
             "values": {"steps": 12, "turbo_lora": False}},
            {"id": "good", "label": "Good",
             "why": "runs clean, close to best, a bit quicker",
             "values": {"steps": 16, "turbo_lora": False}},
            {"id": "best", "label": "Best", "default": True,
             "why": "runs clean, what every good clip we made used",
             "values": {"steps": 20, "turbo_lora": False}},
        ],
        # No fast tiers, no "turbo_lora" key: see the ruling above ref2v does
        # not have a speed pack wired in, so it starts at Middle.
        "ref2v": [
            {"id": "middle", "label": "Middle",
             "why": "runs clean, in between, can look soft",
             "values": {"steps": 12}},
            {"id": "good", "label": "Good",
             "why": "runs clean, close to best, a bit quicker",
             "values": {"steps": 16}},
            {"id": "best", "label": "Best", "default": True,
             "why": "runs clean, what every good clip we made used",
             "values": {"steps": 20}},
        ],
        # Same tiers as fl2va, same base recipe -- continue's own graph adds
        # only the previous-shot jack, no separate quality axis.
        "continue": [
            {"id": "fast", "label": "Fast", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, quickest, good for trying an idea",
             "values": {"steps": 4, "turbo_lora": True}},
            {"id": "fast-plus", "label": "Fast+", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, a little more detail",
             "values": {"steps": 6, "turbo_lora": True}},
            {"id": "fast-best", "label": "Fast best", "requires": "turbo",
             "unavailable_reason": "the quick ones need a speed pack, which this machine does not have",
             "why": "speed pack on, best of the quick ones",
             "values": {"steps": 8, "turbo_lora": True}},
            {"id": "middle", "label": "Middle",
             "why": "runs clean, in between, can look soft",
             "values": {"steps": 12, "turbo_lora": False}},
            {"id": "good", "label": "Good",
             "why": "runs clean, close to best, a bit quicker",
             "values": {"steps": 16, "turbo_lora": False}},
            {"id": "best", "label": "Best", "default": True,
             "why": "runs clean, what every good clip we made used",
             "values": {"steps": 20, "turbo_lora": False}},
        ],
    },
    # H2: "Try this" -- fl2va's starting picture is optional (pure
    # text-to-video works); ref2v needs at least one reference picture first;
    # continue needs an earlier shot already made and picked, not a file
    # this example can preset (the jack is filled by a cable, never typed).
    "examples": {
        "fl2va": [
            {"id": "fl2va-try", "label": "A video from an idea", "recipe": "default-length",
             "quality": "best",
             "values": {"prompt": "a paper lantern floating gently down a river at dusk"},
             "why": "a video animated from a starting idea, with sound", "needs": None},
        ],
        "ref2v": [
            {"id": "ref2v-try", "label": "A video with your reference", "recipe": "default-length",
             "quality": "best",
             "values": {"prompt": "the person steps into a sunlit garden"},
             "why": "a video that places your reference into a new scene",
             "needs": "picture"},
        ],
        "continue": [
            {"id": "continue-try", "label": "Continue the shot before", "recipe": "default-length",
             "quality": "best",
             "values": {"prompt": "the car keeps driving along the coast road"},
             "why": "carries the motion and sound of the shot before it into this one",
             "needs": "previous shot"},
        ],
    },
    "licence": {
        "name": "MiniMax-H3 Model License",
        "shippable": False,
        "attribution": "MiniMax-H3 by MiniMax",
        "url": "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE",
        "note": "Open weights are licensed outside the EU, UK, South Korea and the USA; "
                "elsewhere MiniMax asks you to apply (platform.minimax.io/h3-license).",
    },
}
