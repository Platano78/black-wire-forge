"""Q21 acceptance gate: the Balanced RECIPE's graph matches arm D of the
cfg/APG/FreSca A/B (owner-confirmed 2026-09-23) -- same node classes, same
wiring shape, same numeric sampling inputs. Arm D's own graph is read live
from D_P_4242.png's embedded ComfyUI "prompt" chunk (Pillow), never a typed
copy of it.

Since the owner's 2026-10-03 ruling arm D is no longer the t2i DEFAULT: the
default is now raw (Plain, cfg 1, 40 steps) and arm D survives as the opt-in
`balanced` preset. So this file compares arm D against the `balanced` preset,
pins the Balanced graph byte-for-byte against its own golden
(tests/golden/qwen_t2i_balanced.json, generated from the preset's values), and
pins the raw Default separately (no APG/FreSca, euler/simple, cfg 1.0, 40
steps). tests/golden/qwen_t2i.json is unchanged -- it is built from the older
ARGS fixture, which sends no guidance_style, so the graph builder's fallback
still resolves it to Balanced there (tests/test_packs_golden.py and
tests/test_style_lora.py read that file). The edit Default byte-identical check
lives in test_packs_golden.py (qwen_edit.json golden is unchanged by this
slice).

Run: python3 tests/test_qwen_balanced_golden.py
"""
import json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines

# The evidence lives outside this repo, in a private research archive.
ARM_D_PNG = "/nonexistent/D_P_4242.png"  # private A/B evidence, not included in this repo

MODELS = json.load(open(os.path.join(HERE, "golden", "models.json")))

# Volatile/per-render keys stripped before comparison: loader filenames,
# prompt text, seed, filename prefix. Width/height/resolution are ALSO
# stripped here -- a JUDGMENT CALL (LOW-CONFIDENCE, flagged in the report):
# arm D was rendered small (1024^2) purely for A/B render-time economy, while
# the shipped presets keep the app's existing 1328^2 -- ruling 4
# never says to change that, and the graph property being tested is the
# GUIDANCE MECHANISM (APG/FreSca/KSampler wiring and numbers), not output
# size.
STRIP_INPUT_KEYS = {"unet_name", "clip_name", "vae_name", "prompt", "negative_prompt",
                     "seed", "filename_prefix", "width", "height"}
STRIP_NODE_CLASSES_INPUTS = {"EmptyLatentImage": {"width", "height"},
                             "TextEncodeQwenImage21": {"resolution"}}


def read_arm_d_graph(png_path):
    from PIL import Image
    im = Image.open(png_path)
    raw = im.info.get("prompt")
    if not raw:
        raise ValueError("%s carries no embedded ComfyUI prompt chunk" % png_path)
    return json.loads(raw)


def canon(graph):
    """A graph as a class-topology multiset: for each node, its class_type
    plus its inputs with volatile keys stripped and any [node_id, slot] ref
    replaced by (that node's class_type, slot) -- so node-ID numbering
    (ours vs. ComfyUI's own) never matters, only shape."""
    classes = {nid: n.get("class_type") for nid, n in graph.items()}
    out = []
    for nid, n in sorted(graph.items()):
        cls = n.get("class_type")
        strip = STRIP_INPUT_KEYS | STRIP_NODE_CLASSES_INPUTS.get(cls, set())
        items = []
        for k, v in sorted(n.get("inputs", {}).items()):
            if k in strip:
                continue
            if k.startswith("images.image_"):
                continue  # edit-only reference wiring, irrelevant to t2i
            if isinstance(v, list) and len(v) == 2 and v[0] in classes:
                v = ("REF", classes[v[0]], v[1])
            else:
                v = json.dumps(v, sort_keys=True)
            items.append((k, v))
        out.append((cls, tuple(sorted(items))))
    return sorted(out)


def build_args(cap, mode, preset_id, prompt="P", negative="N", seed=4242):
    fields = engines.fields(cap, mode)
    args = {f["id"]: f["default"] for f in fields if "default" in f}
    preset = next(p for p in engines.presets(cap, mode) if p["id"] == preset_id)
    args.update(preset["values"])
    args["prompt"] = prompt
    args.setdefault("negative", negative)
    args["seed"] = seed
    return args, fields


