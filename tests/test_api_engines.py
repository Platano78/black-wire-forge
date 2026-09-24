"""Acceptance gate for GET /api/engines (slice B, R1/R2).

Two things this pins:
1. Every audio mode is declared, and every declared mode has a non-empty
   field list -- the UI has nothing else to build a form from.
2. R2's promise made mechanical: every field's `default` equals the FROZEN
   builder's own default for that kwarg, read via `inspect.signature` --
   not retyped by hand, so a default cannot silently drift from the source
   it is supposed to carry forward.

Run: python3 tests/test_api_engines.py
"""
import importlib.util
import inspect
import json
import os
import re
import sys
import types

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

FROZEN_PATH = "/nonexistent/graph_builders.py"  # frozen source, not included in this repo
# audio mode -> the frozen builder function name it must agree with. The names
# alone (not the functions) are enough for the structural checks below; only
# R2 (further down) needs the frozen module itself.
FROZEN_FN = {"song": "build_song", "music": "build_music", "sfx": "build_sfx",
             "yue2": "build_yue2", "cover": "build_yue2_cover"}
if os.path.isfile(FROZEN_PATH):
    spec = importlib.util.spec_from_file_location("frozen_graph_builders", FROZEN_PATH)
    frozen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frozen)
    BUILDER = {mode: getattr(frozen, fn) for mode, fn in FROZEN_FN.items()}
else:
    # Not included in the public repo -- see R2 below, which is the only
    # check that needs the frozen module itself rather than just mode names.
    frozen = None
    BUILDER = dict.fromkeys(FROZEN_FN)

print("every audio mode is declared with a non-empty field list")
modes = engines.modes_for("audio")
check("all five audio modes present", sorted(modes) == sorted(BUILDER),
      str(sorted(modes)))
for mode in BUILDER:
    fields = engines.fields("audio", mode)
    check("%s: has a non-empty field list" % mode, bool(fields))
    ids = [f["id"] for f in fields]
    check("%s: field ids are unique" % mode, len(ids) == len(set(ids)), str(ids))

print()
print("mode_words gives every mode a label")
words = engines.mode_words("audio")
for mode in BUILDER:
    check("%s: has a mode_words label" % mode, bool(words.get(mode)), repr(words.get(mode)))

print()
print("R2: every field default equals the FROZEN builder's own default for that kwarg")
if frozen is None:
    print("  SKIP  needs the frozen graph-builder source, not included in the public repo")
else:
    for mode, builder in BUILDER.items():
        sig = inspect.signature(builder)
        for f in engines.fields("audio", mode):
            fid = f["id"]
            if fid not in sig.parameters:
                continue  # required positional (no default) -- nothing to compare
            param = sig.parameters[fid]
            if param.default is inspect.Parameter.empty:
                # The frozen builder requires this arg (no default) -- e.g. build_sfx's
                # `negative`. The pack (engines/audio.py, untouched here) may still
                # supply a p.get(..., "") fallback to make it optional in the UI; that
                # is a declared UI decision, not disagreement with a frozen default
                # that does not exist, so there is nothing to compare here.
                continue
            check("%s.%s: default %r matches frozen %r" % (mode, fid, f.get("default"), param.default),
                  f.get("default") == param.default,
                  "field=%r frozen=%r" % (f.get("default"), param.default))
            # Python's `138 == 138.0` is True, so the value check above alone would
            # NOT have caught the actual C2 defect: bpm's DECLARED default was
            # already the int 138 (equal to frozen's int 138) -- the bug was the
            # field's "type": "number", which server.py coerces with float() at
            # request time, silently turning 138 into 138.0 in the dispatched
            # graph. So the gate that matters checks the declared TYPE against
            # the frozen default's Python type, not just the default value.
            # bool is deliberately not conflated with int (Python's own
            # `isinstance(True, int)` is True, which would wrongly pass a
            # checkbox field here if compared with isinstance instead of type()).
            frozen_type = type(param.default)
            if frozen_type is bool:
                want_field_type = "checkbox"
            elif frozen_type is int:
                want_field_type = "int"
            elif frozen_type is float:
                want_field_type = "number"
            elif frozen_type is str:
                want_field_type = {"text", "textarea", "select"}
            else:
                want_field_type = None
            if want_field_type is not None:
                ok = (f["type"] in want_field_type if isinstance(want_field_type, set)
                      else f["type"] == want_field_type)
                check("%s.%s: field type %r matches frozen default's type %s" % (
                          mode, fid, f["type"], frozen_type.__name__),
                      ok, "field type=%r frozen type=%s" % (f["type"], frozen_type.__name__))

