"""Acceptance gate for the engine extraction (atomic tasks 2-4).

The packs are correct iff they reproduce the graphs server.py built BEFORE the
extraction, byte for byte. Golden files were captured from the pre-extraction
code; nothing here is a judgement call.

Run: python3 tests/test_packs_golden.py
"""
import json, os, sys
sys.dont_write_bytecode = True  # a same-length source edit leaves file SIZE
# unchanged, so .pyc invalidation (mtime+size) can serve stale bytecode for code
# you just changed -- observed 2026-09-22 reporting a defect already reverted.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
G = os.path.join(HERE, "golden")

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import engines

models = json.load(open(os.path.join(G, "models.json")))
args = json.load(open(os.path.join(G, "args.json")))

CASES = [("qwen_t2i", "image", "t2i", "img"), ("qwen_edit", "image", "edit", "img"),
         ("h3_fl2va", "video", "fl2va", "vid"), ("h3_ref2va", "video", "ref2v", "vid")]

print("graphs reproduce the pre-extraction output exactly")
for golden, cap, mode, argset in CASES:
    want = json.load(open(os.path.join(G, golden + ".json")))
    try:
        got = engines.graph_for(cap, mode, dict(args[argset]), models)
    except Exception as e:
        check("%s via engines.graph_for" % golden, False, "%s: %s" % (type(e).__name__, e)); continue
    same = json.loads(json.dumps(got, sort_keys=True)) == json.loads(json.dumps(want, sort_keys=True))
    d = ""
    if not same:
        wk, gk = set(want), set(got)
        if wk != gk: d = "node ids differ: missing %s extra %s" % (sorted(wk - gk), sorted(gk - wk))
        else: d = "differs at " + str(sorted(k for k in wk if want[k] != got[k]))
    check("%s matches golden" % golden, same, d)

print("capabilities and words come from packs")
check("image ability true with qwen files", engines.abilities(models).get("image") is True)
check("video ability true with h3 files", engines.abilities(models).get("video") is True)
none = {}
check("image missing names the model", any("Qwen" in w or "picture" in w.lower() or "image" in w.lower()
                                           for w in engines.missing_words(none, "image")))
check("describe image is human", "Qwen" in engines.describe(models, "image"))
check("describe video is human", "H3" in engines.describe(models, "video") or "MiniMax" in engines.describe(models, "video"))

print("licences are declared by packs, not the core")
lic = {l["engine"]: l for l in engines.licences()}
check("every pack declares a licence", len(lic) >= 2, str(sorted(lic)))
check("each licence states shippability", all("shippable" in l for l in lic.values()))
check("each licence states attribution", all(l.get("attribution") for l in lic.values()))
check("ltx licence links to its licence text",
      lic.get("ltx", {}).get("url") == "https://github.com/Lightricks/LTX-2/blob/main/LICENSE-2_x")
check("h3 licence links to its licence text",
      lic.get("minimax-h3", {}).get("url") == "https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE")
check("h3 licence notes the geography restriction", "EU" in lic.get("minimax-h3", {}).get("note", ""))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
