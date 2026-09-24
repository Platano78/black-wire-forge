"""Gate for rooms by task (slice R, the internal rooms-by-task design doc).

Pins engines.rooms() -- rooms.json merged with each pack's `mode_rooms`:
1. every mode of every pack lands in exactly one room;
2. the ten task rooms + the Cutting Room exist, in order;
3. a mode naming an unknown room, and a mode with no `mode_rooms` entry,
   each still get a room (a stranger's pack shows up) -- proved with a
   SYNTHETIC pack, never by breaking a real one;
4. a room no pack fills reports `modes: []`;
5. rooms.json names no engine, and every `mode_notes` line is plain words.

Run: python3 tests/test_rooms.py
"""
import json
import os
import re
import sys
import tempfile

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

ROOMS = engines.rooms()
ALL_MODES = [(cap, mode) for cap in engines.caps() for mode in engines.modes_for(cap)]

print("every mode of every pack lands in exactly one room")
placed = {}
for r in ROOMS:
    for m in r.get("modes") or []:
        placed.setdefault((m["cap"], m["mode"]), []).append(r["id"])
check("the real packs declare at least one mode", len(ALL_MODES) > 0)
for cap, mode in ALL_MODES:
    where = placed.get((cap, mode), [])
    check("%s/%s is in exactly one room" % (cap, mode), len(where) == 1, repr(where))
check("no room lists a mode no pack provides", set(placed) <= set(ALL_MODES),
      repr(set(placed) - set(ALL_MODES)))

print()
print("the ten task rooms + the Cutting Room exist, in order")
WANT = ["music", "cover", "sfx", "picture", "pixelart", "cleanup", "textures", "video", "talking", "3d", "cutting"]
ids = [r["id"] for r in ROOMS]
check("room ids in order == %r" % WANT, ids[:len(WANT)] == WANT, repr(ids))
check("no fallback room exists for the real packs", len(ids) == len(WANT), repr(ids[len(WANT):]))
orders = [r["order"] for r in ROOMS]
check("orders ascend", orders == sorted(orders), repr(orders))
GROUPS = {"music": "SOUND", "cover": "SOUND", "sfx": "SOUND", "picture": "PICTURE", "pixelart": "PICTURE",
          "cleanup": "PICTURE", "textures": "PICTURE", "video": "MOTION", "talking": "MOTION", "3d": "OBJECT"}
by_id = {r["id"]: r for r in ROOMS}
for rid, g in GROUPS.items():
    r = by_id.get(rid, {})
    check("%s: group %s" % (rid, g), r.get("group") == g, repr(r.get("group")))
    check("%s: has a name, a blurb and an empty line" % rid,
          bool(r.get("name")) and bool(r.get("blurb")) and bool(r.get("empty")), repr(r))
cut = by_id.get("cutting", {})
check("cutting room is kind=cutting, named 'Cutting Room'", cut.get("kind") == "cutting" and cut.get("name") == "Cutting Room",
      repr(cut))
check("cutting room is passed through (carries no modes)", not cut.get("modes"), repr(cut.get("modes")))

print()
print("rooms_spec.md's table: which modes each room holds today, in (cap_order, pack id, graphs) order")
WANT_MODES = {
    "music": ["song", "music", "yue2"], "cover": ["cover"], "sfx": ["sfx"],
    "picture": ["t2i", "edit"], "pixelart": ["pixelart"], "cleanup": ["cutout", "upscale"],
    "video": ["ltx", "ltx_loop", "fl2va", "ref2v", "continue"], "talking": ["talking"], "3d": ["mesh", "turntable"],
}
for rid, want in WANT_MODES.items():
    got = [m["mode"] for m in by_id.get(rid, {}).get("modes") or []]
    check("%s modes == %r" % (rid, want), got == want, repr(got))
check("Picture opens on text-to-picture (its first mode is t2i, not a tool)",
      (by_id.get("picture", {}).get("modes") or [{}])[0].get("mode") == "t2i")

print()
print("a room with no pack reports modes: []")
check("textures has modes == []", by_id.get("textures", {}).get("modes") == [],
      repr(by_id.get("textures", {}).get("modes")))

print()
print("fallback rooms, proved on a SYNTHETIC pack (unknown room id + no mode_rooms entry)")
SYNTH = {
    "id": "zz-synthetic", "cap": "image", "roles": {}, "provides": {}, "words": {},
    "graphs": {"odd": lambda a, m: {}, "bare": lambda a, m: {}, "odd2": lambda a, m: {}},
    "describe": lambda m: "",
    "mode_words": {"odd": "An odd thing", "bare": "A bare thing"},
    "mode_rooms": {"odd": "no-such-room", "odd2": "no-such-room"},
}
real = engines._discover()
engines._PACKS = real + [SYNTH]
try:
    fr = engines.rooms()
finally:
    engines._PACKS = real