print()
print("R1/R4: curation gate -- every mode has 1-5 primary fields, across ALL packs")
ALL_MODES = [(cap, mode) for cap in engines.caps() for mode in engines.modes_for(cap)]
for cap, mode in ALL_MODES:
    fields = engines.fields(cap, mode)
    primary = [f for f in fields if f.get("tier") == "primary"]
    check("%s/%s: has at least one primary field" % (cap, mode), len(primary) >= 1,
          str([f["id"] for f in fields]))
    check("%s/%s: has at most 5 primary fields (this IS the curation gate)" % (cap, mode),
          len(primary) <= 5, "primary=%s" % [f["id"] for f in primary])

print()
print("R1: every numeric field declares units, and ui_range lies inside range")
for cap, mode in ALL_MODES:
    for f in engines.fields(cap, mode):
        if f["type"] not in ("number", "int"):
            continue
        fid = "%s/%s.%s" % (cap, mode, f["id"])
        check("%s: declares units" % fid, bool(f.get("units")), repr(f.get("units")))
        rng, ui = f.get("range"), f.get("ui_range")
        if rng is None or ui is None:
            check("%s: declares range and ui_range" % fid, False, "range=%r ui_range=%r" % (rng, ui))
            continue
        check("%s: ui_range %r lies inside range %r" % (fid, ui, rng),
              rng[0] <= ui[0] and ui[1] <= rng[1])

print()
print("R2: every enabled_when names a field that exists in that same mode")
for cap, mode in ALL_MODES:
    ids = {f["id"] for f in engines.fields(cap, mode)}
    for f in engines.fields(cap, mode):
        cond = f.get("enabled_when")
        if cond is None:
            continue
        check("%s/%s.%s: enabled_when names a real field" % (cap, mode, f["id"]),
              cond.get("field") in ids, repr(cond))
        check("%s/%s.%s: enabled_when has a disabled_reason" % (cap, mode, f["id"]),
              bool(f.get("disabled_reason")))

print()
print("R3: every preset's values keys exist as fields of that mode")
for cap, mode in ALL_MODES:
    ids = {f["id"] for f in engines.fields(cap, mode)}
    for preset in engines.presets(cap, mode):
        unknown = set(preset["values"]) - ids
        check("%s/%s preset %r: values keys are all real fields" % (cap, mode, preset["id"]),
              not unknown, "unknown=%s" % sorted(unknown))

print()
print("R3: every mode of every cap declares at least one preset")
for cap, mode in ALL_MODES:
    n = len(engines.presets(cap, mode))
    check("%s/%s: has >=1 preset" % (cap, mode), n >= 1, "got %d" % n)

print()
print("R3: every audio mode declares at least two presets")
for mode in BUILDER:
    n = len(engines.presets("audio", mode))
    check("audio/%s: has >=2 presets" % mode, n >= 2, "got %d" % n)

print()
print("R1/R2/R4/R5/R6: quality ladders (slice C2a)")

ALL_MODES_Q = [(cap, mode) for cap in engines.caps() for mode in engines.modes_for(cap)]


