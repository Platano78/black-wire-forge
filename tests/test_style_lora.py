"""LORA-1 Build A acceptance gate: the Style picker's graph wiring.

No style chosen -> the t2i/edit/pixelart graphs are byte-identical to the
pre-slice golden (already pinned by tests/test_packs_golden.py and
tests/test_pixelart.py -- this file adds the "a pack is chosen" half those
never covered): a LoraLoaderModelOnly node lands between the UNet loader
("1") and whatever reads the model next, carrying the chosen strength, and
a second style stacks on top of the first.

Run: python3 tests/test_style_lora.py
"""
import json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
G = os.path.join(HERE, "golden")

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines

MODELS = json.load(open(os.path.join(G, "models.json")))
ARGS = json.load(open(os.path.join(G, "args.json")))


def by_class(graph, cls):
    hits = [n for n in graph.values() if n.get("class_type") == cls]
    return hits


print("t2i (guidance_style Balanced by default): one style chains onto the model wire")
args = dict(ARGS["img"], style_1="cool_style_qwen_image.safetensors", style_1_strength=0.7)
g = engines.graph_for("image", "t2i", args, MODELS)
loras = by_class(g, "LoraLoaderModelOnly")
check("exactly one LoraLoaderModelOnly node", len(loras) == 1, loras)
if loras:
    lora = loras[0]
    check("its model input reads node 1 (the UNet loader)", lora["inputs"]["model"] == ["1", 0], lora)
    check("its lora_name is the chosen style", lora["inputs"]["lora_name"] == "cool_style_qwen_image.safetensors")
    check("its strength_model is the chosen strength", lora["inputs"]["strength_model"] == 0.7)
apg = g.get("11")
check("APG reads the LoRA chain's output, not node 1 directly",
      apg is not None and apg["inputs"]["model"] == ["30", 0], apg)

print()
print("t2i: two styles stack, in order")
args2 = dict(ARGS["img"], style_1="a_qwen_image.safetensors", style_1_strength=1.0,
             style_2="b_qwen_image.safetensors", style_2_strength=0.5)
g2 = engines.graph_for("image", "t2i", args2, MODELS)
loras2 = by_class(g2, "LoraLoaderModelOnly")
check("two LoraLoaderModelOnly nodes", len(loras2) == 2, loras2)
n30, n31 = g2.get("30"), g2.get("31")
check("node 30 (style 1) reads node 1", n30 is not None and n30["inputs"]["model"] == ["1", 0], n30)
check("node 31 (style 2) reads node 30's output, not node 1", n31 is not None and n31["inputs"]["model"] == ["30", 0], n31)
apg2 = g2.get("11")
check("APG reads the second style's output", apg2 is not None and apg2["inputs"]["model"] == ["31", 0], apg2)

print()
print("edit (guidance_style Plain by default): the style feeds ModelSamplingAuraFlow directly")
args3 = dict(ARGS["img"], style_1="edit_style_qwen_image.safetensors")
g3 = engines.graph_for("image", "edit", args3, MODELS)
loras3 = by_class(g3, "LoraLoaderModelOnly")
check("exactly one LoraLoaderModelOnly node", len(loras3) == 1, loras3)
msaf = g3.get("7")
check("ModelSamplingAuraFlow reads the style chain's output",
      msaf is not None and msaf["class_type"] == "ModelSamplingAuraFlow"
      and msaf["inputs"]["model"] == ["30", 0], msaf)

print()
print("no style chosen: t2i/edit stay byte-identical to golden (also pinned by test_packs_golden.py)")
plain_t2i = engines.graph_for("image", "t2i", dict(ARGS["img"]), MODELS)
want_t2i = json.load(open(os.path.join(G, "qwen_t2i.json")))
check("t2i graph unchanged with style_1/style_2 absent",
      json.loads(json.dumps(plain_t2i, sort_keys=True)) == json.loads(json.dumps(want_t2i, sort_keys=True)))
plain_edit = engines.graph_for("image", "edit", dict(ARGS["img"]), MODELS)
want_edit = json.load(open(os.path.join(G, "qwen_edit.json")))
check("edit graph unchanged with style_1/style_2 absent",
      json.loads(json.dumps(plain_edit, sort_keys=True)) == json.loads(json.dumps(want_edit, sort_keys=True)))
check("no style, explicit empty string, also stays unchanged (server sends '' for None)",
      json.loads(json.dumps(engines.graph_for("image", "t2i", dict(ARGS["img"], style_1="", style_2=""), MODELS),
                            sort_keys=True))
      == json.loads(json.dumps(want_t2i, sort_keys=True)))

print()
print("field declarations: qwen t2i/edit and pixelart all offer the Style group")
for cap, mode in (("image", "t2i"), ("image", "edit"), ("image", "pixelart")):
    fields = engines.fields(cap, mode)
    style_ids = {f["id"] for f in fields if f.get("group") == "Style"}
    check("%s/%s declares style_1/style_1_strength/style_2/style_2_strength" % (cap, mode),
          style_ids == {"style_1", "style_1_strength", "style_2", "style_2_strength"}, style_ids)
    s1 = next(f for f in fields if f["id"] == "style_1")
    check("%s/%s style_1 is a pool_select over the 'lora' pool" % (cap, mode),
          s1["type"] == "pool_select" and s1.get("pool") == "lora", s1)
    check("%s/%s style_1's match rule is family-agnostic (qwen_image/qwen-image only)" % (cap, mode),
          s1.get("match", {}).get("any") == ["qwen_image", "qwen-image"], s1.get("match"))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
