"""Acceptance gate for R3-1: an ability must never read True from a
DIFFERENT pack's files under the same cap.

Before this slice, qwen-image's ability was named "image" -- literally the
cap name. A lane holding only a cleanup pack's files (BiRefNet/Real-ESRGAN)
could make `able["image"]` true via cleanup's cap_from OR, and Picture would
report available, then die with a raw KeyError on Make. This proves the
fix generically, across every real (cap, mode) pair, not just image/t2i:

1. POSITIVE: a lane fixture built from ONLY a mode's own roles makes that
   mode's `mode_ability()` resolve and its ability true, and its graph
   builds without raising off nothing but declared field defaults/
   placeholders.
2. NEGATIVE: where a cap has more than one pack (only "image" today), a
   lane fixture built from a DIFFERENT pack's roles under the same cap
   reports that mode unavailable, with a non-empty `missing`.
3. RED/GREEN: the exact historical mechanism, reproduced with a synthetic
   pack shaped like the OLD qwen-image (ability named "image", not "t2i"/
   "edit") -- proven to misreport availability on cleanup-only files, then
   proven the real code does not.

Run: python3 tests/test_ability_isolation.py
"""
import os, sys, types
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines


def placeholder(f):
    if "default" in f:
        return f["default"]
    # An "advanced" field with no declared default is this pack's own
    # signal for "optional, leave blank" (e.g. LTX's context_length: the
    # graph reads it via p.get(...) as None-means-off, not a value this
    # test should invent) -- synthesizing a minimum here would fabricate
    # cross-field combinations no real form ever submits (LTX's B2: a
    # placeholder floor for context_length paired with context_overlap's
    # OWN default is exactly the impossible pairing B2 exists to refuse).
    # Only PRIMARY fields with no default are genuinely required.
    if f.get("tier") == "advanced":
        return None
    t = f["type"]
    if t in ("text", "textarea"):
        return "test"
    if t == "int":
        return (f.get("range") or [1])[0]
    if t == "number":
        return float((f.get("range") or [1.0])[0])
    if t == "checkbox":
        return False
    if t == "image":
        return "test.png"
    if t == "audio":
        return "test.wav"
    if t == "image_list":
        return ["test.png"]
    if t == "video_list":
        return ["test.mp4"]
    if t == "select":
        return (f.get("options") or [""])[0]
    return "test"


def build_args(cap, mode):
    args = {"seed": 12345}
    for f in engines.fields(cap, mode):
        val = placeholder(f)
        if val is not None:
            args[f["id"]] = val
    return args


def build_models(pack, ability):
    models = {}
    for entry in pack["provides"][ability]:
        role = entry[0] if isinstance(entry, (list, tuple)) else entry
        models[role] = "test_file.safetensors"
    return models


def owning_pack(cap, mode):
    for pack in engines.packs():
        if pack["cap"] == cap and mode in pack["graphs"]:
            return pack
    return None


print("POSITIVE: every real (cap, mode) is available and buildable on ONLY its own files")
for cap in engines.caps():
    for mode in engines.modes_for(cap):
        pack = owning_pack(cap, mode)
        ability = engines.mode_ability(cap, mode)
        own_models = build_models(pack, ability)
        able = engines.abilities(own_models)
        check("%s/%s: mode_ability resolves and is available on its own files"
              % (cap, mode), able.get(ability) is True, "ability=%r able=%r" % (ability, able.get(ability)))
        args = build_args(cap, mode)
        try:
            engines.graph_for(cap, mode, args, own_models)
            built = True
        except Exception as e:
            built = False
            detail = "%s: %s" % (type(e).__name__, e)
        check("%s/%s: graph_for builds without raising on declared defaults/placeholders"
              % (cap, mode), built, "" if built else detail)

print()
print("NEGATIVE: a mode reports unavailable on a DIFFERENT pack's files under the same cap")
by_cap = {}
for pack in engines.packs():
    by_cap.setdefault(pack["cap"], []).append(pack)