def validate_tiers(cap, mode, tiers):
    """Every violation of R1/R2's tier contract, as plain strings -- shared
    between the perturbation proof below and the real-data gate."""
    ids = {f["id"] for f in engines.fields(cap, mode)}
    out = []
    if not tiers:
        out.append("%s/%s: no tiers declared" % (cap, mode))
    defaults = [t for t in tiers if t.get("default")]
    if len(defaults) != 1:
        out.append("%s/%s: %d tiers marked default (want exactly 1)" % (cap, mode, len(defaults)))
    for t in tiers:
        unknown = set(t.get("values", {})) - ids
        if unknown:
            out.append("%s/%s tier %r: values keys not real fields: %s" % (cap, mode, t["id"], sorted(unknown)))
        req = t.get("requires")
        if req and req not in engines.abilities({}):
            out.append("%s/%s tier %r: requires %r, not a real ability" % (cap, mode, t["id"], req))
        if req and not t.get("unavailable_reason"):
            out.append("%s/%s tier %r: requires %r but no unavailable_reason" % (cap, mode, t["id"], req))
    return out


print()
print("falsifiability proof: a broken tier list is CAUGHT, then the real data is clean")
broken = [
    {"id": "a", "label": "A", "why": "x", "values": {"not_a_real_field": 1}, "default": True},
    {"id": "b", "label": "B", "why": "y", "values": {}, "default": True},   # two defaults
]
violations = validate_tiers("image", "t2i", broken)
check("RED: a broken tier list is caught", len(violations) >= 2, str(violations))
for v in violations:
    print("    RED  " + v)
violations = validate_tiers("image", "t2i", engines.quality("image", "t2i"))
check("GREEN: image/t2i's real tiers are clean", violations == [], str(violations))

print()
print("every mode of every cap has >=1 tier and exactly one default")
for cap, mode in ALL_MODES_Q:
    tiers = engines.quality(cap, mode)
    check("%s/%s: has >=1 quality tier" % (cap, mode), len(tiers) >= 1)
    defaults = [t for t in tiers if t.get("default")]
    check("%s/%s: exactly one default tier" % (cap, mode), len(defaults) == 1,
          "got %d: %s" % (len(defaults), [t["id"] for t in defaults]))

print()
print("every tier's values keys are real fields; every requires names a real ability")
for cap, mode in ALL_MODES_Q:
    violations = validate_tiers(cap, mode, engines.quality(cap, mode))
    check("%s/%s: quality tiers pass validate_tiers" % (cap, mode), violations == [], str(violations))

print()
print("image/video ladder values equal an earlier page's constants (golden captured "
      "from that commit's index.html -- the current tree's index.html no longer carries "
      "IMG_Q/VID_Q at all, the ladders live in the packs now)")
_old = json.load(open(os.path.join(HERE, "golden", "old_page_quality.json")))
img_steps, img_why = _old["img_steps"], _old["img_why"]
vid_steps, vid_why = _old["vid_steps"], _old["vid_why"]
for mode in ("t2i", "edit"):
    got = [t["values"]["steps"] for t in engines.quality("image", mode)]
    check("image/%s: step counts match IMG_Q %s" % (mode, img_steps), got == img_steps, str(got))
    for t in engines.quality("image", mode):
        want = img_why.get(str(t["values"]["steps"]))
        check("image/%s tier %r: why matches IMG_Q_WHY" % (mode, t["id"]), t["why"] == want,
              "got=%r want=%r" % (t["why"], want))
got = [t["values"]["steps"] for t in engines.quality("video", "fl2va")]
check("video/fl2va: step counts match VID_Q %s" % vid_steps, got == vid_steps, str(got))
for t in engines.quality("video", "fl2va"):
    want = vid_why.get(str(t["values"]["steps"]))
    check("video/fl2va tier %r: why matches VID_Q_WHY" % t["id"], t["why"] == want,
          "got=%r want=%r" % (t["why"], want))

