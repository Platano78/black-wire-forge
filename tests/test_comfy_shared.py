"""engines/_comfy.py: the pool / ranking / output helpers moved out of server.py with no
behaviour change, plus the two new helpers the pack builder uses.

Contract under test:
  * server.py re-imports each moved symbol under its OLD name, and it is the same object
    (so every existing test that patches or calls srv.POOL_NODES / srv._rank / ... still works);
  * engines._discover() does not take _comfy for a pack (leading underscore);
  * normalize_object_info rewrites V3 COMBO specs to the legacy shape, is idempotent, works
    on a copy and leaves "remote" combos alone;
  * pool_names_from_object_info reads one whole /object_info the way server.py's pool_names
    reads per-node answers.

Run: python3 tests/test_comfy_shared.py
"""
import copy, importlib.util, json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import _scratch_config  # noqa: E402,F401 -- before server.py's own exec_module below
import engines  # noqa: E402
from engines import _comfy  # noqa: E402

spec = importlib.util.spec_from_file_location("srv_comfy_shared", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

print("the move is a move: same objects under the old names")
check("POOL_NODES is the _comfy dict", srv.POOL_NODES is _comfy.POOL_NODES)
check("_rank is _comfy.rank", srv._rank is _comfy.rank)
check("_rule_matches is _comfy.rule_matches", srv._rule_matches is _comfy.rule_matches)
check("small_card_for is _comfy.small_card_for", srv.small_card_for is _comfy.small_card_for)
check("collect_outputs is _comfy.collect_outputs", srv.collect_outputs is _comfy.collect_outputs)
check("SMALL_QUANTS / BIG_QUANTS are the _comfy tuples",
      srv.SMALL_QUANTS is _comfy.SMALL_QUANTS and srv.BIG_QUANTS is _comfy.BIG_QUANTS)
check("pool names unchanged", list(_comfy.POOL_NODES) == ["unet", "clip", "vae", "lora", "checkpoint", "audio_encoder",
                                                           "bg_removal", "upscale_model", "clip_vision", "latent_upscaler"])
check("engine discovery does not take _comfy for a pack", all(p["id"] != "_comfy" for p in engines.packs()))

print("rank / rule_matches / small_card_for behave as before")
rule = {"all": ["m"], "prefer": ["int8"]}
check("rank: preferred term and quant outrank a bare name",
      _comfy.rank("m_int8.safetensors", rule, True) > _comfy.rank("m_bf16.safetensors", rule, True))
check("rank: shorter name wins a tie", _comfy.rank("m.a", {}, False) > _comfy.rank("m.abc", {}, False))
check("rule_matches: all / none / any", _comfy.rule_matches(["a_x", "a_y", "b_x"], {"all": ["a"], "none": ["y"]}) == ["a_x"]
      and _comfy.rule_matches(["a", "b", "c"], {"any": ["b", "c"]}) == ["b", "c"])
check("small_card_for: unknown counts as small", _comfy.small_card_for(None) and not _comfy.small_card_for(32e9))
check("collect_outputs skips non-output entries and classes media",
      _comfy.collect_outputs({"outputs": {"1": {"images": [{"filename": "a.mp4", "type": "input"},
                                                            {"filename": "b.glb", "type": "output"}]}}})
      == [{"filename": "b.glb", "subfolder": "", "type": "output", "media": "3d"}])

print("normalize_object_info")
V3 = {"X": {"input": {"required": {"a": ["COMBO", {"options": ["p.safetensors", "q.safetensors"], "default": "p.safetensors"}],
                                   "r": ["COMBO", {"options": ["x"], "remote": {"route": "/r"}}],
                                   "n": ["INT", {"min": 1}]},
                      "optional": {"b": ["COMBO", {"options": ["o"]}]}}}}
before = copy.deepcopy(V3)
once = _comfy.normalize_object_info(V3)
check("the input is not modified", V3 == before)
check("a V3 COMBO becomes [[options], rest]", once["X"]["input"]["required"]["a"] == [["p.safetensors", "q.safetensors"], {"default": "p.safetensors"}])
check("optional inputs too", once["X"]["input"]["optional"]["b"] == [["o"], {}])
check("remote combos are skipped", once["X"]["input"]["required"]["r"][0] == "COMBO")
check("other types untouched", once["X"]["input"]["required"]["n"] == ["INT", {"min": 1}])
check("idempotent", _comfy.normalize_object_info(once) == once)

print("pool_names_from_object_info")
OI = {"UNETLoader": {"input": {"required": {"unet_name": [["a.safetensors", "dir/b.gguf", "not_a_file", "a.safetensors"], {}]}}},
      "UnetLoaderGGUF": {"input": {"required": {"unet_name": ["COMBO", {"options": ["c.gguf", "a.safetensors"]}]}}},
      "VAELoader": {"input": {"required": {"vae_name": [["pixel_space", "v.safetensors"]]}}},
      "LoraLoaderModelOnly": {"input": {"required": {"lora_name": ["STRING", {}]}}}}
check("both dropdown shapes, files only, no duplicates",
      _comfy.pool_names_from_object_info(OI, "unet") == ["a.safetensors", "dir/b.gguf", "c.gguf"])
check("a pseudo-entry without a dot is not a file", _comfy.pool_names_from_object_info(OI, "vae") == ["v.safetensors"])
check("an unrecognised shape yields nothing", _comfy.pool_names_from_object_info(OI, "lora") == [])
check("a node the lane lacks yields nothing", _comfy.pool_names_from_object_info(OI, "clip") == [])

print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for n in FAILED: print("  -", n)
    sys.exit(1)
print("all passed")
