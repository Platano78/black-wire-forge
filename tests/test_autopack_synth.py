"""engines/_autopack.py synthesis (spec section 4): ComfyUI template -> draft pack.

Contract under test, on 8 hand-made structural replicas of real templates
(tests/fixtures/autopack/: same classes, links, colon ids, proxyWidgets; our own text and
placeholder file names):
  * GOLDEN: expected/<name>.manifest.json + .flags.json are frozen. Only
    tests/capture_autopack_golden.py writes them; changing one needs a stated reason;
  * the rules the goldens cannot show: refusals, ids and modes, the exact-stem-first model
    rule, substitution acks, one-attempt eligibility, nothing bound twice;
  * every synthesized manifest is accepted by BWF's own loader (cap 3d aside until the loader
    learns it) and a pack that is not blocked builds a graph the validator accepts.

Run: python3 tests/test_autopack_synth.py
"""
import copy, json, os, sys, tempfile
os.environ["GENCENTER_DATA"] = tempfile.mkdtemp(prefix="bwf-aps-")
import _autopack_fixture as fx_mod
ap, _comfy = fx_mod.ap, fx_mod._comfy
from engines import _userpacks as up

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

fx = fx_mod.Fx()
EXP = os.path.join(fx_mod.FIX, "expected")
D = {n: fx.synth(n) for n in fx_mod.NAMES if n != "image_billed"}


def fids(d):
    return [f["id"] for f in d["manifest"]["fields"]]


def flags(d):
    return {f["id"]: f["kind"] for f in d["flags"]}


def field(d, fid):
    return next(f for f in d["manifest"]["fields"] if f["id"] == fid)


print("golden manifests and flags (frozen)")
for n, d in D.items():
    mp, fp = os.path.join(EXP, n + ".manifest.json"), os.path.join(EXP, n + ".flags.json")
    check("%s: manifest golden exists" % n, os.path.isfile(mp))
    check("%s: flags golden exists" % n, os.path.isfile(fp))
    if os.path.isfile(mp):
        check("%s: manifest equals its golden" % n, d["manifest"] == fx_mod.load(mp))
    if os.path.isfile(fp):
        check("%s: flags equal their golden" % n, d["flags"] == fx_mod.load(fp))

print("flat_t2i")
d = D["image_flat_t2i"]; m = d["manifest"]
check("fields, in order; an empty negative prompt is not offered", fids(d) == ["prompt", "width", "height", "steps", "cfg", "seed"], fids(d))
check("prompt binds the CLIPTextEncode text", field(d, "prompt")["bind"] == [{"node": "4", "input": "text"}]
      and field(d, "prompt")["default"].startswith("A red lighthouse") and field(d, "prompt")["tier"] == "primary")
check("size, steps, cfg and seed carry the template's own values",
      (field(d, "width")["default"], field(d, "height")["default"], field(d, "steps")["default"], field(d, "cfg")["default"]) == (1024, 768, 20, 4.5)
      and field(d, "seed")["default"] == 123456789012 % 2147483647 and field(d, "seed")["range"] == [0, 2147483647])
check("generic ranges", field(d, "width")["range"] == [64, 4096] and field(d, "steps")["range"] == [1, 200] and field(d, "cfg")["range"] == [0, 30])
check("ids and identity", (m["id"], m["mode"], m["cap"], m["label"]) == ("image-flat-t2i", "flat_t2i", "image", "Flat: Text to Image"), m)
check("note is the first sentence of the description", m["note"] == "Makes a picture from words.", m.get("note"))
check("no room", "room" not in m)
check("licence: unknown and never shippable", m["licence"]["shippable"] is False and m["licence"]["name"] == "Unknown (from the ComfyUI template image_flat_t2i)"
      and "Test lane" in m["licence"]["attribution"] and "Flat Model" in m["licence"]["attribution"], m["licence"])
