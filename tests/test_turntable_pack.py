"""Acceptance gate for the turntable pack (slice B2 of
the internal Blender process-lane design doc): the pack's contract (fields, tiers,
presets, licence), its run plan (bin tokens, background in the argv,
per-size timeout budget capped at 21600 s, progress regex, outputs),
invalid input refused with ValueError, and that the Blender script the
plan points at exists where {pack} resolves it, compiles, and stays CPU.

Run: python3 tests/test_turntable_pack.py
"""
import os
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import engines
import runner

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

K, M = "3d", "turntable"
PACKS = {p["id"]: p for p in engines.packs()}

# 1. discovery: the pack is there, is a process pack, and owns the bins
check("pack discovered", M in PACKS)
check("cap is 3d", PACKS.get(M, {}).get("cap") == K)
check("lane_kind is process", engines.lane_kind(K, M) == "process")
check("bins are exactly blender+ffmpeg",
      engines.bins(K, M) == {"blender": "blender", "ffmpeg": "ffmpeg"},
      repr(engines.bins(K, M)))
check("modes_for(3d) includes turntable", M in engines.modes_for(K))
check("mode_rooms points at the 3d room", engines.mode_room(K, M) == "3d")
check("mode_words is the spec's line",
      (engines.mode_words(K) or {}).get(M) == "Turn a 3D model around",
      repr((engines.mode_words(K) or {}).get(M)))

# 2. {pack} resolves to the engines dir, and the script the plan names exists
pack_dir = engines.pack_dir(K, M)
script = os.path.join(pack_dir, "blender", "turntable.py")
check("pack_dir is the engines directory",
      os.path.realpath(pack_dir) == os.path.realpath(os.path.join(ROOT, "engines")), pack_dir)
check("blender script exists at {pack}/blender/turntable.py", os.path.isfile(script), script)

# 3. fields: the model input leads; the spec's fields, defaults and ranges
fields = engines.fields(K, M)
check("fields declared", bool(fields))
by_id = {f["id"]: f for f in fields}
check("first field is the model input",
      fields and fields[0]["id"] == "model" and fields[0]["type"] == "model",
      repr(fields[:1]))
for f in fields:
    if f["type"] in ("int", "number"):
        rng = f.get("range")
        ok = bool(rng)
        d = f.get("default")
        check("numeric %s has a range and a default inside it" % f["id"],
              ok and d is not None and rng[0] <= d <= rng[1], repr((d, rng)))
check("frames: int, default 72, range [24, 240], units frames",
      by_id["frames"]["type"] == "int" and by_id["frames"]["default"] == 72
      and by_id["frames"]["range"] == [24, 240] and by_id["frames"]["units"] == "frames",
      repr(by_id["frames"]))
check("size: select over the four frame sizes, default 512",
      by_id["size"]["type"] == "select"
      and sorted(by_id["size"]["options"]) == [384, 512, 768, 1024]
      and by_id["size"]["default"] == 512, repr(by_id["size"]))
check("fps: int, default 24, range [12, 60], advanced",
      by_id["fps"]["type"] == "int" and by_id["fps"]["default"] == 24
      and by_id["fps"]["range"] == [12, 60] and by_id["fps"]["tier"] == "advanced",
      repr(by_id["fps"]))
check("samples: int, default 32, range [8, 256], advanced",
      by_id["samples"]["type"] == "int" and by_id["samples"]["default"] == 32
      and by_id["samples"]["range"] == [8, 256] and by_id["samples"]["tier"] == "advanced",
      repr(by_id["samples"]))
check("elevation: number, default 20, range [-10, 60], units degrees, advanced",
      by_id["elevation"]["type"] == "number" and by_id["elevation"]["default"] == 20
      and by_id["elevation"]["range"] == [-10, 60]
      and by_id["elevation"]["units"] == "degrees"
      and by_id["elevation"]["tier"] == "advanced", repr(by_id["elevation"]))
check("background: select dark/light/transparent, default dark, advanced",
      by_id["background"]["type"] == "select"
      and by_id["background"]["options"] == ["dark", "light", "transparent"]
      and by_id["background"]["default"] == "dark"
      and by_id["background"]["tier"] == "advanced", repr(by_id["background"]))