fby = {r["id"]: r for r in fr}
odd = fby.get("no-such-room")
check("unknown room id -> a room is built for it", odd is not None, repr([r["id"] for r in fr]))
if odd:
    check("  named after the first mode's own label", odd["name"] == "An odd thing", repr(odd["name"]))
    check("  grouped under its cap's word, upper-cased", odd["group"] == engines.cap_word("image").upper(),
          repr(odd["group"]))
    check("  ordered 1000+", odd["order"] >= 1000, repr(odd["order"]))
    check("  both modes naming it share the one room", [m["mode"] for m in odd["modes"]] == ["odd", "odd2"],
          repr(odd["modes"]))
bare = fby.get("image-bare")
check("no mode_rooms entry -> room id <cap>-<mode>", bare is not None, repr([r["id"] for r in fr]))
if bare:
    check("  named after the mode's label", bare["name"] == "A bare thing", repr(bare["name"]))
    check("  holds exactly that mode", bare["modes"] == [{"cap": "image", "mode": "bare"}], repr(bare["modes"]))
check("fallback rooms sort after the declared ones", [r["id"] for r in fr][:len(WANT)] == WANT)
check("the real rooms are untouched by the synthetic pack",
      [m["mode"] for m in fby["picture"]["modes"]] == WANT_MODES["picture"])

print()
print("an empty rooms.json room with no pack (a synthetic rooms file)")
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
    json.dump([{"id": "nobody", "name": "Nobody", "group": "X", "order": 1, "blurb": "b", "empty": "e"}], tf)
try:
    only = engines.rooms(tf.name)
finally:
    os.unlink(tf.name)
nb = [r for r in only if r["id"] == "nobody"]
check("a room nothing fills keeps modes: []", bool(nb) and nb[0]["modes"] == [], repr(nb))
check("every real mode then falls back to its own room, still exactly once",
      sorted((m["cap"], m["mode"]) for r in only for m in r["modes"]) == sorted(ALL_MODES))

print()
print("a missing or broken rooms.json falls back to rooms built from the packs, never raises")
import logging
logging.disable(logging.WARNING)   # the warning is expected here; keep the gate output readable
try:
    for label, content in (("missing", None), ("unparseable", "{not json"), ("wrong shape", '{"rooms": 1}')):
        if content is None:
            fpath = os.path.join(tempfile.gettempdir(), "no-such-rooms-%d.json" % os.getpid())
        else:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
                tf.write(content)
            fpath = tf.name
        try:
            got = engines.rooms(fpath)
            err = None
        except Exception as e:  # the defect this pins
            got, err = [], e
        finally:
            if content is not None:
                os.unlink(fpath)
        check("%s rooms.json: rooms() does not raise" % label, err is None, repr(err))
        check("%s rooms.json: every mode still lands in exactly one room" % label,
              sorted((m["cap"], m["mode"]) for r in got for m in r.get("modes") or []) == sorted(ALL_MODES))
finally:
    logging.disable(logging.NOTSET)

print()
print("rooms.json names no engine; mode_notes are plain words")
ENGINE_TOKENS = {p["id"] for p in engines.packs() if p["id"] not in engines.caps()} | {
    "qwen", "minimax", "h3", "ace", "yue", "stable audio", "ltx", "trellis", "sdxl",
    "birefnet", "esrgan", "rife",
}
ENGINE_NAME = re.compile(r"\b(" + "|".join(re.escape(t) for t in ENGINE_TOKENS) + r")\b", re.I)
PLUMBING = re.compile(r"\b(pipeline|model|workflow|cfg|vae|sampler|scheduler|latent|checkpoint|lora)s?\b", re.I)
raw = json.load(open(os.path.join(ROOT, "rooms.json")))
for r in raw:
    for k in ("name", "group", "blurb", "empty"):
        v = r.get(k) or ""
        check("rooms.json %s.%s: no engine name" % (r["id"], k), not ENGINE_NAME.search(v), v)
        check("rooms.json %s.%s: no plumbing words" % (r["id"], k), not PLUMBING.search(v), v)
check("RED: a planted engine name is caught", bool(ENGINE_NAME.search("Clean-up with BiRefNet")))
noted = 0
for cap, mode in ALL_MODES:
    note = engines.mode_note(cap, mode)
    if note is None:
        continue
    noted += 1
    check("%s/%s note: no engine name" % (cap, mode), not ENGINE_NAME.search(note), note)
    check("%s/%s note: no plumbing words" % (cap, mode), not PLUMBING.search(note), note)
    check("%s/%s note: one line" % (cap, mode), "\n" not in note and " -- " not in note, note)
check("GREEN: the real packs carry at least one note to check", noted > 0)

print()
print("every mode_notes line carries a '# source:' comment beside it")
ENG = os.path.join(ROOT, "engines")
for fn in sorted(os.listdir(ENG)):
    if not fn.endswith(".py") or fn.startswith("_"):
        continue
    src = open(os.path.join(ENG, fn)).read()
    m = re.search(r'"mode_notes":\s*\{(.*?)\n    \}', src, re.S)
    if not m:
        continue
    lines = [ln for ln in m.group(1).splitlines() if re.match(r'\s*"[^"]+":\s*"', ln)]
    for ln in lines:
        check("%s: %s has a # source:" % (fn, ln.strip().split(":")[0]), "# source:" in ln, ln.strip())

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
