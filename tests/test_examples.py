"""Acceptance gate for H2's "Try this" examples.

Every mode of every pack declares at least one example (a one-click recipe +
quality + field values the page fills in, never a Make). This pins the
contract engines/__init__.py's docstring describes: `values` may only name
declared field ids and must stay inside a field's own range/options,
`recipe`/`quality` must name real ids on that mode, example ids are unique
per mode, and GET /api/engines carries the whole thing through.

Run: python3 tests/test_examples.py
"""
import importlib.util
import os
import sys

sys.dont_write_bytecode = True  # see test_audio_golden.py's comment on stale .pyc
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)

import engines


def value_in_range(field, value):
    """Whether one example value obeys a field's own declared constraint --
    "options" for a select, "range" for a number/int, anything else for a
    field with neither (text/textarea/checkbox/upload types)."""
    if field.get("options") is not None:
        return value in field["options"]
    if field.get("range") is not None:
        lo, hi = field["range"]
        try:
            return lo <= float(value) <= hi
        except (TypeError, ValueError):
            return False
    return True


print("every (cap, mode) has at least one example")
all_modes = [(cap, mode) for cap in engines.caps() for mode in engines.modes_for(cap)]
check("at least one (cap, mode) exists", bool(all_modes))
for cap, mode in all_modes:
    exs = engines.examples(cap, mode)
    check("%s/%s: has >= 1 example" % (cap, mode), bool(exs))

print()
print("every example's shape is honest against its own mode's declared contract")
for cap, mode in all_modes:
    exs = engines.examples(cap, mode)
    if not exs:
        continue
    fields = {f["id"]: f for f in engines.fields(cap, mode)}
    preset_ids = {p["id"] for p in engines.presets(cap, mode)}
    quality_ids = {q["id"] for q in engines.quality(cap, mode)}
    ids = [ex["id"] for ex in exs]
    check("%s/%s: example ids unique" % (cap, mode), len(ids) == len(set(ids)), str(ids))
    for ex in exs:
        label = "%s/%s example %r" % (cap, mode, ex["id"])
        values = ex.get("values") or {}
        bad_keys = sorted(set(values) - set(fields))
        check("%s: values keys are declared fields" % label, not bad_keys, str(bad_keys))
        for k, v in values.items():
            f = fields.get(k)
            if f is None:
                continue
            check("%s: value for %r is within its field's range/options" % (label, k),
                  value_in_range(f, v), "value=%r field=%r" % (v, f))
        recipe = ex.get("recipe")
        check("%s: recipe id exists among this mode's presets" % label,
              recipe is None or recipe in preset_ids, "recipe=%r presets=%r" % (recipe, sorted(preset_ids)))
        qual = ex.get("quality")
        check("%s: quality id exists among this mode's tiers" % label,
              qual is None or qual in quality_ids, "quality=%r tiers=%r" % (qual, sorted(quality_ids)))
        check("%s: has a 'why' line" % label, bool(ex.get("why")))
        check("%s: 'needs' is None or a plain string" % label,
              ex.get("needs") is None or isinstance(ex.get("needs"), str))

print()
print("GET /api/engines carries examples per mode")
import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below
spec = importlib.util.spec_from_file_location("srv_examples", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
payload = srv.Handler.engines_payload(None, {})
for cap, mode in all_modes:
    modes = payload.get(cap, {}).get("modes", [])
    entry = next((m for m in modes if m["id"] == mode), None)
    check("%s/%s: present in /api/engines" % (cap, mode), entry is not None)
    if entry is None:
        continue
    check("%s/%s: /api/engines 'examples' matches engines.examples()" % (cap, mode),
          entry.get("examples") == engines.examples(cap, mode), repr(entry.get("examples")))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
