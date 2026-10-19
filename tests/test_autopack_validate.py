"""engines/_autopack.py validator (the COMBO-fixed check, spec section 3).

Contract under test:
  * a model file outside a V3 COMBO enum is found (the spike's blind spot), where the
    prototype's legacy-only check lets it pass -- both assertions stay, side by side;
  * upload pickers, links and CustomCombo.choice are never enum-checked; a dotted child of a
    dynamic combo is checked against the OPTION CHOSEN in its parent;
  * a literal INT given as a string is a warning, an out-of-range number an error;
  * billed classes and unknown classes are listed; the graph is pruned to what an output needs;
  * required autogrow / dynamic inputs are never "missing".

Run: python3 tests/test_autopack_validate.py
"""
import copy, os, sys, tempfile
os.environ["GENCENTER_DATA"] = tempfile.mkdtemp(prefix="bwf-apv-")
import _autopack_fixture as fx_mod
from fixtures.autopack.legacy_enum_check import legacy_enum_problems
ap, _comfy = fx_mod.ap, fx_mod._comfy

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

fx = fx_mod.Fx()
OI = fx.oi


def N(cls, **inputs):
    return {"class_type": cls, "inputs": inputs}


def clean(g):
    return ap.validate_api_graph(g, OI)


print("every public replica's converted graph validates (billed aside)")
for name in fx_mod.NAMES:
    r = clean(fx.api(name))
    expect_billed = name == "image_billed"
    check("%s: no errors, no unknown classes%s" % (name, ", billed" if expect_billed else ""),
          not r["errors"] and not r["unknown_classes"] and bool(r["billed"]) == expect_billed, r)

print("the V3 COMBO blind spot: fixed here, characterised in the legacy check")
bg = {"1": N("LoadBackgroundRemovalModel", bg_removal_name="other.safetensors"),
      "2": N("PreviewAny", source=["1", 0])}
r = clean(bg)
check("missing_files has the V3 file", r["missing_files"] == [{"node": "1", "class": "LoadBackgroundRemovalModel",
                                                                 "input": "bg_removal_name", "value": "other.safetensors"}], r)
check("the legacy-only check lets it pass (blind spot, kept as a characterisation)",
      legacy_enum_problems(bg, OI) == [])
bg_ok = copy.deepcopy(bg); bg_ok["1"]["inputs"]["bg_removal_name"] = "model.safetensors"
check("a listed V3 file is fine", clean(bg_ok)["missing_files"] == [])
check("the legacy check does see a LEGACY dropdown miss",
      legacy_enum_problems({"1": N("UNETLoader", unet_name="nope.safetensors", weight_dtype="default")}, OI)
      == [("1", "unet_name", "nope.safetensors")])
check("validating a normalised copy gives the same answer", ap.validate_api_graph(bg, _comfy.normalize_object_info(OI)) == r)
check("the object_info passed in is not modified", OI == fx_mod.load(os.path.join(fx_mod.FIX, "object_info.json")))

print("what is not enum-checked")
g = {"1": N("LoadImage", image="never-listed.png"), "2": N("RunDA3", da3=["3", 0], image=["1", 0]),
     "3": N("LoadDA3Model", model_name="other_geometry.safetensors"), "4": N("SaveImage", images=["2", 0], filename_prefix="x")}
check("an upload picker may name any file", clean(g)["errors"] == [] and clean(g)["missing_files"] == [], clean(g))
g2 = {"1": N("CustomCombo", choice="anything"), "2": N("PreviewAny", source=["1", 0])}
check("CustomCombo.choice is frontend-defined", clean(g2)["errors"] == [], clean(g2))
g3 = {"1": N("LoadDA3Model", model_name=["9", 0]), "2": N("PreviewAny", source=["1", 0])}
check("a link into an enum input is not compared to the options (the link check speaks)",
      clean(g3)["missing_files"] == [] and any("non-existent node" in e for e in clean(g3)["errors"]), clean(g3))

print("dotted child of a dynamic combo")
aud = lambda **k: {"1": N("SaveAudioAdvanced", audio=["2", 0], filename_prefix="a", **k), "2": N("CustomCombo", choice="x")}
check("format=mp3 accepts format.quality=V0", clean(aud(format="mp3", **{"format.quality": "V0"}))["errors"] == [])
bad = clean(aud(format="mp3", **{"format.quality": "9k"}))
check("a value outside the chosen option's enum is an error (not a model file)",
      any("format.quality" in e for e in bad["errors"]) and bad["missing_files"] == [], bad)