check("only the licence needs an answer", flags(d) == {"licence": "answer"}, flags(d))
check("roles: unet, clip, vae, in graph order", [r["role"] for r in m["requires"]] == ["unet", "clip", "vae"])
r = next(r for r in m["requires"] if r["role"] == "unet")
check("model rule: stem first in prefer, dtype after, sibling excluded",
      r["match"] == {"all": ["flatmodel"], "none": ["distilled"], "prefer": ["flatmodel_bf16", "bf16"]}, r["match"])
check("role words name the pool and the file", r["words"] == "the unet file flatmodel_bf16.safetensors", r["words"])
check("the graph is kept exactly as converted", d["graph"] == fx.api("image_flat_t2i"))
for r in m["requires"]:
    hits = _comfy.rule_matches(fx.pools[r["pool"]], r["match"])
    orig = d["graph"][r["node"]]["inputs"][r["input"]]
    best = sorted(hits, key=lambda x: (-_comfy.rank(x, r["match"], True), x))[0]
    check("rule for %s resolves to the template's own file" % r["role"], best == orig, (best, orig))
check("nothing is bound twice", len({(b["node"], b["input"]) for f in m["fields"] for b in f["bind"]}
                                   | {(r["node"], r["input"]) for r in m["requires"]})
      == sum(len(f["bind"]) for f in m["fields"]) + len(m["requires"]))

print("sub_edit: subgraph ids, TextGenerate, ResolutionSelector, proxyWidgets")
d = D["image_sub_edit"]; m = d["manifest"]
check("fields", fids(d) == ["prompt", "picture", "aspect", "megapixels", "steps", "cfg", "seed", "denoise", "sampler_name"], fids(d))
check("prompt resolves through TextGenerate and StringConcatenate to the user's text, not the system string",
      field(d, "prompt")["bind"] == [{"node": "20", "input": "value"}] and field(d, "prompt")["default"].startswith("Make the sky"))
check("one picture: one field, required, labelled by the slot", field(d, "picture")["type"] == "image" and field(d, "picture")["required"] is True)
check("size from the ResolutionSelector: aspect + megapixels", field(d, "aspect")["options"] == ["1:1 (Square)", "16:9 (Landscape)", "9:16 (Portrait)"]
      and field(d, "megapixels")["range"] == [0.1, 4.0])
check("the sampler seed and the text generator's seed are one field", sorted((b["node"], b["input"]) for b in field(d, "seed")["bind"]) == [("30:10", "seed"), ("30:5", "seed")])
check("author-promoted widgets become advanced fields; a model-file combo is skipped",
      field(d, "denoise")["tier"] == "advanced" and field(d, "denoise")["group"] == "Template options" and field(d, "denoise")["range"] == [0.0, 1.0]
      and field(d, "sampler_name")["type"] == "select" and "unet_name" not in fids(d))
check("flags", flags(d) == {"licence": "answer", "promoted_unreviewed": "ack"}, flags(d))
check("a preview nothing consumes is dropped; colon ids survive", "24" not in d["graph"] and "30:5" in d["graph"])

print("multi_ref_switch: three pictures, a switch, math-derived length")
d = D["video_multi_ref_switch"]; m = d["manifest"]
check("fields", fids(d) == ["prompt", "picture1", "picture2", "picture3", "width", "height", "length", "cfg", "seed", "opt_enable_light"], fids(d))
check("slot labels name the node title or say untitled", [field(d, "picture%d" % i)["label"] for i in (1, 2, 3)]
      == ["Picture 1 (Subject)", "Picture 2 (untitled)", "Picture 3 (untitled)"])
check("hints carry the node and the prompt's own picture mentions",
      all("<Picture 1>" in field(d, "picture%d" % i)["hint"] and "image 2" in field(d, "picture%d" % i)["hint"] for i in (1, 2, 3))
      and "node 2" in field(d, "picture2")["hint"], field(d, "picture2")["hint"])
