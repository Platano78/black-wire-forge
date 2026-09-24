"""Acceptance gate for the cleanup + 3D engine packs (cutout, upscale, mesh).

Same method as test_audio_golden.py: the pack is correct iff it reproduces
the FROZEN graph_builders.py output byte-for-byte, modulo exactly one
declared deviation per mode (R7's filename_prefix). Golden files were
captured straight from the frozen source by tests/capture_tools_golden.py --
never from the packs under test.

Also proves R1's risky seam: two packs (qwen-image, retouch) share cap
"image". describe()/abilities()/cap_from/mode_ability must all still behave,
and t2i/edit must be unaffected.

Run: python3 tests/test_tools_golden.py
"""
import json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
G = os.path.join(HERE, "golden")

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines

models = json.load(open(os.path.join(G, "tools_models.json")))
args = json.load(open(os.path.join(G, "tools_args.json")))

# mode -> (cap, golden file, node_id, frozen filename_prefix, pack filename_prefix)
CASES = {
    "cutout":  ("image", "tools_cutout",  "5",  "owui/cutout",  "blackwire/CUTOUT"),
    "upscale": ("image", "tools_upscale", "4",  "owui/upscale", "blackwire/UPSCALE"),
    "mesh":    ("3d",    "tools_mesh",    "27", "owui/mesh3d",  "blackwire/MESH3D"),
}


def diff_paths(want, got, prefix=()):
    out = []
    keys = set(want) | set(got)
    for k in keys:
        p = prefix + (k,)
        if k not in want or k not in got:
            out.append((p, want.get(k), got.get(k)))
        elif isinstance(want[k], dict) and isinstance(got[k], dict):
            out.extend(diff_paths(want[k], got[k], p))
        elif want[k] != got[k]:
            out.append((p, want[k], got[k]))
    return out


print("cutout/upscale/mesh graphs reproduce the frozen source exactly, modulo R7's filename_prefix")
for mode, (cap, golden_name, node, frozen_val, pack_val) in CASES.items():
    want = json.load(open(os.path.join(G, golden_name + ".json")))
    got = engines.graph_for(cap, mode, dict(args[mode]), models)
    diffs = diff_paths(want, got)
    allowlist_path = (node, "inputs", "filename_prefix")
    unexpected = [d for d in diffs if d[0] != allowlist_path]
    check("%s: no undeclared differences" % mode, not unexpected, str(unexpected[:3]))
    allowed = [d for d in diffs if d[0] == allowlist_path]
    check("%s: the ONLY difference is filename_prefix" % mode,
          allowed == [(allowlist_path, frozen_val, pack_val)], str(allowed))

print()
print("G2 falsifiability proof: perturb one pack value, confirm RED, then GREEN again")
import engines.cleanup as cleanup_mod
_orig = cleanup_mod.upscale_graph
def _broken(p, m):
    g = _orig(p, m)
    g["3"]["inputs"]["image"] = ["999", 0]   # perturb a value the golden pins
    return g
cleanup_mod.upscale_graph = _broken
cleanup_mod.ENGINE["graphs"]["upscale"] = _broken
engines._PACKS = None
want = json.load(open(os.path.join(G, "tools_upscale.json")))
got = engines.graph_for("image", "upscale", dict(args["upscale"]), models)
red = diff_paths(want, got)
red_unexpected = [d for d in red if d[0] != ("4", "inputs", "filename_prefix")]
print("  RED (perturbed):  %d unexpected diff(s): %s" % (len(red_unexpected), red_unexpected[:2]))
check("perturbation is caught (RED)", len(red_unexpected) > 0)

cleanup_mod.upscale_graph = _orig
cleanup_mod.ENGINE["graphs"]["upscale"] = _orig
engines._PACKS = None
got2 = engines.graph_for("image", "upscale", dict(args["upscale"]), models)
green = diff_paths(want, got2)
green_unexpected = [d for d in green if d[0] != ("4", "inputs", "filename_prefix")]
print("  GREEN (reverted): %d unexpected diff(s): %s" % (len(green_unexpected), green_unexpected[:2]))
check("revert is clean again (GREEN)", len(green_unexpected) == 0)