check("flac declares no quality: the child is unknown and does not pass silently",
      clean(aud(format="flac", **{"format.quality": "V0"}))["errors"] != [] or
      clean(aud(format="flac", **{"format.quality": "V0"}))["warnings"] != [])

print("types, ranges, warnings")
ks = fx.api("image_flat_t2i")
s = copy.deepcopy(ks); s["7"]["inputs"]["steps"] = "20"
r = clean(s)
check("an INT given as a string is a warning, not an error", r["errors"] == [] and any("steps" in w for w in r["warnings"]), r)
s = copy.deepcopy(ks); s["7"]["inputs"]["cfg"] = 500.0
check("a number out of range is an error", any("cfg" in e for e in clean(s)["errors"]), clean(s))
s = copy.deepcopy(ks); s["7"]["inputs"]["steps"] = 2.5
check("a fractional INT is an error", any("steps" in e for e in clean(s)["errors"]), clean(s))
s = copy.deepcopy(ks); del s["7"]["inputs"]["latent_image"]
check("a missing required input is an error", any("latent_image" in e for e in clean(s)["errors"]), clean(s))
s = copy.deepcopy(ks); s["7"]["inputs"]["model"] = ["99", 0]
check("a link to a missing node is an error", any("99" in e for e in clean(s)["errors"]), clean(s))
s = copy.deepcopy(ks); s["7"]["inputs"]["model"] = ["1", 5]
check("a link past the node's outputs is an error", any("slot" in e for e in clean(s)["errors"]), clean(s))
s = copy.deepcopy(ks); s["1"]["inputs"]["unet_name"] = "gone.safetensors"
r = clean(s)
check("a model file outside a legacy dropdown is missing_files", [m["value"] for m in r["missing_files"]] == ["gone.safetensors"], r)
s = copy.deepcopy(ks); s["7"]["inputs"]["sampler_name"] = "bogus"
r = clean(s)
check("a non-file value outside a dropdown is an error, not missing_files",
      r["missing_files"] == [] and any("sampler_name" in e for e in r["errors"]), r)
s = copy.deepcopy(ks); s["4"]["inputs"]["not_an_input"] = 1
check("an input the schema does not know is a warning (the backend ignores it)", any("not_an_input" in w for w in clean(s)["warnings"]))

print("classes")
r = clean(fx.api("image_billed"))
check("a billed class is listed", r["billed"] == ["PartnerPaidImage"], r)
r = clean({"1": N("NoSuchNode"), "2": N("SaveImage", images=["1", 0], filename_prefix="x")})
check("an unknown class is listed", r["unknown_classes"] == ["NoSuchNode"], r)
r = clean({"1": N("CLIPTextEncode", text="a", clip=["9", 0])})
check("no output node is reported", any("output" in e for e in r["errors"]), r)

print("autogrow and dynamic inputs")
ml = fx.api("video_multi_ref_switch")
r = clean(ml)
check("values.value0 on the autogrow math node is neither unknown nor missing", r["errors"] == [] and r["warnings"] == [], r)
m = {"1": N("ComfyMathExpression", expression="a"), "2": N("PreviewAny", source=["1", 0])}
check("a required autogrow field with no slot is not 'missing'", clean(m)["errors"] == [], clean(m))

print("prune_to_outputs")
pg, has = ap.prune_to_outputs(fx.api("image_sub_edit"), OI)
check("only what an output needs survives (PreviewAny's branch stays: it is an output itself)", has and "40" in pg and "24" in pg)
pg2, has2 = ap.prune_to_outputs({"1": N("CLIPTextEncode", text="a", clip=["2", 0])}, OI)
check("no output node: nothing to prune to", has2 is False and "1" in pg2)
extra = copy.deepcopy(fx.api("image_flat_t2i")); extra["77"] = N("EmptyLatentImage", width=1, height=1, batch_size=1)
pg3, _ = ap.prune_to_outputs(extra, OI)
check("an unreachable node is dropped", "77" not in pg3 and len(pg3) == 9)

print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for n in FAILED: print("  -", n)
    sys.exit(1)
print("all passed")