check("every slot is required", all(field(d, "picture%d" % i)["required"] and field(d, "picture%d" % i)["type"] == "image" for i in (1, 2, 3)))
check("length reached through math keeps the template's value, units 'value'",
      field(d, "length")["units"] == "value" and field(d, "length")["default"] == 5.0 and field(d, "length")["bind"] == [{"node": "22", "input": "value"}])
check("the switch is a checkbox", field(d, "opt_enable_light")["type"] == "checkbox" and field(d, "opt_enable_light")["default"] is False)
check("flags", flags(d) == {"picture_roles": "answer", "length_unit": "answer", "switch:opt_enable_light": "answer",
                            "licence": "answer", "steps_hidden": "ack"}, flags(d))
check("steps behind a switch are not offered", "steps" not in fids(d))
check("the LoRA is a plain role in this slice (optional roles come in S3)",
      [r["role"] for r in m["requires"]] == ["unet", "clip", "vae", "lora"] and all("optional" not in r for r in m["requires"]))

print("unslotted loaders block")
for n, node_input, f in (("utility_unslotted_da3", "2.model_name", "depth_anything_3_large.safetensors"),
                         ("3d_cap3d_moge", "2.model_name", "moge_vitl.safetensors")):
    d = D[n]
    fl = [x for x in d["flags"] if x["kind"] == "block"]
    check("%s: one block flag naming the file and class" % n, len(fl) == 1 and fl[0]["id"] == "unslotted_loader:" + node_input
          and f in fl[0]["text"] and "can't be added yet" in fl[0]["text"], fl)
    check("%s: the draft is still written, with no model role" % n, d["manifest"]["requires"] == [])
d = D["utility_unslotted_da3"]
check("da3: a picture, no prompt (acked as correct)", fids(d) == ["picture"] and flags(d)["no_prompt"] == "ack")
d = D["3d_cap3d_moge"]
check("moge: cap 3d, and id/mode derived from the name", (d["manifest"]["cap"], d["manifest"]["id"], d["manifest"]["mode"]) == ("3d", "3d-cap3d-moge", "cap3d_moge"))

print("preview swapped for a save; two output kinds")
d = D["image_preview_only"]
sv = [n for n in d["graph"].values() if n["class_type"] == "SaveImage"]
check("the preview becomes a SaveImage with filename_prefix bwf/<pack id>", len(sv) == 1 and sv[0]["inputs"]["filename_prefix"] == "bwf/image-preview-only"
      and not any(n["class_type"] == "PreviewImage" for n in d["graph"].values()), d["graph"])
check("and says so", flags(d).get("preview_swapped") == "ack")
two = copy.deepcopy(fx.api("image_flat_t2i")); two["50"] = {"class_type": "CreateVideo", "inputs": {"images": ["8", 0], "fps": 24}}
two["51"] = {"class_type": "SaveVideo", "inputs": {"video": ["50", 0], "filename_prefix": "v", "format": "auto", "codec": "auto"}}
dd = ap.synthesize("image_flat_t2i", fx.ui("image_flat_t2i"), two, fx.oi, fx.meta("image_flat_t2i"), fx.pools, ap.taken_for(None), lane_name="L")
check("two output kinds: video wins, and the pair is named", dd["manifest"]["cap"] == "video" and flags(dd).get("two_output_kinds") == "ack"
      and "image" in next(f["text"] for f in dd["flags"] if f["id"] == "two_output_kinds") and "video" in next(f["text"] for f in dd["flags"] if f["id"] == "two_output_kinds"))

print("substitution")
d = D["image_subst_dit"]
sub = [f for f in d["flags"] if f["id"].startswith("model_substitution")]
check("a different file than the template's is acked, naming both", len(sub) == 1 and sub[0]["id"] == "model_substitution:unet" and sub[0]["kind"] == "ack"
      and "zq_dit_fp16.safetensors" in sub[0]["text"] and "zq_dit_int8_convrot.safetensors" in sub[0]["text"], sub)
