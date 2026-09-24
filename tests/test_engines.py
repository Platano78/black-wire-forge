"""RED-first contract test for the engine pack layer (Principle 0).

The core must know no model names. These assertions are the acceptance criteria
for that: they describe what a pack IS and what the loader must do with it,
independent of Qwen or H3 existing at all.

Run: python3 tests/test_engines.py
"""
import os, sys, types
sys.dont_write_bytecode = True  # a same-length source edit leaves file SIZE
# unchanged, so .pyc invalidation (mtime+size) can serve stale bytecode for code
# you just changed -- observed 2026-09-22 reporting a defect already reverted.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = []
def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond: FAILED.append(name)

import engines

# -- a synthetic pack, so the contract is tested without any real engine -------
fake = types.ModuleType("engines._fake")
fake.ENGINE = {
    "id": "zzz-fake", "cap": "widget",
    "roles": {"w_unet": ("unet", {"all": ["widget"]})},
    "provides": {"widget": ["w_unet"]},
    "words": {"w_unet": "the widget model"},
    "graphs": {"make": lambda a, m: {"1": {"class_type": "W", "inputs": {"n": m["w_unet"]}}}},
    "describe": lambda m: "Widget " + engines.quant_words(m.get("w_unet", "")),
    "licence": {"name": "TEST", "shippable": False, "attribution": "Widget Co"},
}
sys.modules["engines._fake"] = fake

# A second synthetic pack, so caps()/cap_word()/list-licence flattening are
# tested independently of the fake widget pack above.
fake2 = types.ModuleType("engines._fake2")
fake2.ENGINE = {
    "id": "zzz-fake2", "cap": "gadget", "cap_word": "gadgetry", "cap_order": 7,
    "roles": {"g_unet": ("unet", {"all": ["gadget"]})},
    "provides": {"whir": ["g_unet"], "buzz": ["g_unet"]},
    "words": {"g_unet": "the gadget model"},
    "graphs": {"whir": lambda a, m: {"1": {"class_type": "G", "inputs": {}}},
              "buzz": lambda a, m: {"1": {"class_type": "G", "inputs": {}}}},
    "describe": lambda m: "Gadget",
    "licence": [
        {"name": "A", "shippable": True, "attribution": "A Co", "modes": ["whir"]},
        {"name": "B", "shippable": False, "attribution": "B Co", "modes": ["buzz"]},
    ],
}
sys.modules["engines._fake2"] = fake2

# D2's adversarial gate: a THIRD pack sharing "gadget" with fake2, sorting
# FIRST alphabetically ("aaa-" < "zzz-"), that declares neither cap_word nor
# cap_order. cap_word()/cap_order() must still answer from fake2 -- the pack
# that actually declares one -- not silently fall back to the cap/100
# default just because this pack was discovered first.
fake3 = types.ModuleType("engines._fake3")
fake3.ENGINE = {
    "id": "aaa-fake3", "cap": "gadget",
    "roles": {"g_unet2": ("unet", {"all": ["gadget2"]})},
    "provides": {"clank": ["g_unet2"]},
    "words": {"g_unet2": "the second gadget model"},
    "graphs": {"clank": lambda a, m: {"1": {"class_type": "G2", "inputs": {}}}},
    "describe": lambda m: "",
    "licence": {"name": "C", "shippable": True, "attribution": "C Co"},
}
sys.modules["engines._fake3"] = fake3

engines._PACKS = None                      # force a rescan that includes the fakes
engines.packs.__globals__["_PACKS"] = None
_real = engines.packs()
engines.packs.__globals__["_PACKS"] = sorted(
    _real + [fake.ENGINE, fake2.ENGINE, fake3.ENGINE], key=lambda e: e["id"])

print("engine pack contract")
check("packs() returns dicts with an id", all(p.get("id") for p in engines.packs()))
check("role_pool maps role -> pool", engines.role_pool().get("w_unet") == "unet")
check("role_rules maps role -> rule", engines.role_rules().get("w_unet") == {"all": ["widget"]})
check("model_keys includes pack roles", "w_unet" in engines.model_keys())

have = {"w_unet": "widget_v2_Q6_K.gguf"}
none = {}
check("ability true when every role resolved", engines.abilities(have).get("widget") is True)
check("ability false when a role is missing", engines.abilities(none).get("widget") is False)
check("cap is true when an ability is",      engines.abilities(have).get("widget") is True)

check("missing_words names the absent role", engines.missing_words(none, "widget") == ["the widget model"])
check("missing_words empty when satisfied",  engines.missing_words(have, "widget") == [])
check("missing_words empty for unknown ability", engines.missing_words(none, "nope") == [])

check("describe uses the pack", engines.describe(have, "widget") == "Widget GGUF")
check("describe empty for unknown cap", engines.describe(have, "nope") == "")

g = engines.graph_for("widget", "make", {}, have)
check("graph_for builds via the pack", g["1"]["inputs"]["n"] == "widget_v2_Q6_K.gguf")
try:
    engines.graph_for("widget", "absent", {}, have); check("graph_for raises LookupError", False)
except LookupError:
    check("graph_for raises LookupError", True)
check("modes_for lists pack modes", "make" in engines.modes_for("widget"))

lic = [l for l in engines.licences() if l.get("engine") == "zzz-fake"]
check("licences carry the engine id", len(lic) == 1)
check("licences carry shippability", lic and lic[0]["shippable"] is False)