check("background hint says the video is on black",
      "transparent keeps alpha in the poster" in by_id["background"].get("hint", ""),
      repr(by_id["background"].get("hint")))

# 4. quality: exactly draft/standard/high, standard default, spec values,
#    every tier's values inside the field ranges
tiers = {t["id"]: t for t in (engines.quality(K, M) or [])}
check("quality tiers are exactly draft/standard/high",
      set(tiers) == {"draft", "standard", "high"}, repr(set(tiers)))
check("exactly one default tier, it is standard",
      [t for t in tiers.values() if t.get("default")] == [tiers.get("standard")])
check("draft values are the spec's",
      tiers.get("draft", {}).get("values") == {"frames": 48, "size": 384, "samples": 16},
      repr(tiers.get("draft", {}).get("values")))
check("standard values are the spec's",
      tiers.get("standard", {}).get("values") == {"frames": 72, "size": 512, "samples": 32},
      repr(tiers.get("standard", {}).get("values")))
check("high values are the spec's",
      tiers.get("high", {}).get("values") == {"frames": 120, "size": 768, "samples": 64},
      repr(tiers.get("high", {}).get("values")))
check("every tier carries its spec why text",
      all((tiers.get(t, {}).get("why") or "").strip() for t in ("draft", "standard", "high")))
for t, tv in tiers.items():
    v = tv.get("values", {})
    check("tier %s values inside the field ranges" % t,
          by_id["frames"]["range"][0] <= v.get("frames", -1) <= by_id["frames"]["range"][1]
          and v.get("size") in by_id["size"]["options"]
          and by_id["samples"]["range"][0] <= v.get("samples", -1) <= by_id["samples"]["range"][1],
          repr(v))
check("at least one preset", len(engines.presets(K, M) or []) >= 1)

# 5. user-facing words: the note carries its source comment on the line
#    itself (repo convention, cf. mesh3d)
note_line_ok = False
for line in open(os.path.join(ROOT, "engines", M + ".py")):
    if '"turntable": "renders on the processor' in line and "# source:" in line:
        note_line_ok = True
check("mode_notes line carries its # source: comment", note_line_ok)

# 6. the plan: two steps (blender render, ffmpeg encode), background in the
#    argv, tokens intact
def plan_for(**over):
    args = {"model": "x.glb", "frames": 72, "fps": 24, "size": 512,
            "samples": 32, "background": "dark", "elevation": 20}
    args.update(over)
    return engines.graph_for(K, M, args, {"blender": "b", "ffmpeg": "f"})

pl = plan_for()
check("plan has exactly two steps", len(pl.get("steps", [])) == 2, repr(pl))
s1, s2 = pl["steps"][0], pl["steps"][1]
check("step 1 is blender and names the script",
      s1["argv"][0] == "{bin:blender}" and "{pack}/blender/turntable.py" in s1["argv"], repr(s1["argv"]))
check("step 1 stages the uploaded input via {in:model}",
      "{in:model}" in s1["argv"], repr(s1["argv"]))
check("step 1 carries the background value in the argv",
      s1["argv"][s1["argv"].index("--background") + 1] == "dark", repr(s1["argv"]))
pl_bg = plan_for(background="transparent")
check("step 1 honours a non-default background",
      pl_bg["steps"][0]["argv"][pl_bg["steps"][0]["argv"].index("--background") + 1] == "transparent",
      repr(pl_bg["steps"][0]["argv"]))
# Sparse-dict regression (the live path forwards ONLY what the client sent
# plus the quality tier's values -- field defaults are applied HERE, in the
# graph, not by the caller): a nearly-empty dict must still build a plan,
# with the declared defaults (72 frames, 512 px, dark background).
pl_default = engines.graph_for(K, M, {"model": "x.glb"}, {"blender": "b", "ffmpeg": "f"})
a0 = (pl_default or {}).get("steps", [{}])[0].get("argv", [])
check("plan builds from a sparse dict (field defaults applied in-graph)",
      len((pl_default or {}).get("steps", [])) == 2 and "--frames" in a0 and "--background" in a0,
      repr(a0))
check("sparse plan uses the default 72-frame budget (30 s/frame, capped 21600)",
      pl_default["steps"][0]["timeout_s"] == min(30 * 72, 21600),
      repr(pl_default["steps"][0].get("timeout_s")))
