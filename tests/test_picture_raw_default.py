"""Gate for the owner's 2026-10-03 ruling: the Picture room's t2i default is
RAW -- plain guidance, cfg 1, 40 steps, no APG/FreSca -- and the old Balanced
recipe survives as the opt-in `balanced` preset.

Pins the declared field defaults, the preset ids and values, the "Things to
avoid" caveat, and BOTH graphs: the raw default (no APG/FreSca, euler/simple,
cfg 1.0, 40 steps) and the Balanced recipe (APG + FreSca, seeds_2).

Run: python3 tests/test_picture_raw_default.py
"""
import json
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import engines  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


MODELS = json.load(open(os.path.join(HERE, "golden", "models.json")))
FIELDS = {f["id"]: f for f in engines.fields("image", "t2i")}
PRESETS = {p["id"]: p for p in engines.presets("image", "t2i")}

RAW = {"steps": 40, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "guidance_style": "Plain"}
OLD_DEFAULT = {"steps": 20, "cfg": 3.0, "sampler": "seeds_2", "scheduler": "sgm_uniform",
               "guidance_style": "Balanced"}


def build(values):
    """The mode's declared defaults plus the given preset's values -- what the
    page and the API actually send for that preset."""
    args = {f["id"]: f["default"] for f in engines.fields("image", "t2i") if "default" in f}
    args.update(values)
    args.update({"prompt": "P", "negative": "", "seed": 4242})
    return engines.graph_for("image", "t2i", args, MODELS)


def ksampler(graph):
    return next(n for n in graph.values() if n.get("class_type") == "KSampler")["inputs"]


print("t2i field defaults are exactly the raw recipe")
for key, want in RAW.items():
    check("%s defaults to %r" % (key, want), FIELDS.get(key, {}).get("default") == want,
          FIELDS.get(key, {}).get("default"))

print()
print("the 'Things to avoid' hint says it does nothing at the default")
neg_hint = (FIELDS.get("negative") or {}).get("hint") or ""
check("negative hint says it has no effect at Guidance strength 1",
      "no effect" in neg_hint and "1" in neg_hint, neg_hint)
check("negative hint names the way out (raise it, or the Balanced recipe)",
      "2.5" in neg_hint and "Balanced" in neg_hint, neg_hint)

print()
print("presets: exactly the seven ids, in order")
check("preset ids", [p["id"] for p in engines.presets("image", "t2i")]
      == ["default", "balanced", "fast-plain", "sharp-text", "seamless-tile",
          "set-plate", "character-anchor"], sorted(PRESETS))

print()
print("the default preset IS the raw recipe; balanced IS the old default")
dv = PRESETS["default"]["values"]
check("default values", {k: dv.get(k) for k in RAW} == RAW, dv)
check("default is 1328x1328", (dv.get("width"), dv.get("height")) == (1328, 1328), dv)
check("default is labelled 'Default (raw)'", PRESETS["default"]["label"] == "Default (raw)",
      PRESETS["default"]["label"])
bv = PRESETS["balanced"]["values"]
check("balanced values are the old default", {k: bv.get(k) for k in OLD_DEFAULT} == OLD_DEFAULT, bv)
check("balanced is 1328x1328", (bv.get("width"), bv.get("height")) == (1328, 1328), bv)

print()
print("the raw default graph: no APG, no FreSca, euler/simple, cfg 1.0, 40 steps")
raw_graph = build(PRESETS["default"]["values"])
classes = {n.get("class_type") for n in raw_graph.values()}
check("no APG node", "APG" not in classes, sorted(classes))
check("no FreSca node", "FreSca" not in classes, sorted(classes))
ks = ksampler(raw_graph)
check("sampler_name euler", ks.get("sampler_name") == "euler", repr(ks))
check("scheduler simple", ks.get("scheduler") == "simple", repr(ks))
check("cfg 1.0", ks.get("cfg") == 1.0, repr(ks))
check("steps 40", ks.get("steps") == 40, repr(ks))

print()
print("the balanced recipe graph still has APG + FreSca on a seeds_2 sampler")
bal_graph = build(PRESETS["balanced"]["values"])
bclasses = {n.get("class_type") for n in bal_graph.values()}
check("has an APG node", "APG" in bclasses, sorted(bclasses))
check("has a FreSca node", "FreSca" in bclasses, sorted(bclasses))
check("FreSca reads APG's output", (bal_graph.get("12") or {}).get("inputs", {}).get("model") == ["11", 0],
      bal_graph.get("12"))
bks = ksampler(bal_graph)
check("sampler_name seeds_2", bks.get("sampler_name") == "seeds_2", repr(bks))
check("scheduler sgm_uniform", bks.get("scheduler") == "sgm_uniform", repr(bks))
check("cfg 3.0", bks.get("cfg") == 3.0, repr(bks))
check("steps 20", bks.get("steps") == 20, repr(bks))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)