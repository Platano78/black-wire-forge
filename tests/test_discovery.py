"""Acceptance gate for role discovery (D3, round-1 review of slice A).

fetch_pool() and pick_model() had no committed test: the V3-COMBO fix this
slice exists for, and the 11 new audio roles, were both unverified by
anything that runs again. This loads tests/golden/pools_rig.json -- REAL
dropdown payloads captured off the live rig (tests/capture_pools_rig.py,
GET-only, same provenance discipline as the audio golden) -- and drives
fetch_pool()/pick_model() against it with server.py's actual HTTP call
monkeypatched to serve the fixture, so the code under test is the real code,
not a reimplementation of it.

Run: python3 tests/test_discovery.py
"""
import importlib.util, json, os, sys
sys.dont_write_bytecode = True  # a same-length source edit leaves file SIZE
# unchanged, so .pyc invalidation (mtime+size) can serve stale bytecode for code
# you just changed -- observed 2026-09-22 reporting a defect already reverted.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv_disco", os.path.join(ROOT, "server.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

FIXTURE = json.load(open(os.path.join(HERE, "golden", "pools_rig.json")))

# server.py's POOL_NODES keys are ("Node", "field") pairs, per pool. The
# fixture is keyed the same way: pools_rig.json[pool]["Node.field"] -> opts.
def fake_http_get_json(url, timeout=8.0):
    # url looks like http://host:port/object_info/<Node>
    node = url.rsplit("/", 1)[-1]
    for pool, entries in FIXTURE.items():
        for key, opts in entries.items():
            if key.split(".")[0] == node:
                return {node: {"input": {"required": {key.split(".", 1)[1]: opts}}}}
    return {}

LANE = {"id": "rig", "name": "Rig", "host": "10.0.0.50", "port": 8188}