small = plan_for(frames=24, size=384)["steps"][0]
big = plan_for(frames=240, size=768)["steps"][0]
check("timeout scales with frames at <=512 (30 s each)",
      small["timeout_s"] == 30 * 24, repr(small["timeout_s"]))
check("timeout moves to the 60 s/frame budget above 512",
      big["timeout_s"] == 60 * 240, repr(big["timeout_s"]))
check("timeout never exceeds 21600",
      all(s["timeout_s"] <= 21600 for s in (small, big, s1)))
check("step 2 is ffmpeg and encodes the frame dir",
      s2["argv"][0] == "{bin:ffmpeg}" and "{job}/frames/f_%04d.png" in s2["argv"]
      and "{job}/turntable.mp4" in s2["argv"] and s2["timeout_s"] == 600, repr(s2["argv"]))
check("ffmpeg input pattern matches the script's frame naming",
      "f_%04d.png" in open(script).read(), "grep f_%%04d.png in the script")
check("outputs are the video and the poster",
      pl.get("outputs") == ["turntable.mp4", "poster.png"], repr(pl.get("outputs")))
check("progress regex matches the script's PROGRESS lines",
      re.match(pl.get("progress", ""), "PROGRESS 3/48") is not None, repr(pl.get("progress")))
check("every placeholder in the argv is a known one",
      all(re.fullmatch(r"\{(bin|in):[A-Za-z0-9_]+\}|\{job\}|\{pack\}", ph)
          for s in pl["steps"] for t in s["argv"] for ph in re.findall(r"\{[^{}]*\}", t)),
      repr(pl["steps"]))

# 7. bad input is a ValueError with a sentence
for bad in ({"frames": 5}, {"size": 999}, {"background": "pink"},
            {"samples": 1}, {"fps": 200}, {"elevation": 90}):
    try:
        plan_for(**bad)
        check("args %r refused" % (bad,), False, "no ValueError")
    except ValueError:
        check("args %r refused with ValueError" % (bad,), True)
    else:
        check("args %r refused" % (bad,), False)

# 8. resolution: with stub bins the argv resolves, no {…} left; the script
#    path in argv is the real file
job = tempfile.mkdtemp(prefix="tt-job-")
steps = runner.resolve_plan(pl, {"blender": "/x/blender", "ffmpeg": "/x/ffmpeg"},
                            job, {"model": "/tmp/x.glb"}, pack_dir)
check("resolve: no unresolved placeholders",
      all(t.count("{") == 0 and t.count("}") == 0 for s in steps for t in s["argv"]),
      repr(steps))
check("resolve: step 1 argv[0] is the blender bin", steps[0]["argv"][0] == "/x/blender",
      repr(steps[0]["argv"][:1]))
check("resolve: blender script path is the real file",
      any(t == os.path.abspath(script) for s in steps for t in s["argv"]), repr(steps[0]))
check("resolve: input staged path made it into argv",
      any(t == "/tmp/x.glb" for s in steps for t in s["argv"]))

# 9. the Blender script: compiles, and is CPU by construction
compiled = subprocess.run([sys.executable, "-m", "py_compile", script])
check("blender script compiles", compiled.returncode == 0)
src = open(script).read()
check("blender script is pinned to the CPU", 'scene.cycles.device = "CPU"' in src)
check("blender script names no GPU device",
      re.search(r'device\s*=\s*["\'](?:GPU|CUDA|OPTIX|HIP|METAL)["\']',
                src, re.IGNORECASE) is None)
check("blender script disarms GPU compute devices",
      'compute_device_type = "NONE"' in src)

# 10. licence: the pack runs Blender, which is GPL
lic = engines.licence_for(K, M)
check("licence present and shippable",
      bool(lic) and lic.get("shippable") is True and lic.get("name"), repr(lic))
check("licence names Blender's GPL",
      lic.get("name") == "GPL-2.0-or-later (Blender)", repr(lic.get("name")))

print()
if FAILED:
    print("TURNTABLE PACK GATE: %d FAILED" % len(FAILED))
    sys.exit(1)
print("TURNTABLE PACK GATE: all passed")