# RULING (owner, post-review): ref2v has NO speed pack wired in
# (h3_ref2va_graph never reads turbo_lora), so unlike fl2va it does not
# get VID_Q's three fast rows -- only the clean middle/good/best subset,
# same step counts and why text as fl2va's own middle/good/best.
ref2v_got = [t["values"]["steps"] for t in engines.quality("video", "ref2v")]
check("video/ref2v: step counts are the clean subset of VID_Q %s" % (vid_steps[-3:],),
      ref2v_got == vid_steps[-3:], str(ref2v_got))
check("video/ref2v: no tier requires turbo (no speed pack wired for this path)",
      not any(t.get("requires") for t in engines.quality("video", "ref2v")))
for t in engines.quality("video", "ref2v"):
    want = vid_why.get(str(t["values"]["steps"]))
    check("video/ref2v tier %r: why matches VID_Q_WHY" % t["id"], t["why"] == want,
          "got=%r want=%r" % (t["why"], want))

print()
print("falsifiability proof: this gate still fails on a real perturbation, then passes again on revert")
real_t2i_steps = [t["values"]["steps"] for t in engines.quality("image", "t2i")]
perturbed = real_t2i_steps[:-1] + [999]   # last tier's step count wrong
check("RED: a perturbed step count is caught against the golden", perturbed != img_steps,
      "perturbed=%s golden=%s" % (perturbed, img_steps))
check("GREEN: the real (unperturbed) pack values match the golden again", real_t2i_steps == img_steps,
      "real=%s golden=%s" % (real_t2i_steps, img_steps))

print()
print("a requires:turbo tier is unavailable on a lane fixture with no turbo LoRA, with a reason")
models_all = json.load(open(os.path.join(HERE, "golden", "models.json")))
models_no_turbo = dict(models_all); models_no_turbo["h3_turbo_lora"] = ""
able_no_turbo = engines.abilities(models_no_turbo)
able_with_turbo = engines.abilities(models_all)
check("RED (fixture check): turbo ability is false with h3_turbo_lora stripped", able_no_turbo["turbo"] is False)
check("GREEN (fixture check): turbo ability is true with the full fixture", able_with_turbo["turbo"] is True)
for mode in ("fl2va", "ref2v"):
    for t in engines.quality("video", mode):
        if t.get("requires") != "turbo":
            continue
        avail_no = bool(able_no_turbo.get("turbo"))
        avail_yes = bool(able_with_turbo.get("turbo"))
        check("video/%s tier %r: unavailable without the turbo LoRA" % (mode, t["id"]), avail_no is False)
        check("video/%s tier %r: available with the turbo LoRA" % (mode, t["id"]), avail_yes is True)
        check("video/%s tier %r: carries a plain-sentence reason" % (mode, t["id"]),
              bool(t.get("unavailable_reason")), repr(t.get("unavailable_reason")))

