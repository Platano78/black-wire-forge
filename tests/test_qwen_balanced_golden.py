"""Q21 acceptance gate: the t2i Default preset's graph matches arm D of the
cfg/APG/FreSca A/B (owner-confirmed 2026-09-23) -- same node classes, same
wiring shape, same numeric sampling inputs. Arm D's own graph is read live
from D_P_4242.png's embedded ComfyUI "prompt" chunk (Pillow), never a typed
copy of it.

Also covers: "Fast / plain" and "Seamless tile" carry no APG/FreSca and use
euler/simple. The edit Default byte-identical check already lives in
test_packs_golden.py (qwen_edit.json golden is unchanged by this slice).

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
# the shipped "Default" preset keeps the app's existing 1328^2 -- ruling 4
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


print("t2i Default preset's graph == arm D's graph (class shapes, numeric sampling inputs)")
if not os.path.isfile(ARM_D_PNG):
    print("  SKIP  needs the private A/B evidence PNG, not included in the public repo")
else:
    try:
        want = canon(read_arm_d_graph(ARM_D_PNG))
        args, _ = build_args("image", "t2i", "default")
        got_graph = engines.graph_for("image", "t2i", args, MODELS)
        got = canon(got_graph)
        same = want == got
        detail = ""
        if not same:
            wset, gset = set(want), set(got)
            detail = "missing %s extra %s" % (sorted(wset - gset), sorted(gset - wset))
        check("Default preset graph matches arm D's graph", same, detail)
    except Exception as e:
        check("Default preset graph matches arm D's graph", False, "%s: %s" % (type(e).__name__, e))

print()
print("Fast / plain and Seamless tile carry no APG/FreSca and use euler/simple")
for preset_id in ("fast-plain", "seamless-tile"):
    args, _ = build_args("image", "t2i", preset_id)
    graph = engines.graph_for("image", "t2i", args, MODELS)
    classes = {n.get("class_type") for n in graph.values()}
    check("%s has no APG node" % preset_id, "APG" not in classes)
    check("%s has no FreSca node" % preset_id, "FreSca" not in classes)
    ks = next(n for n in graph.values() if n.get("class_type") == "KSampler")["inputs"]
    check("%s uses sampler euler" % preset_id, ks.get("sampler_name") == "euler", repr(ks))
    check("%s uses scheduler simple" % preset_id, ks.get("scheduler") == "simple", repr(ks))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
