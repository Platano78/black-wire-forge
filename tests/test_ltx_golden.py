"""Acceptance gate for the LTX-2.5 engine pack (video port, atomic task ltx-1).

The pack is correct iff it reproduces the FROZEN graph_builders.py output
byte-for-byte, modulo exactly one declared deviation per case: the SaveVideo
filename_prefix (video/... in the frozen source, blackwire/... in the pack,
matching the audio pack's own R7 convention). Golden files were captured
straight from the frozen source by tests/capture_ltx_golden.py -- never from
the pack under test.

"talking" is not itself a frozen builder (it is this pack's own composition of
_ltx_graph + talking_head_prompt, ported per R3 as a single piece, not the
frozen script-chaining machinery), so it has no golden case here -- it is
proven correct by construction (same _ltx_graph as "ltx") and exercised live
in gate 3.

Run: python3 tests/test_ltx_golden.py
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

m = json.load(open(os.path.join(G, "ltx_models.json")))
args = json.load(open(os.path.join(G, "ltx_args.json")))

# mode -> golden file -> arg key -> the ONLY node/field path allowed to differ
# from the frozen output. Each entry is (node_id, input_field,
# expected_frozen_value, expected_pack_value).
CASES = {
    ("video", "ltx"):      ("ltx_t2v",   "t2v",   "75", "video/LTX_2.5_t2v",   "blackwire/LTX"),
    ("video", "ltx_loop"): ("ltx_loop",  "loop",  "75", "video/LTX25_looping", "blackwire/LTX_LOOP"),
}
# The i2v/flf2v branches of the SAME "ltx" mode are checked separately below
# (same allowlist, different arg set) -- graph_for's mode is still "ltx".
EXTRA_LTX_CASES = [
    ("ltx_i2v", "i2v"),
    ("ltx_flf2v", "flf2v"),
]


def diff_paths(want, got, prefix=()):
    """Every (path, want_value, got_value) where the two graphs differ,
    walking both node dicts. path is a tuple like (node_id, 'inputs', field)."""
    out = []
    keys = set(want) | set(got)
    for k in keys:
        p = prefix + (k,)
        if k not in want or k not in got:
            out.append((p, want.get(k), got.get(k)))
        elif isinstance(want[k], dict) and isinstance(got[k], dict):
            out.extend(diff_paths(want[k], got[k], p))
        elif want[k] != got[k]:
            out.append((p, want[k], got[k]))
    return out


def run_case(label, golden_name, cap, mode, argset, node, frozen_val, pack_val):
    want = json.load(open(os.path.join(G, golden_name + ".json")))
    got = engines.graph_for(cap, mode, dict(args[argset]), m)
    diffs = diff_paths(want, got)
    allowlist_path = (node, "inputs", "filename_prefix")
    unexpected = [d for d in diffs if d[0] != allowlist_path]
    check("%s: no undeclared differences" % label, not unexpected, str(unexpected[:3]))
    allowed = [d for d in diffs if d[0] == allowlist_path]
    check("%s: the ONLY difference is filename_prefix" % label,
          allowed == [(allowlist_path, frozen_val, pack_val)], str(allowed))


print("ltx graphs reproduce the frozen source exactly, modulo filename_prefix")
for (cap, mode), (golden_name, argset, node, frozen_val, pack_val) in CASES.items():
    run_case("%s/%s" % (cap, mode), golden_name, cap, mode, argset, node, frozen_val, pack_val)

for golden_name, argset in EXTRA_LTX_CASES:
    run_case("video/ltx (%s)" % argset, golden_name, "video", "ltx", argset,
              "75", "video/LTX_2.5_t2v", "blackwire/LTX")

print()
print("talking mode builds without error and carries the composed prompt")
talking_graph = engines.graph_for("video", "talking", dict(args["talking"]), m)
prompt_node = talking_graph.get("364", {}).get("inputs", {}).get("text", "")
check("talking: prompt names the spoken line",
      '"Welcome to the forge' in prompt_node, prompt_node[:80])
check("talking: prompt asks for lip sync",
      "sync with the words" in prompt_node)
check("talking: start_image is the face picture",
      talking_graph.get("300", {}).get("inputs", {}).get("image") == "test_face.png")
check("talking: audio branch present (LTXVAudioVAEDecode)",
      any(n.get("class_type") == "LTXVAudioVAEDecode" for n in talking_graph.values()))
check("talking: filename_prefix is blackwire/TALKING",
      talking_graph["75"]["inputs"]["filename_prefix"] == "blackwire/TALKING")

print()
print("G2 falsifiability proof: perturb one pack value, confirm RED, then GREEN again")
import engines.ltx as ltx_mod
_orig = ltx_mod.ltx_loop_graph
def _broken(p, mm):
    g = _orig(p, mm)
    g["500"]["inputs"]["adain_factor"] = 999.0   # perturb a value the golden pins
    return g
ltx_mod.ltx_loop_graph = _broken
ltx_mod.ENGINE["graphs"]["ltx_loop"] = _broken
engines._PACKS = None  # force rescan so graph_for sees the perturbed function
want = json.load(open(os.path.join(G, "ltx_loop.json")))
got = engines.graph_for("video", "ltx_loop", dict(args["loop"]), m)
red = diff_paths(want, got)
red_unexpected = [d for d in red if d[0] != ("75", "inputs", "filename_prefix")]
print("  RED (perturbed):  %d unexpected diff(s): %s" % (len(red_unexpected), red_unexpected[:2]))
check("perturbation is caught (RED)", len(red_unexpected) > 0)

ltx_mod.ltx_loop_graph = _orig
ltx_mod.ENGINE["graphs"]["ltx_loop"] = _orig
engines._PACKS = None
got2 = engines.graph_for("video", "ltx_loop", dict(args["loop"]), m)
green = diff_paths(want, got2)
green_unexpected = [d for d in green if d[0] != ("75", "inputs", "filename_prefix")]
print("  GREEN (reverted): %d unexpected diff(s): %s" % (len(green_unexpected), green_unexpected[:2]))
check("revert is clean again (GREEN)", len(green_unexpected) == 0)

print()
print("B2: an impossible window/overlap combination refuses with a plain sentence, not the raw-kwarg message")

# RED: the frozen-equivalent body's OWN check still exists (untouched, R2
# verbatim) and still names raw Python kwargs -- that is what a person saw
# before B2's wrapper. Calling it directly (not through the public entry
# point) reproduces exactly that, without editing the frozen-equivalent body.
try:
    ltx_mod._ltx_graph({"prompt": "t", "length": 97, "context_length": 9,
                        "context_overlap": 40}, m, "blackwire/LTX")
    red_msg = None
except ValueError as e:
    red_msg = str(e)
check("RED: the untouched frozen-equivalent body still names raw kwargs",
      red_msg is not None and "context_overlap" in red_msg and "context_length" in red_msg, str(red_msg))

# GREEN: the public entry point (what the core actually calls) catches it
# FIRST, with the plain sentence -- for both modes that have this shape.
try:
    engines.graph_for("video", "ltx", {"prompt": "t", "length": 97, "context_length": 9}, m)
    green_msg_ltx = None
except ValueError as e:
    green_msg_ltx = str(e)
check("GREEN: video/ltx refuses with the plain sentence, no raw kwargs",
      green_msg_ltx == "The overlap must be shorter than the window.", str(green_msg_ltx))

try:
    engines.graph_for("video", "ltx_loop", {"prompt": "t", "length": 361, "temporal_tile_size": 20}, m)
    green_msg_loop = None
except ValueError as e:
    green_msg_loop = str(e)
check("GREEN: video/ltx_loop refuses with the plain sentence too (same bug shape, different kwargs)",
      green_msg_loop == "The overlap must be shorter than the window.", str(green_msg_loop))

# GREEN: the SAFE default combination (context_length unset, or window
# comfortably bigger than the default overlap) still builds -- this is
# already proven byte-identical above, restated here as the explicit
# negative-space check for this gate.
try:
    engines.graph_for("video", "ltx", dict(args["t2v"]), m)
    safe_built = True
except ValueError:
    safe_built = False
check("GREEN: the golden case's own args (no context_length set) still build with no error",
      safe_built)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