print("fetch_pool handles both real shapes off the live rig")
_orig_http = m.http_get_json
m.http_get_json = fake_http_get_json
try:
    unet_pool = m.fetch_pool(LANE, "unet")
    clip_pool = m.fetch_pool(LANE, "clip")
    vae_pool = m.fetch_pool(LANE, "vae")
    checkpoint_pool = m.fetch_pool(LANE, "checkpoint")
    audio_encoder_pool = m.fetch_pool(LANE, "audio_encoder")

    check("unet pool (legacy shape) is non-empty", len(unet_pool) > 0, str(unet_pool[:3]))
    check("clip pool (legacy shape, merged CLIPLoader+DualCLIPLoader) is non-empty",
          len(clip_pool) > 0, str(clip_pool[:3]))
    check("checkpoint pool (legacy shape) is non-empty", len(checkpoint_pool) > 0)
    check("audio_encoder pool (V3 COMBO shape) finds sheetsage2",
          "sheetsage2_bf16.safetensors" in audio_encoder_pool, str(audio_encoder_pool))

    print()
    print("the V3 branch can FAIL: prove it by removing it and re-running")
    def pre_fix_fetch_pool(lane, pool):
        # The pre-D-R4 guard: only isinstance(opts[0], list) was accepted.
        names = []
        for node, field in m.POOL_NODES[pool]:
            try:
                info = m.http_get_json(m.lane_url(lane, "/object_info/%s" % node), timeout=8.0)
            except Exception:
                continue
            spec_ = (info or {}).get(node) or {}
            opts = ((spec_.get("input") or {}).get("required") or {}).get(field)
            if not (isinstance(opts, list) and opts and isinstance(opts[0], list)):
                continue
            for o in opts[0]:
                if isinstance(o, str) and "." in o and o not in names:
                    names.append(o)
        return names
    pre_fix_pool = pre_fix_fetch_pool(LANE, "audio_encoder")
    check("pre-fix logic loses sheetsage2 (the exact defect this slice fixes)",
          "sheetsage2_bf16.safetensors" not in pre_fix_pool, str(pre_fix_pool))
    check("post-fix code recovers it", "sheetsage2_bf16.safetensors" in audio_encoder_pool)

    print()
    print("an unrecognised THIRD shape is logged and skipped, not silently dropped")
    logged = []
    _orig_log = m.log
    m.log = lambda text, level="info": logged.append((level, text))
    def weird_http_get_json(url, timeout=8.0):
        node = url.rsplit("/", 1)[-1]
        if node == "CheckpointLoaderSimple":
            return {node: {"input": {"required": {"ckpt_name": ["WEIRD_SHAPE", {"nope": True}]}}}}
        return {}
    m.http_get_json = weird_http_get_json
    weird_pool = m.fetch_pool(LANE, "checkpoint")
    m.log = _orig_log
    check("an unrecognised shape yields no candidates", weird_pool == [])
    check("an unrecognised shape is LOGGED (not silent)",
          any("unrecognised dropdown shape" in text for _, text in logged), str(logged))

    print()
    print("pick_model resolves all 11 audio roles to the expected live-rig filenames")
    m.http_get_json = fake_http_get_json
    small_card = True   # 5080 16GB, matches the small-card branch on this rig
    pools = {p: m.fetch_pool(LANE, p) for p in m.POOL_NODES}
    want_audio = {
        "ace_unet": "ace_step_1.5/acestep_v1.5_turbo.safetensors",
        "ace_clip1": "ace_step_1.5/qwen_0.6b_ace15.safetensors",
        "ace_clip2": "ace_step_1.5/qwen_1.7b_ace15.safetensors",
        "ace_vae": "ace_step_1.5/ace_1.5_vae.safetensors",
        "music3_unet": "minimax_music3/minimax_music3_dit_int8_convrot.safetensors",
        "music3_clip": "minimax_music3/minimax_music3_text_encoder_pruned_int8_convrot.safetensors",
        "music3_vae": "minimax_music3/minimax_music3_dav.safetensors",
        "sao_ckpt": "stable-audio-open-1.0/stable-audio-open-1.0.safetensors",
        "sao_clip": "stable-audio-open-1.0/t5-base.safetensors",
        "yue2_ckpt": "yue2_3b_bf16.safetensors",
        "yue2_audio_encoder": "sheetsage2_bf16.safetensors",
    }
    for role, want in want_audio.items():
        got = m.pick_model(pools[m.ROLE_POOL[role]], m.ROLE_RULES[role], small_card)
        check("role %s -> %s" % (role, want), got == want, "got %r" % (got,))

    print()
    print("pick_model resolves the new cleanup/3D roles (R4) to the expected live-rig filenames")
    want_tools = {
        "birefnet_model": "model.safetensors",
        "upscale_model": "RealESRGAN_x4plus.pth",
        "trellis_unet": "trellis2/trellis_2_int8_convrot.safetensors",
        "trellis_shape_vae": "trellis2/trellis_2_shape_vae_bf16.safetensors",
        "trellis_texture_vae": "trellis2/trellis_2_texture_vae_bf16.safetensors",
        "trellis_clip_vision": "trellis2/dino_v3_vit_l.safetensors",
    }
    for role, want in want_tools.items():
        got = m.pick_model(pools[m.ROLE_POOL[role]], m.ROLE_RULES[role], small_card)
        check("role %s -> %s" % (role, want), got == want, "got %r" % (got,))

    print()
    print("every pre-existing image/video role still resolves to exactly what it")
    print("resolved to before this change -- a collision would steal one")
    print("of these from H3 or Qwen")
    want_preexisting = {
        "qwen_unet": "qwen_image_2.1_Q6_K.gguf",
        "qwen_clip": "qwen3vl_8b_int8_convrot.safetensors",
        "qwen_vae": "qwen_image_2.1_vae_bf16.safetensors",
        "h3_unet_fl2va": "minimax_h3/MiniMax-H3-FL2VA-Pruned-Q4_K_M.gguf",
        "h3_unet_ref2va": "minimax_h3/MiniMax-H3-Ref2VA-Pruned-Q4_K_M.gguf",
        "h3_clip_nvfp4": "minimax_h3/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "h3_clip_int8": "minimax_h3/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "h3_vae_video": "minimax_h3/vae/minimax_h3_video_vae_fp16.safetensors",
        "h3_vae_audio": "minimax_h3/vae/minimax_h3_audio_vae_fp32.safetensors",
        "h3_turbo_lora": "minimax_h3/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
    }
    for role, want in want_preexisting.items():
        got = m.pick_model(pools[m.ROLE_POOL[role]], m.ROLE_RULES[role], small_card)
        check("pre-existing role %s unchanged -> %s" % (role, want), got == want, "got %r" % (got,))

    print()
    print("the positive availability case is pinned, not just the negative one")
    all_audio_models = json.load(open(os.path.join(HERE, "golden", "audio_models.json")))
    import engines
    able = engines.abilities(all_audio_models)
    for mode in ("song", "music", "sfx", "yue2", "cover", "audio"):
        check("abilities()['%s'] is True with all audio models present" % mode, able.get(mode) is True)

    all_tools_models = json.load(open(os.path.join(HERE, "golden", "tools_models.json")))
    able_tools = engines.abilities(all_tools_models)
    for mode in ("cutout", "upscale", "mesh", "3d"):
        check("abilities()['%s'] is True with all cleanup/3D models present" % mode,
              able_tools.get(mode) is True)

finally:
    m.http_get_json = _orig_http

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
