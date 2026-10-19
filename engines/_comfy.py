"""What both the app and the pack builder need to know about a ComfyUI lane, with no
dependency on server.py: which dropdown each model pool is read from, how a pack's rule is
matched and ranked against a lane's files, and how a finished job's outputs are read.

Moved out of server.py unchanged (POOL_NODES, SMALL_QUANTS / BIG_QUANTS, rank, rule_matches,
small_card_for, collect_outputs); server.py re-imports each under its old name, so
`srv._rule_matches`, `srv.POOL_NODES` and `srv.collect_outputs` are these same objects.
New here: normalize_object_info and pool_names_from_object_info, which read ONE whole
/object_info the way server.py's pool_names reads per-node answers.

The leading underscore keeps engines._discover() from taking this module for a pack.
Nothing here names a model or a vendor.
"""

import copy
import os

# role -> which loader's dropdown to search
POOL_NODES = {
    "unet": [("UNETLoader", "unet_name"), ("UnetLoaderGGUF", "unet_name")],
    "clip": [("CLIPLoader", "clip_name"), ("DualCLIPLoader", "clip_name1")],
    "vae": [("VAELoader", "vae_name")],
    "lora": [("LoraLoaderModelOnly", "lora_name")],
    "checkpoint": [("CheckpointLoaderSimple", "ckpt_name")],
    "audio_encoder": [("AudioEncoderLoader", "audio_encoder_name")],
    "bg_removal": [("LoadBackgroundRemovalModel", "bg_removal_name")],
    "upscale_model": [("UpscaleModelLoader", "model_name")],
    "clip_vision": [("CLIPVisionLoader", "clip_name")],
    "latent_upscaler": [("LatentUpscaleModelLoader", "model_name")],
}

# Quantisation markers, for choosing a build the card can actually hold.
SMALL_QUANTS = ("nvfp4", "int8", "fp8", "w4a8", "int4", "gguf", "q8", "q6", "q5", "q4", "awq")
BIG_QUANTS = ("bf16", "fp16", "fp32", "float16")


def rank(name, rule, small_card):
    """Higher is better. Ties break towards the shorter (plainer) filename."""
    n = name.lower()
    score = 0.0
    prefer = rule.get("prefer") or []
    for i, term in enumerate(prefer):
        if term in n:
            score += (len(prefer) - i) * 10.0
    if small_card:
        if any(q in n for q in SMALL_QUANTS):
            score += 5.0
        elif any(q in n for q in BIG_QUANTS):
            score -= 5.0
    return score - len(name) * 0.01


def rule_matches(pool, rule):
    """Every name in `pool` that satisfies `rule` (all/none/any substrings,
    case-insensitive). Shared by pick_model (picks the single best match)
    and a "pool_select" field's options (LORA-1: every match is offered,
    not just the best)."""
    must_all = rule.get("all") or []
    must_none = rule.get("none") or []
    must_any = rule.get("any") or []
    hits = []
    for name in pool:
        n = name.lower()
        if not all(t in n for t in must_all):
            continue
        if any(t in n for t in must_none):
            continue
        if must_any and not any(t in n for t in must_any):
            continue
        hits.append(name)
    return hits


def small_card_for(vram):
    """Discovery's card test, shared with Setup: under ~25GB, prefer a quantised
    build over bf16/fp16. Unknown counts as small: erring towards the lighter
    file is the safer mistake."""
    return (vram or 0) < 25e9


def collect_outputs(hist_entry):
    """ComfyUI puts SaveImage under 'images' and SaveVideo ALSO under 'images'
    (with animated: true). Scan every list of file dicts.

    A loader node (LoadVideo) echoes the file it just loaded back into this
    same history dict as a UI preview, tagged `"type": "input"` -- and a
    PreviewImage-style node tags its own preview `"type": "temp"`. Both are
    shaped identically to a real SaveX result, so skip anything not tagged
    "output" (a missing `type` still counts as "output", same as before --
    most real Save nodes never set it explicitly). Found 2026-09-23: an H3
    "continue" job's LoadVideo (prev_video) ran before its SaveVideo, so
    outputs[0] silently became the echoed prev_video instead of the render."""
    outs = []
    for node_id, node_out in (hist_entry.get("outputs") or {}).items():
        for key, val in node_out.items():
            if not isinstance(val, list):
                continue
            for item in val:
                if isinstance(item, dict) and item.get("filename"):
                    if item.get("type", "output") != "output":
                        continue
                    fn = item["filename"]
                    ext = os.path.splitext(fn)[1].lower()
                    if ext in (".mp4", ".webm", ".mov", ".mkv"):
                        media = "video"
                    elif ext in (".flac", ".mp3", ".opus", ".wav", ".m4a", ".ogg"):
                        media = "audio"
                    elif ext in (".glb", ".gltf", ".obj", ".ply"):
                        media = "3d"
                    else:
                        media = "image"
                    outs.append({
                        "filename": fn,
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                        "media": media,
                    })
    return outs


def normalize_object_info(oi):
    """A copy of one whole /object_info in which every V3 dropdown
    ["COMBO", {"options": [...], ...}] is rewritten to the legacy shape [[...], {...rest}].

    Why: a reader that knows only the legacy shape takes the V3 one for a typed value and
    never checks the file against the options (the blind spot the 2026-10-16 spike found:
    a background-removal template looked ready with a file the lane did not list). A
    "remote" combo (options fetched at run time) is left alone. Idempotent."""
    out = copy.deepcopy(oi)
    for node in out.values():
        inputs = node.get("input") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for group in ("required", "optional"):
            for name, spec in (inputs.get(group) or {}).items():
                if (isinstance(spec, list) and len(spec) > 1 and spec[0] == "COMBO" and isinstance(spec[1], dict)
                        and isinstance(spec[1].get("options"), list) and not spec[1].get("remote")):
                    inputs[group][name] = [spec[1]["options"], {k: v for k, v in spec[1].items() if k != "options"}]
    return out


def pool_names_from_object_info(oi, pool):
    """One pool's file names out of one WHOLE /object_info answer, by the same shape rules as
    server.py's pool_names: both dropdown shapes are read, an unrecognised one is skipped,
    and only things that look like files (a dot in the name) are kept."""
    names = []
    for node, field in POOL_NODES[pool]:
        spec = (oi or {}).get(node) or {}
        opts = ((spec.get("input") or {}).get("required") or {}).get(field)
        if not (isinstance(opts, list) and opts):
            continue
        if isinstance(opts[0], list):
            candidates = opts[0]
        elif opts[0] == "COMBO" and len(opts) > 1 and isinstance(opts[1], dict):
            candidates = opts[1].get("options") or []
        else:
            continue
        for o in candidates:
            if isinstance(o, str) and "." in o and o not in names:
                names.append(o)
    return names
