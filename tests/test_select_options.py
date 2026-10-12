"""Gate: a "select" field's value is matched against each option by that option's own
type, and the OPTION itself is what the graph gets. Producer's Grid check and Re-arrange
declare beats_per_bar as ["auto", 2, 3, 4, 5, 6, 7]: coercing every value to the first
option's type turned 4 into "4", which is not in the list, so every numeric choice was
refused. An all-int select (turntable's size) and an all-str select keep their behaviour.

Run: python3 tests/test_select_options.py
"""
import importlib.util
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + repr(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


import _scratch_config  # noqa: E402,F401 -- before server.py's own exec_module
spec = importlib.util.spec_from_file_location("srv_select", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
import engines  # noqa: E402


def field(cap, mode, fid):
    return next(f for f in engines.fields(cap, mode) if f["id"] == fid)


def coerce(f, val):
    try:
        return "ok", srv._coerce_field_value(f, val)
    except ValueError as e:
        return "refused", str(e)


REFUSED = "Beats per bar must be one of auto, 2, 3, 4, 5, 6, 7."
for mode in ("grid", "arrange"):
    f = field("producer", mode, "beats_per_bar")
    print("producer/%s beats_per_bar %r" % (mode, f["options"]))
    for val, want in ((4, 4), ("4", 4), (" 4 ", 4), ("auto", "auto"), (7, 7)):
        got = coerce(f, val)
        check("%s: %r -> %r (%s)" % (mode, val, want, type(want).__name__),
              got[0] == "ok" and got[1] == want and type(got[1]) is type(want), got)
    for val in (9, "x", "", "4.5"):
        check("%s: %r is refused with the same sentence" % (mode, val), coerce(f, val) == ("refused", REFUSED),
              coerce(f, val))

size = field("3d", "turntable", "size")
print("turntable size %r (all int)" % (size["options"],))
for val, want in ((512, 512), ("512", 512), ("1024", 1024)):
    got = coerce(size, val)
    check("size: %r -> %r (int)" % (val, want), got[0] == "ok" and got[1] == want and type(got[1]) is int, got)
check("size: 600 is refused", coerce(size, 600)[0] == "refused", coerce(size, 600))

plain = {"id": "look", "label": "Look", "type": "select", "options": ["match", "max"]}
print("an all-str select")
check("str: 'max' -> 'max'", coerce(plain, "max") == ("ok", "max"), coerce(plain, "max"))
check("str: 'other' is refused", coerce(plain, "other") == ("refused", "Look must be one of match, max."),
      coerce(plain, "other"))
digits = {"id": "n", "label": "N", "type": "select", "options": ["1", "2"]}
check("str options of digits: 2 -> '2' (the option, a str)", coerce(digits, 2) == ("ok", "2"), coerce(digits, 2))

print("\nFAILED: %d" % len(FAILED) + (": " + ", ".join(FAILED) if FAILED else ""))
sys.exit(1 if FAILED else 0)
