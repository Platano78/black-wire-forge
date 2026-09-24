"""Acceptance gate for the audio engine pack (slice A, atomic task audio-1).

The pack is correct iff it reproduces the FROZEN graph_builders.py output
byte-for-byte, modulo exactly one declared deviation per mode (R7's
filename_prefix: owui/... in the frozen source, blackwire/... in the pack,
matching the other packs' convention). Golden files were captured straight
from the frozen source by tests/capture_audio_golden.py -- never from the
pack under test.

Run: python3 tests/test_audio_golden.py
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

models = json.load(open(os.path.join(G, "audio_models.json")))
args = json.load(open(os.path.join(G, "audio_args.json")))

# mode -> golden file -> the ONLY node/field path allowed to differ from the
# frozen output. Each entry is (node_id, input_field, expected_frozen_value,
# expected_pack_value).
CASES = {
    "song":  ("audio_song",  "107", "owui/song",  "blackwire/SONG"),
    "music": ("audio_music", "9",   "owui/music", "blackwire/MUSIC"),
    "sfx":   ("audio_sfx",   "8",   "owui/sfx",   "blackwire/SFX"),
    "yue2":  ("audio_yue2",  "40",  "owui/yue2",  "blackwire/YUE2"),
    "cover": ("audio_cover", "58",  "owui/cover", "blackwire/COVER"),
}


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


print("audio graphs reproduce the frozen source exactly, modulo R7's filename_prefix")
for mode, (golden_name, node, frozen_val, pack_val) in CASES.items():
    want = json.load(open(os.path.join(G, golden_name + ".json")))
    got = engines.graph_for("audio", mode, dict(args[mode]), models)
    diffs = diff_paths(want, got)
    allowlist_path = (node, "inputs", "filename_prefix")
    unexpected = [d for d in diffs if d[0] != allowlist_path]
    check("%s: no undeclared differences" % mode, not unexpected,
          str(unexpected[:3]))
    allowed = [d for d in diffs if d[0] == allowlist_path]
    check("%s: the ONLY difference is filename_prefix" % mode,
          allowed == [(allowlist_path, frozen_val, pack_val)],
          str(allowed))

print()
print("G2 falsifiability proof: perturb one pack value, confirm RED, then GREEN again")
import engines.audio as audio_mod
_orig = audio_mod.sfx_graph
def _broken(p, m):
    g = _orig(p, m)
    g["6"]["inputs"]["cfg"] = 999.0   # perturb a value the golden pins
    return g
audio_mod.sfx_graph = _broken
audio_mod.ENGINE["graphs"]["sfx"] = _broken
engines._PACKS = None  # force rescan so graph_for sees the perturbed function
want = json.load(open(os.path.join(G, "audio_sfx.json")))
got = engines.graph_for("audio", "sfx", dict(args["sfx"]), models)
red = diff_paths(want, got)
red_unexpected = [d for d in red if d[0] != ("8", "inputs", "filename_prefix")]
print("  RED (perturbed):  %d unexpected diff(s): %s" % (len(red_unexpected), red_unexpected[:2]))
check("perturbation is caught (RED)", len(red_unexpected) > 0)

audio_mod.sfx_graph = _orig
audio_mod.ENGINE["graphs"]["sfx"] = _orig
engines._PACKS = None
got2 = engines.graph_for("audio", "sfx", dict(args["sfx"]), models)
green = diff_paths(want, got2)
green_unexpected = [d for d in green if d[0] != ("8", "inputs", "filename_prefix")]
print("  GREEN (reverted): %d unexpected diff(s): %s" % (len(green_unexpected), green_unexpected[:2]))
check("revert is clean again (GREEN)", len(green_unexpected) == 0)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