print("the Balanced recipe's graph == arm D's graph (class shapes, numeric sampling inputs)")
if not os.path.isfile(ARM_D_PNG):
    print("  SKIP  needs the private A/B evidence PNG, not included in the public repo")
else:
    try:
        want = canon(read_arm_d_graph(ARM_D_PNG))
        args, _ = build_args("image", "t2i", "balanced")
        got_graph = engines.graph_for("image", "t2i", args, MODELS)
        got = canon(got_graph)
        same = want == got
        detail = ""
        if not same:
            wset, gset = set(want), set(got)
            detail = "missing %s extra %s" % (sorted(wset - gset), sorted(gset - wset))
        check("Balanced preset graph matches arm D's graph", same, detail)
    except Exception as e:
        check("Balanced preset graph matches arm D's graph", False, "%s: %s" % (type(e).__name__, e))

print()
print("the Balanced recipe is still the pinned arm D golden, byte for byte")
G = os.path.join(HERE, "golden")
want_bal = json.load(open(os.path.join(G, "qwen_t2i_balanced.json")))
args_bal, _ = build_args("image", "t2i", "balanced", prompt="P", negative="N", seed=4242)
got_bal = engines.graph_for("image", "t2i", args_bal, MODELS)
got_bal = json.loads(json.dumps(got_bal, sort_keys=True))
want_bal = json.loads(json.dumps(want_bal, sort_keys=True))
check("balanced preset graph == golden/qwen_t2i_balanced.json", got_bal == want_bal,
      "node ids differ: missing %s extra %s" % (sorted(set(want_bal) - set(got_bal)),
                                                sorted(set(got_bal) - set(want_bal)))
      if set(want_bal) != set(got_bal)
      else "differs at " + str(sorted(k for k in want_bal if want_bal.get(k) != got_bal.get(k))))

print()
print("the raw Default preset carries no APG/FreSca and uses euler/simple, cfg 1.0, 40 steps")
args_raw, _ = build_args("image", "t2i", "default")
graph = engines.graph_for("image", "t2i", args_raw, MODELS)
classes = {n.get("class_type") for n in graph.values()}
check("default has no APG node", "APG" not in classes)
check("default has no FreSca node", "FreSca" not in classes)
ks = next(n for n in graph.values() if n.get("class_type") == "KSampler")["inputs"]
check("default uses sampler euler", ks.get("sampler_name") == "euler", repr(ks))
check("default uses scheduler simple", ks.get("scheduler") == "simple", repr(ks))
check("default uses cfg 1.0", ks.get("cfg") == 1.0, repr(ks))
check("default uses 40 steps", ks.get("steps") == 40, repr(ks))

print()
print("Fast / plain, Sharp text and Seamless tile carry no APG/FreSca and use euler/simple")
for preset_id in ("fast-plain", "sharp-text", "seamless-tile"):
    args, _ = build_args("image", "t2i", preset_id)
    graph = engines.graph_for("image", "t2i", args, MODELS)
    classes = {n.get("class_type") for n in graph.values()}
    check("%s has no APG node" % preset_id, "APG" not in classes)
    check("%s has no FreSca node" % preset_id, "FreSca" not in classes)
    ks = next(n for n in graph.values() if n.get("class_type") == "KSampler")["inputs"]
    check("%s uses sampler euler" % preset_id, ks.get("sampler_name") == "euler", repr(ks))
    check("%s uses scheduler simple" % preset_id, ks.get("scheduler") == "simple", repr(ks))

print()
print("Sharp text: cfg 1, 40 steps")
args, _ = build_args("image", "t2i", "sharp-text")
graph = engines.graph_for("image", "t2i", args, MODELS)
ks = next(n for n in graph.values() if n.get("class_type") == "KSampler")["inputs"]
check("sharp-text uses cfg 1.0", ks.get("cfg") == 1.0, repr(ks))
check("sharp-text uses 40 steps", ks.get("steps") == 40, repr(ks))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