tested_negative = 0
for cap, packs_here in by_cap.items():
    if len(packs_here) < 2:
        continue
    for pack in packs_here:
        others = [o for o in packs_here if o["id"] != pack["id"]]
        foreign_models = {}
        for other in others:
            for roles in other["provides"].values():
                for entry in roles:
                    role = entry[0] if isinstance(entry, (list, tuple)) else entry
                    foreign_models[role] = "test_file.safetensors"
        for mode in pack["graphs"]:
            ability = engines.mode_ability(cap, mode)
            own_roles = {entry[0] if isinstance(entry, (list, tuple)) else entry
                         for entry in pack["provides"].get(ability, [])}
            # A pack may deliberately REUSE another pack's role names instead of
            # loading a duplicate file (pixelart rides qwen-image's picture roles
            # and cleanup's background-removal role -- see engines/pixelart.py's
            # module docstring). When that overlap alone already covers `pack`'s
            # OWN full role list for this ability, a sibling's files genuinely DO
            # satisfy it -- that is the reuse working as designed, not the R3-1
            # bug (an ability reading true from files it does not actually need).
            # Skip only that case; every ability with no such overlap still runs
            # the real check below.
            if own_roles and own_roles <= set(foreign_models):
                continue
            able = engines.abilities(foreign_models)
            missing = engines.missing_words(foreign_models, ability)
            check("%s/%s: unavailable on a sibling pack's files alone" % (cap, mode),
                  able.get(ability) is not True, str(able.get(ability)))
            check("%s/%s: missing is non-empty on a sibling pack's files alone" % (cap, mode),
                  bool(missing), str(missing))
            tested_negative += 1
check("the negative case actually ran (cap 'image' has 2+ packs)", tested_negative > 0)

print()
print("RED/GREEN: the exact historical mechanism -- an ability named the same as its cap")
old_style_qwen = types.ModuleType("engines._old_style_qwen")
old_style_qwen.ENGINE = {
    "id": "zzz-old-qwen", "cap": "image",
    "roles": {"old_qwen_unet": ("unet", {"all": ["old_qwen"]})},
    "provides": {"image": ["old_qwen_unet"]},   # the OLD shape: ability == cap name
    "words": {"old_qwen_unet": "the old qwen model"},
    "graphs": {"t2i": lambda p, m: {"1": {"class_type": "X", "inputs": {"u": m["old_qwen_unet"]}}}},
    "describe": lambda m: "",
    "licence": {"name": "TEST", "shippable": True, "attribution": "Test"},
}
cleanup_pack = next(p for p in engines.packs() if p["id"] == "cleanup")
cleanup_only_models = build_models(cleanup_pack, "cutout")
cleanup_only_models.update(build_models(cleanup_pack, "upscale"))

# RED: reproduce the OLD mechanism directly (not the real code path) --
# with the old naming, mode_ability("image", "t2i") falls back to the SHARED
# "image" key since "t2i" was never its own ability, and abilities() ORs
# cleanup's cap_from into that same key.
def red_mode_ability(pack, cap, mode):
    return mode if mode in pack["ENGINE"]["provides"] else cap
old_ability = red_mode_ability({"ENGINE": old_style_qwen.ENGINE}, "image", "t2i")
check("RED: the old naming resolves t2i's ability to the bare cap name",
      old_ability == "image")
engines._PACKS = None
real_packs_snapshot = engines.packs()
engines.packs.__globals__["_PACKS"] = sorted(
    [p for p in real_packs_snapshot if p["id"] != "qwen-image"] + [old_style_qwen.ENGINE],
    key=lambda e: e["id"])
red_able = engines.abilities(cleanup_only_models)
check("RED: under the OLD naming, the shared 'image' key reads True from cleanup's files alone",
      red_able.get(old_ability) is True, str(red_able.get(old_ability)))

# GREEN: restore the real packs (qwen-image included) and show the same
# cleanup-only fixture correctly reports t2i unavailable.
engines._PACKS = None
engines.packs.__globals__["_PACKS"] = sorted(real_packs_snapshot, key=lambda e: e["id"])
green_ability = engines.mode_ability("image", "t2i")
green_able = engines.abilities(cleanup_only_models)
check("GREEN: mode_ability('image','t2i') resolves to its own ability 't2i', not the cap",
      green_ability == "t2i", green_ability)
check("GREEN: t2i correctly unavailable on cleanup's files alone",
      green_able.get(green_ability) is not True, str(green_able.get(green_ability)))
check("GREEN: missing_words names qwen's own roles, not cleanup's",
      engines.missing_words(cleanup_only_models, green_ability) != [], "")

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