print("caps and cap_word")
check("caps() includes the fake pack's cap", "widget" in engines.caps())
check("caps() includes a second pack's cap", "gadget" in engines.caps())
check("caps() is sorted", engines.caps() == sorted(engines.caps()))
check("cap_word defaults to the cap name", engines.cap_word("widget") == "widget")
check("cap_word uses a pack's declared cap_word", engines.cap_word("gadget") == "gadgetry")
check("cap_word for an unknown cap is the cap itself", engines.cap_word("nope") == "nope")
check("D2: a non-declaring pack sorting FIRST does not shadow cap_word",
      engines.cap_word("gadget") == "gadgetry")
check("D2: a non-declaring pack sorting FIRST does not shadow cap_order",
      engines.cap_order("gadget") == 7)
_articles = ("a ", "an ", "the ", "some ")
check("no pack's cap_word starts with an article or quantifier -- it must fit \"the ___ models\"",
      not any(engines.cap_word(c).lower().startswith(_articles) for c in engines.caps()))

print("missing_words()'s cap-level fallback answers among the DECLARING packs only")
# A temporary, isolated pack set (saved/restored) so it cannot disturb the
# cap_word/cap_order checks above or the rest of this file.
_saved_packs = engines.packs.__globals__["_PACKS"]

# Case 1: a non-declaring TOOL pack ("aaa-tool", sorts FIRST, one missing
# role) shares a cap with a DECLARING generator pack ("zzz-gen", also one
# missing role -- tied length, so a naive "first pack by id" or "globally
# shortest, any pack" scan would happily return the tool's words). The
# declaring pack must win regardless.
tool_pack = {
    "id": "aaa-tool", "cap": "mw-cap",
    "roles": {"tool_unet": ("unet", {"all": ["mwtool"]})},
    "provides": {"tool-make": ["tool_unet"]},
    "words": {"tool_unet": "the tool model"},
    "graphs": {"tool-make": lambda p, m: {}},
    "describe": lambda m: "",
    "licence": {"name": "T", "shippable": True, "attribution": "T"},
}
gen_pack = {
    "id": "zzz-gen", "cap": "mw-cap", "cap_word": "mwthing",
    "roles": {"gen_unet": ("unet", {"all": ["mwgen"]})},
    "provides": {"gen-make": ["gen_unet"]},
    "words": {"gen_unet": "the generator model"},
    "graphs": {"gen-make": lambda p, m: {}},
    "describe": lambda m: "",
    "licence": {"name": "G", "shippable": True, "attribution": "G"},
}
engines.packs.__globals__["_PACKS"] = sorted(_saved_packs + [tool_pack, gen_pack], key=lambda e: e["id"])
check("RED-shape proof: the tool pack DOES sort first and IS tied-shortest (the trap this guards against)",
      sorted(p["id"] for p in engines.packs() if p["cap"] == "mw-cap")[0] == "aaa-tool")
check("GREEN: cap-level missing for 'mw-cap' comes from the declaring pack, not the earlier-sorting tool",
      engines.missing_words({}, "mw-cap") == ["the generator model"])
engines.packs.__globals__["_PACKS"] = _saved_packs

# Case 2: two DECLARING packs share a cap; the one with the SHORTER missing
# list has the LATER id ("zzz-long" < nothing sorts after it, so pick an
# earlier-sorting pack with the LONGER list to prove length decides, not id).
long_pack = {
    "id": "aaa-long", "cap": "mw-cap2", "cap_word": "longthing",
    "roles": {"long_a": ("unet", {"all": ["mwlonga"]}), "long_b": ("clip", {"all": ["mwlongb"]})},
    "provides": {"long-make": ["long_a", "long_b"]},
    "words": {"long_a": "the long model A", "long_b": "the long model B"},
    "graphs": {"long-make": lambda p, m: {}},
    "describe": lambda m: "",
    "licence": {"name": "L", "shippable": True, "attribution": "L"},
}
short_pack = {
    "id": "zzz-short", "cap": "mw-cap2", "cap_word": "shortthing",
    "roles": {"short_a": ("unet", {"all": ["mwshorta"]})},
    "provides": {"short-make": ["short_a"]},
    "words": {"short_a": "the short model"},
    "graphs": {"short-make": lambda p, m: {}},
    "describe": lambda m: "",
    "licence": {"name": "S", "shippable": True, "attribution": "S"},
}
engines.packs.__globals__["_PACKS"] = sorted(_saved_packs + [long_pack, short_pack], key=lambda e: e["id"])
check("GREEN: the shorter declaring pack's missing list wins despite sorting AFTER the longer one",
      engines.missing_words({}, "mw-cap2") == ["the short model"])
engines.packs.__globals__["_PACKS"] = _saved_packs

print("licence as a list, and modes on a list entry")
lic2 = [l for l in engines.licences() if l.get("engine") == "zzz-fake2"]
check("a list licence yields one entry per regime", len(lic2) == 2)
check("each list entry carries the engine id", all(l["engine"] == "zzz-fake2" for l in lic2))
whir = next(l for l in lic2 if l["name"] == "A")
check("a list entry's modes are preserved", whir["modes"] == ["whir"])
check("a dict licence (unchanged pack) still works",
      any(l["engine"] == "zzz-fake" and l["name"] == "TEST" for l in engines.licences()))

print("shared helpers")
check("unet_loader picks GGUF node", engines.unet_loader("a.gguf")["class_type"] == "UnetLoaderGGUF")
check("GGUF loader omits weight_dtype", "weight_dtype" not in engines.unet_loader("a.gguf")["inputs"])
check("unet_loader picks UNETLoader", engines.unet_loader("a.safetensors")["class_type"] == "UNETLoader")
check("safetensors loader sets weight_dtype", engines.unet_loader("a.safetensors")["inputs"]["weight_dtype"] == "default")
check("quant_words finds a marker", engines.quant_words("m_Q6_K.gguf") == "GGUF")
check("quant_words empty when none", engines.quant_words("plain.safetensors") == "")

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
