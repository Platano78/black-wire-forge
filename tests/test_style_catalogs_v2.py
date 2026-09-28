"""LORA-2A acceptance gate: match rules against the real-rig LoRA pool fixture
(spec-A-engines.md point 3) and engines.style_catalogs()'s CONTRACT v2 shape.
Run: python3 tests/test_style_catalogs_v2.py"""
import json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines


def matches(rule, path):
    """The documented match rule: case-insensitive substring on "any",
    excluding any "none" substring."""
    p = path.lower()
    if any(bad.lower() in p for bad in rule.get("none", [])):
        return False
    return any(good.lower() in p for good in rule.get("any", []))


CATALOGS = {sc["id"]: sc for sc in engines.style_catalogs()}
POOL = json.load(open(os.path.join(HERE, "fixtures", "pools_extra.json")))["lora"]["LoraLoaderModelOnly.lora_name"][0]

print("match rules against the real-rig pool fixture")
qwen_hits = [f for f in POOL if matches(CATALOGS["qwen_image"]["match"], f)]
check("qwen_image lists only the qwen style file", qwen_hits == ["test_style_qwen_image.safetensors"], qwen_hits)
h3_hits = [f for f in POOL if matches(CATALOGS["minimax_h3"]["match"], f)]
check("minimax_h3 lists NOTHING here -- every real minimax_h3 file in this fixture is a turbo/speed LoRA",
      h3_hits == [], h3_hits)
ltx_hits = [f for f in POOL if matches(CATALOGS["ltx25"]["match"], f)]
check("ltx25 excludes the Licon-MSR file (it's the LTX Multiple-Subject-Reference pack, "
      "a different workflow -- orchestrator ruling, see report-A2.md)",
      ltx_hits == [], ltx_hits)
for turbo_file in ("minimax_h3/lightx2v_hybrid-4to8step-Turbo_r48.safetensors",
                   "minimax_h3/loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
                   "minimax_h3/loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
                   "minimax_h3/loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"):
    check("turbo file matches no family: %s" % turbo_file,
          not any(matches(sc["match"], turbo_file) for sc in CATALOGS.values()), turbo_file)
check("an unrelated file (pixel-sdxl) matches no family",
      not any(matches(sc["match"], "pixel-sdxl/pixel-art-xl.safetensors") for sc in CATALOGS.values()))

print()
print("style_catalogs(): the v2 list carries all six declared families with the spec's exact values")
WANT = {
    "qwen_image": ("Qwen-Image", "Qwen/Qwen-Image-2.1", "qwen_unet"),
    "ltx25": ("LTX-2.5", "Lightricks/LTX-2.5", "ltx_transformer"),
    "minimax_h3": ("MiniMax-H3", "MiniMaxAI/MiniMax-H3", "h3_unet_fl2va"),
    "ace_step15": ("ACE-Step 1.5", "ACE-Step/Ace-Step1.5", "ace_unet"),
    "music3": ("MiniMax-Music3", "MiniMaxAI/MiniMax-Music3", "music3_unet"),
    "yue2": ("YuE2", "m-a-p/YuE2-3B", "yue2_ckpt"),
}
check("exactly the six expected family ids", set(CATALOGS) == set(WANT), set(CATALOGS))
for fam_id, (label, hf_base, role) in WANT.items():
    sc = CATALOGS.get(fam_id)
    check("%s: present" % fam_id, sc is not None)
    if sc:
        check("%s: label/hf_base/role" % fam_id,
              sc["label"] == label and sc["hf_base"] == hf_base and sc["role"] == role, sc)
        check("%s: folder defaults to its own id" % fam_id, sc.get("folder") == fam_id, sc.get("folder"))
        check("%s: carries a 'none' exclusion list" % fam_id, "none" in sc.get("match", {}), sc.get("match"))

print()
print("style_catalogs(): a legacy single 'style_catalog' dict pack is still converted (backward compat)")
_orig_discover = engines._discover
FAKE_PACK = {
    "id": "fake-legacy", "cap": "image",
    "graphs": {"modeA": lambda a, m: {}, "modeB": lambda a, m: {}},
    "style_catalog": {"role": "fake_role", "match": {"any": ["fake"]}, "hf_base": "Fake/Base"},
}
engines._discover = lambda: [FAKE_PACK]
try:
    legacy_out = engines.style_catalogs()
finally:
    engines._discover = _orig_discover
check("exactly one converted entry", len(legacy_out) == 1, legacy_out)
if legacy_out:
    entry = legacy_out[0]
    check("id/cap default from the pack", entry["id"] == "fake_legacy" and entry["cap"] == "image", entry)
    check("modes defaults to every mode the pack provides", set(entry["modes"]) == {"modeA", "modeB"}, entry)
    check("role/match/hf_base carried through unchanged",
          entry["role"] == "fake_role" and entry["match"] == {"any": ["fake"]} and entry["hf_base"] == "Fake/Base", entry)
check("the real packs are unaffected after restoring _discover",
      {sc["id"] for sc in engines.style_catalogs()} == set(WANT))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