print()
print("R5: estimate_s is null under 3 finished jobs, the median at 3+ (synthetic job records)")
import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below
spec2 = importlib.util.spec_from_file_location("srv_for_estimate", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(srv)
LANE_ID, CAP, MODE, QID = "estimate-lane", "image", "t2i", "standard"
srv.JOBS.clear()


def add_job(elapsed, status="done"):
    jid = "j%d" % len(srv.JOBS)
    srv.JOBS[jid] = {"lane": LANE_ID, "kind": CAP, "mode": MODE, "quality": QID,
                     "status": status, "elapsed": elapsed}


check("RED: 0 finished jobs -> None", srv.estimate_seconds(LANE_ID, CAP, MODE, QID) is None)
add_job(10.0); add_job(20.0)
check("RED: 2 finished jobs -> still None", srv.estimate_seconds(LANE_ID, CAP, MODE, QID) is None)
add_job(30.0)
got = srv.estimate_seconds(LANE_ID, CAP, MODE, QID)
check("GREEN: 3 finished jobs -> median", got == 20.0, str(got))
add_job(999.0, status="queued")  # not "done" -- must not count
got = srv.estimate_seconds(LANE_ID, CAP, MODE, QID)
check("a queued job (not done) is excluded from the median", got == 20.0, str(got))
add_job(40.0)
got = srv.estimate_seconds(LANE_ID, CAP, MODE, QID)
check("even count -> average of the two middle values", got == 25.0, str(got))
srv.JOBS.clear()

print()
print("R6: /api/credits (engines.licences()) covers every pack; a dispatched job's licence is the right one")
lic = engines.licences()
check("licences() is non-empty", len(lic) > 0)
WANT_LICENCE = {
    ("audio", "song"):  "MIT",
    ("audio", "music"): "MiniMax-Music3 Community License",
    ("audio", "sfx"):   "Stability AI Community License",
    ("audio", "yue2"):  "CC BY-NC 4.0",
    ("audio", "cover"): "CC BY-NC 4.0",
    ("image", "t2i"):   "Qwen Research License",
    ("image", "edit"):  "Qwen Research License",
    ("video", "fl2va"): "MiniMax-H3 Model License",
    ("video", "ref2v"): "MiniMax-H3 Model License",
}
for (cap, mode), want in WANT_LICENCE.items():
    got = engines.licence_for(cap, mode)
    check("licence_for(%r, %r) == %r" % (cap, mode, want), (got or {}).get("name") == want, repr(got))
check("licence_for on an unknown mode is None (RED/GREEN: caught then clean)",
      engines.licence_for("audio", "no-such-mode") is None)

print()
print("no user-facing label/hint/note/why leaks ComfyUI jargon (a derived UI shows pack copy verbatim)")
BANNED_WORDS = ("cfg", "vae", "sampler", "scheduler", "latent", "checkpoint", "enum", "nvfp4", "int8", "lora")
JARGON = re.compile(r"\b(" + "|".join(BANNED_WORDS) + r")\b", re.I)


def jargon_hits(cap, mode, item, keys):
    return [k for k in keys if item.get(k) and JARGON.search(item[k])]


print()
print("falsifiability proof: one planted word is CAUGHT, then the real packs are clean")
planted = {"id": "x", "label": "X", "hint": "Uses the model's own VAE for this."}
check("RED: a planted 'VAE' in a hint is caught", jargon_hits("image", "t2i", planted, ("label", "hint")) == ["hint"])
clean = 0
for cap, mode in ALL_MODES_Q:
    for f in engines.fields(cap, mode):
        hits = jargon_hits(cap, mode, f, ("label", "hint"))
        check("%s/%s field %r: no jargon in label/hint" % (cap, mode, f["id"]), not hits, str(hits))
        clean += 0 if hits else 1
    for p in engines.presets(cap, mode):
        hits = jargon_hits(cap, mode, p, ("label", "note"))
        check("%s/%s preset %r: no jargon in label/note" % (cap, mode, p["id"]), not hits, str(hits))
    for t in engines.quality(cap, mode):
        hits = jargon_hits(cap, mode, t, ("label", "why"))
        check("%s/%s quality %r: no jargon in label/why" % (cap, mode, t["id"]), not hits, str(hits))
check("GREEN: the real packs have at least one field to check", clean > 0)

print()
print("no user-facing label/hint/note/why/mode_words names an engine, or writes a visible ' -- '")
print("(licence.attribution is EXCLUDED on purpose -- that field IS the credit, engine names belong there)")
# Built FROM the packs themselves, not hand-typed: each pack's own id, plus
# the vendor/model tokens a pack's prose might otherwise leak. A pack id
# that equals its own cap name (audio.py's id IS "audio") is excluded --
# that is the generic media-type word, legitimately used in ordinary prose
# ("the clip's audio"), not a vendor/model name; the multi-word pack ids
# (qwen-image, minimax-h3) are unambiguous and stay in.
ENGINE_TOKENS = {p["id"] for p in engines.packs() if p["id"] not in engines.caps()} | {
    "qwen", "minimax", "h3", "ace", "yue", "stable audio", "ltx", "trellis", "sdxl",
}
ENGINE_NAME = re.compile(r"\b(" + "|".join(re.escape(t) for t in ENGINE_TOKENS) + r")\b", re.I)
DASH_PAT = re.compile(r" -- ")


def engine_hits(item, keys):
    return [k for k in keys if item.get(k) and ENGINE_NAME.search(item[k])]


def dash_hits(item, keys):
    return [k for k in keys if item.get(k) and DASH_PAT.search(item[k])]


print()
print("falsifiability proof: a planted engine name and a planted ' -- ' are both caught")
planted = {"id": "x", "label": "X", "hint": "Powered by Qwen -- fast and clean."}
check("RED: a planted engine name is caught", engine_hits(planted, ("label", "hint")) == ["hint"])
check("RED: a planted ' -- ' is caught", dash_hits(planted, ("label", "hint")) == ["hint"])

for cap, mode in ALL_MODES_Q:
    for f in engines.fields(cap, mode):
        check("%s/%s field %r: no engine name in label/hint" % (cap, mode, f["id"]),
              not engine_hits(f, ("label", "hint")), str(engine_hits(f, ("label", "hint"))))
        check("%s/%s field %r: no ' -- ' in label/hint" % (cap, mode, f["id"]),
              not dash_hits(f, ("label", "hint")), str(dash_hits(f, ("label", "hint"))))
    for p in engines.presets(cap, mode):
        check("%s/%s preset %r: no engine name in label/note" % (cap, mode, p["id"]),
              not engine_hits(p, ("label", "note")), str(engine_hits(p, ("label", "note"))))
        check("%s/%s preset %r: no ' -- ' in label/note" % (cap, mode, p["id"]),
              not dash_hits(p, ("label", "note")), str(dash_hits(p, ("label", "note"))))
    for t in engines.quality(cap, mode):
        check("%s/%s quality %r: no engine name in label/why" % (cap, mode, t["id"]),
              not engine_hits(t, ("label", "why")), str(engine_hits(t, ("label", "why"))))
        check("%s/%s quality %r: no ' -- ' in label/why" % (cap, mode, t["id"]),
              not dash_hits(t, ("label", "why")), str(dash_hits(t, ("label", "why"))))
for cap in engines.caps():
    mw = engines.mode_words(cap)
    for mode, word in mw.items():
        check("%s mode_words %r: no engine name" % (cap, mode),
              not ENGINE_NAME.search(word), word)
        check("%s mode_words %r: no ' -- '" % (cap, mode),
              " -- " not in word, word)

print()
print("F1: available and missing agree, for EVERY cap/mode, via the one mode_ability() resolution")
full_models = dict(json.load(open(os.path.join(HERE, "golden", "models.json"))))
full_models.update(json.load(open(os.path.join(HERE, "golden", "audio_models.json"))))
full_models.update(json.load(open(os.path.join(HERE, "golden", "ltx_models.json"))))
# Process packs: the "file" is a program on PATH. The full fixture satisfies
# a bin role exactly the way a live lane's discovery dict would (server.py
# puts the resolved program path under the role name).
for _p in engines.packs():
    for _role in (_p.get("bins") or {}):
        full_models.setdefault(_role, sys.executable)
able_full = engines.abilities(full_models)
able_empty = engines.abilities({})


# R3-1 removed the last REAL (cap, mode) whose mode name differed from its
# resolved ability -- qwen-image's t2i/edit now have their own ability names,
# same as every other pack, so there is no real example left to demonstrate
# the pre-fix bug with. A synthetic pack (test_generic_dispatch.py's pattern)
# stands in: its mode is named "make", its ability "f1-widget-ability" --
# deliberately different, which is the one thing that matters here.
f1_fake = types.ModuleType("engines._f1_fake")
f1_fake.ENGINE = {
    "id": "zzz-f1-fake", "cap": "f1-widget",
    "roles": {"f1_unet": ("unet", {"all": ["f1widget"]})},
    "provides": {"f1-widget-ability": ["f1_unet"]},
    "words": {"f1_unet": "the f1 widget model"},
    "graphs": {"make": lambda p, m: {"1": {"class_type": "F1", "inputs": {"u": m["f1_unet"]}}}},
    "describe": lambda m: "",
    "licence": {"name": "TEST", "shippable": True, "attribution": "Test"},
}
sys.modules["engines._f1_fake"] = f1_fake
_f1_real_packs = engines.packs()
engines.packs.__globals__["_PACKS"] = sorted(_f1_real_packs + [f1_fake.ENGINE], key=lambda e: e["id"])
f1_models = {"f1_unet": "f1_widget.safetensors"}
f1_able = engines.abilities(f1_models)


def red_bug(cap, mode):
    """The pre-fix computation server.py used to make (F1): available/missing
    keyed by the raw mode name, not its resolved ability."""
    return bool(f1_able.get(mode)), ([] if f1_able.get(mode) else engines.missing_words(f1_models, mode))


red_avail, red_missing = red_bug("f1-widget", "make")
check("RED: the pre-fix line reports f1-widget/make unavailable on a lane that HAS it",
      red_avail is False and red_missing == [], "avail=%r missing=%r" % (red_avail, red_missing))
green_ability = engines.mode_ability("f1-widget", "make")
green_avail = bool(f1_able.get(green_ability))
# "make" is not itself a key in the synthetic pack's `provides` (its one
# ability is deliberately named "f1-widget-ability", not "make"), so
# mode_ability() falls back to the pack's CAP -- exactly what it did for
# image/t2i before R3-1 gave qwen-image its own "t2i" ability. That cap-
# level fallback is still the CORRECT resolution (able["f1-widget"] is
# true, since it is this cap's only ability); the raw mode name above is
# what was wrong.
check("GREEN: the real code resolves via mode_ability's cap fallback and reports it available",
      green_ability == "f1-widget" and green_avail is True,
      "ability=%r avail=%r" % (green_ability, green_avail))
engines.packs.__globals__["_PACKS"] = _f1_real_packs

for cap, mode in ALL_MODES_Q:
    ability = engines.mode_ability(cap, mode)
    avail = bool(able_full.get(ability))
    missing = [] if avail else engines.missing_words(full_models, ability)
    check("GREEN full fixture: %s/%s available (ability=%r)" % (cap, mode, ability), avail is True, str(missing))
    check("GREEN full fixture: %s/%s missing == []" % (cap, mode), missing == [], str(missing))

    avail0 = bool(able_empty.get(ability))
    missing0 = [] if avail0 else engines.missing_words({}, ability)
    check("GREEN empty fixture: %s/%s unavailable (ability=%r)" % (cap, mode, ability), avail0 is False)
    check("GREEN empty fixture: %s/%s missing != []" % (cap, mode), missing0 != [], str(missing0))

print()
print("every cap in /api/engines carries a non-empty cap_word and an int cap_order; image < video < audio")


def cap_violations(items):
    """items: list of (cap, word, order). Every contract violation, as strings."""
    out = []
    for cap, word, order in items:
        if not word:
            out.append("%s: empty cap_word" % cap)
        if not isinstance(order, int):
            out.append("%s: cap_order %r is not an int" % (cap, order))
    return out


print()
print("falsifiability proof: a broken cap_word/cap_order list is CAUGHT, then the real caps are clean")
broken = [("image", "", 1), ("video", "video", "two")]
violations = cap_violations(broken)
check("RED: an empty cap_word and a non-int cap_order are both caught", len(violations) == 2, str(violations))
real = [(cap, engines.cap_word(cap), engines.cap_order(cap)) for cap in engines.caps()]
violations = cap_violations(real)
check("GREEN: the real caps are clean", violations == [], str(violations))

order_map = {cap: engines.cap_order(cap) for cap in engines.caps()}
check("image sorts before video", order_map["image"] < order_map["video"], str(order_map))
check("video sorts before audio", order_map["video"] < order_map["audio"], str(order_map))

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