check("a folder-prefix-only difference is not flagged", not [f for f in D["image_flat_t2i"]["flags"] if f["id"].startswith("model_substitution")])
pools2 = {p: list(v) for p, v in fx.pools.items()}
pools2["unet"] = ["sub/dir/flatmodel_bf16.safetensors"]
dd = ap.synthesize("image_flat_t2i", fx.ui("image_flat_t2i"), fx.api("image_flat_t2i"), fx.oi, fx.meta("image_flat_t2i"), pools2, ap.taken_for(None), lane_name="L")
check("same basename under another folder: no flag", not [f for f in dd["flags"] if f["id"].startswith("model_substitution")])

print("refusals: nothing is written, the sentence is plain")
PAID = "This template runs on a paid online service, so it cannot become a mode here."
for label, kw in (("billed node in a non-api_ template", dict(name="image_billed")),
                  ("api_ prefix", dict(name="api_paid_cloud", api="image_flat_t2i")),
                  ("openSource false", dict(name="image_flat_t2i", meta=dict(fx.meta("image_flat_t2i"), local=False)))):
    try:
        ap.synthesize(kw["name"], fx.ui(kw.get("api", kw["name"])), fx.api(kw.get("api", kw["name"])), fx.oi, kw.get("meta") or fx.meta(kw.get("api", kw["name"])),
                      fx.pools, ap.taken_for(None), lane_name="L")
        check("refused: " + label, False, "no Refused raised")
    except ap.Refused as e:
        check("refused: " + label, e.sentence == PAID, e.sentence)
only_prev = {"1": {"class_type": "PreviewAny", "inputs": {"source": ["2", 0]}}, "2": {"class_type": "CustomCombo", "inputs": {"choice": "a"}}}
try:
    ap.synthesize("utility_x", {"nodes": []}, only_prev, fx.oi, {"name": "utility_x", "title": "X", "local": True}, fx.pools, ap.taken_for(None), lane_name="L")
    check("refused: nothing to keep", False, "no Refused raised")
except ap.Refused as e:
    check("refused: nothing to keep", e.sentence == "This template makes no file BWF could keep (it only previews).", e.sentence)

print("ids and modes are unique against what is taken")
taken = ap.taken_for(None)
taken["ids"].add("image-flat-t2i"); taken["modes"].add(("image", "flat_t2i"))
dd = fx.synth("image_flat_t2i", taken=taken)
check("a taken id and mode get -2", (dd["manifest"]["id"], dd["manifest"]["mode"]) == ("image-flat-t2i-2", "flat_t2i_2"), dd["manifest"]["id"])
taken = ap.taken_for(None)
taken["abilities"].add("flat_t2i")
check("an existing ability name counts as taken", fx.synth("image_flat_t2i", taken=taken)["manifest"]["mode"] == "flat_t2i_2")
check("a name that starts with a digit gets a letter prefix, ids obey the loader's regex",
      all(up._ID_RE.match(x["manifest"]["id"]) and "__" not in x["manifest"]["id"] and up._CAP_RE.match(x["manifest"]["mode"]) for x in D.values()))

print("every synthesized pack is something BWF's own loader accepts")
for n, d in D.items():
    work = tempfile.mkdtemp(prefix="bwf-aps-pack-")
    pid = d["manifest"]["id"]
    os.makedirs(os.path.join(work, pid))
    json.dump(d["manifest"], open(os.path.join(work, pid, "manifest.json"), "w"))
    json.dump(d["graph"], open(os.path.join(work, pid, "graph.api.json"), "w"))
    try:
        eng = up._load_one(pid, os.path.realpath(os.path.join(work, pid)),
                           {"ids": set(), "modes": set(), "abilities": set(), "caps": set(), "roles": set()})
        check("%s loads" % n, eng["user_pack"] is True)
    except up.PackError as e:
        check("%s loads" % n, n == "3d_cap3d_moge" and "got '3d'" in str(e), str(e))

print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    for n in FAILED: print("  -", n)
    sys.exit(1)
print("all passed")