print()
print("R1's risky seam: two packs (qwen-image, cleanup) share cap 'image'")
pack_ids = [p["id"] for p in engines.packs()]
# The adversarial case D2 must survive: "cleanup" sorts BEFORE "qwen-image"
# alphabetically, yet must never shadow qwen-image's cap_word/cap_order --
# those must come from whichever pack actually DECLARES one, not the first
# pack discovered.
check("cleanup sorts before qwen-image (the adversarial ordering)",
      pack_ids.index("cleanup") < pack_ids.index("qwen-image"), str(pack_ids))
check("cap_word('image') is still 'picture' despite the adversarial order",
      engines.cap_word("image") == "picture", engines.cap_word("image"))
check("cap_order('image') is still qwen-image's declared 1 despite the adversarial order",
      engines.cap_order("image") == 1, str(engines.cap_order("image")))
# cleanup's OWN describe() is unit-tested directly (never "BiRefNet"/
# "Real-ESRGAN"); the merged engines.describe(models, "image") is NOT
# asserted "" here -- qwen-image's describe() is unconditionally truthy
# even with qwen_unet absent (a pre-existing quirk this port did not
# introduce and is out of scope to fix), so it still headlines regardless
# of order once cleanup's own describe correctly declines to.
cleanup_pack = next(p for p in engines.packs() if p["id"] == "cleanup")
check("cleanup's own describe() never names an engine (always '')",
      cleanup_pack["describe"]({"birefnet_model": "model.safetensors", "upscale_model": "x"}) == "")
check("modes_for('image') lists every image pack's modes (qwen-image, cleanup, pixelart)",
      sorted(engines.modes_for("image")) == sorted(["t2i", "edit", "cutout", "upscale", "pixelart"]),
      str(engines.modes_for("image")))
# R3-1: t2i/edit resolve to their OWN mode-specific abilities now, not the
# bare cap name -- see tests/test_ability_isolation.py for the full RED/
# GREEN proof of why (a cleanup-only lane could otherwise read t2i as
# available).
check("mode_ability('image','t2i') resolves to its own ability, unaffected by the second pack",
      engines.mode_ability("image", "t2i") == "t2i")
check("mode_ability('image','edit') resolves to its own ability, unaffected by the second pack",
      engines.mode_ability("image", "edit") == "edit")
check("mode_ability('image','cutout') resolves to its own ability, not qwen's",
      engines.mode_ability("image", "cutout") == "cutout")
check("mode_ability('image','upscale') resolves to its own ability, not qwen's",
      engines.mode_ability("image", "upscale") == "upscale")

qwen_only = {"qwen_unet": "qwen_image_2.1_Q6_K.gguf", "qwen_clip": "qwen3vl_8b_int8_convrot.safetensors",
             "qwen_vae": "qwen_image_2.1_vae_bf16.safetensors"}
able_qwen_only = engines.abilities(qwen_only)
check("t2i/edit still available on qwen files alone (cleanup absent)", able_qwen_only.get("image") is True)
check("cutout NOT available on qwen files alone", able_qwen_only.get("cutout") is not True)
check("upscale NOT available on qwen files alone", able_qwen_only.get("upscale") is not True)

cleanup_only = {"birefnet_model": "model.safetensors", "upscale_model": "RealESRGAN_x4plus.pth"}
able_cleanup_only = engines.abilities(cleanup_only)
check("image cap still True on cleanup files alone (cap_from OR)", able_cleanup_only.get("image") is True)
check("cutout available on cleanup files alone", able_cleanup_only.get("cutout") is True)
check("upscale available on cleanup files alone", able_cleanup_only.get("upscale") is True)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
