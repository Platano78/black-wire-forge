"""Acceptance gate for E1 (owner: "fix the error messages
after L5"): no raw Python exception text ever reaches the user as a job's
`error`.

For EVERY (cap, mode) engines.modes_for(...) declares, and every one of
that mode's own declared fields, this drives generate(p) (in-process, no
real ComfyUI lane -- dispatch()/dispatch_process() are stubbed) with the
field (a) missing, (b) set to "", (c) set to "not-a-number" (number/int
fields only), and (d) a select field set to an unknown option. Whenever the
result is an error, its message must: be non-empty; never look like a raw
Python exception; and, for the coercion cases (a/b/c -- the ones this
slice actually rewrites), name the field by its declared label.

RED/GREEN in one run: `git show HEAD:server.py` is loaded as the "old"
module (server.py before E1) exactly the way tests/test_slot_generate.py's
OLD_SERVER does it, and the working tree as "new" -- so this file proves
the bug on the code as it stood, then proves the fix, without a second
invocation.

ISOLATION: a scratch GENCENTER_DATA/GENCENTER_CONFIG (mkdtemp), set BEFORE
either server.py is imported -- the real data/ and config.json are never
opened. The one lane in the scratch config is never contacted: every case
here returns before generate() would reach dispatch()/dispatch_process(),
and both are stubbed on each loaded module as a second line of defense.

Run: python3 tests/test_error_messages.py
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


import engines

# ---------------------------------------------------------------------------
# Scratch env BEFORE either server.py is imported (test_slot_generate.py's
# / test_process_lane.py's pattern).
# ---------------------------------------------------------------------------
SCRATCH = tempfile.mkdtemp(prefix="bwf_e1_")
DATA = os.path.join(SCRATCH, "data")
os.makedirs(DATA)
CONFIG = os.path.join(SCRATCH, "config.json")

# One fake lane carrying every role every pack's `provides` names, so every
# (cap, mode)'s ability reads true and the coercion/graph_for path runs for
# all of them -- turntable's "roles" are the bare bin names "blender"/
# "ffmpeg" (engines/turntable.py has no `roles` dict; `provides` names them
# directly), everything else is a plain model filename.
MODELS = {
    "ace_unet": "a", "ace_clip1": "a", "ace_clip2": "a", "ace_vae": "a",
    "music3_unet": "a", "music3_clip": "a", "music3_vae": "a",
    "sao_ckpt": "a", "sao_clip": "a", "yue2_ckpt": "a", "yue2_audio_encoder": "a",
    "qwen_unet": "a", "qwen_clip": "a", "qwen_vae": "a",
    "birefnet_model": "a", "upscale_model": "a",
    "h3_unet_fl2va": "a", "h3_unet_ref2va": "a", "h3_clip_nvfp4": "a", "h3_clip_int8": "a",
    "h3_vae_video": "a", "h3_vae_audio": "a", "h3_turbo_lora": "a",
    "ltx_transformer": "a", "ltx_clip": "a", "ltx_vae_video": "a", "ltx_vae_audio": "a", "ltx_upscaler": "a",
    "trellis_unet": "a", "trellis_shape_vae": "a", "trellis_texture_vae": "a", "trellis_clip_vision": "a",
    "blender": "a", "ffmpeg": "a",
}

json.dump({"port": 0, "bind": "127.0.0.1", "title": "e1",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": 1,
                      "caps": ["image", "video", "audio", "3d"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = DATA


def load_server(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.LANE_BY_ID["t"]["models"] = dict(MODELS)
    with mod.STATE_LOCK:
        mod.LANE_STATE["t"] = {"up": True, "checked": 1.0, "err": ""}
    # Never actually reach a lane: any successful case would try to POST a
    # ComfyUI prompt or run a process job. This test only cares about the
    # REFUSED cases, but a field that turns out to have a harmless default
    # (case (a) on an optional field, say) legitimately succeeds -- stub
    # both dispatch paths so that "succeeds" means "returned ok", not "hung
    # trying to reach 127.0.0.1:1".
    mod.dispatch = lambda lane, graph, kind, mode, meta: ({"ok": True, "job": {"id": "fake"}}, 200)
    mod.dispatch_process = lambda lane, graph, kind, mode, meta, args: ({"ok": True, "job": {"id": "fake"}}, 200)
    return mod


# "The old code" means server.py before E1 -- which is HEAD only up until
# E1 itself is committed, at which point "HEAD:server.py" silently becomes
# E1's OWN fixed code and every RED assertion below stops reproducing
# anything (found running this file for E2: all three RED checks failed on
# an untouched checkout, before any E2 edit existed). A first attempt to fix
# this derived E1's commit as `git log -1 -- tests/test_error_messages.py`,
# assuming the file's own history pointed at its introducing commit -- but
# E2 (5d33f8a) ALSO touched this file, so that query silently started
# resolving to E2 instead, and E2~1 is E1 itself: already-fixed code, so
# every RED check failed again (caught by orchestrator review, C3.3).
# Pinned as a literal instead -- the actual commit that introduced this
# file's RED behaviour -- so it can never drift no matter which later
# commit next happens to touch this test file.
_E1_COMMIT = "671958b"
# A public checkout is a single squashed commit with no history before it --
# this commit (and the RED proof that needs it) exists only in the private
# repo. Check with `git cat-file -e` rather than trying `git show` and
# catching the failure: `check=True` on the show itself would still work,
# but git prints its own "fatal: invalid object name" to stderr either way,
# which is noise for a check this file already expects to skip cleanly.
OLD = None
have_old = subprocess.run(["git", "cat-file", "-e", "%s~1" % _E1_COMMIT], cwd=ROOT,
                           capture_output=True).returncode == 0
if have_old:
    OLD_SERVER_SRC = subprocess.run(["git", "show", "%s~1:server.py" % _E1_COMMIT], cwd=ROOT,
                                     capture_output=True, text=True, check=True).stdout
    OLD_SERVER_PATH = os.path.join(SCRATCH, "old_server.py")
    with open(OLD_SERVER_PATH, "w") as f:
        f.write(OLD_SERVER_SRC)
    OLD = load_server(OLD_SERVER_PATH, "srv_e1_old")
NEW = load_server(os.path.join(ROOT, "server.py"), "srv_e1_new")

BAD_RE = re.compile(
    r"invalid literal|could not convert|KeyError|Traceback|NoneType|"
    r"object has no attribute|^'[^']*'$|unsupported operand|list index")


def sample_value(f):
    """A plausible, in-range value for one declared field -- used to fill
    in every OTHER field of the request so only the field under test is
    broken."""
    t = f["type"]
    if t in ("text", "textarea"):
        return f.get("default") or "sample text"
    if t == "int":
        d = f.get("default")
        if d is not None:
            return d
        rng = f.get("range")
        return rng[0] if rng else 1
    if t == "number":
        d = f.get("default")
        if d is not None:
            return d
        rng = f.get("range")
        return rng[0] if rng else 1.0
    if t == "checkbox":
        return f.get("default", False)
    if t == "select":
        return f.get("default") or (f.get("options") or ["x"])[0]
    if t == "image":
        return "sample.png"
    if t == "image_list":
        return ["sample.png"]
    if t == "video_list":
        return ["sample.mp4"]
    if t == "audio":
        return "sample.wav"
    if t == "model":
        return "sample.glb"
    return "x"


# yue2/cover each declare their OWN field literally named "mode" (the
# full/melody selector), which collides with the dispatch-level "mode" key
# every flat request body already carries. E2 gave a reserved-colliding
# field a way to travel independently (nested under body["values"], never
# flattened -- server.py's RESERVED_FIELD_IDS / _field_request_value); this
# test's own full_body()/build_cases() build FLAT bodies only, the same
# shape a pre-E2 caller always used, so "mode" stays out of the matrix here
# -- it is exercised (missing/empty/bad-select alike, via the nested form)
# by test_generic_dispatch.py's dedicated yue2 "values" cases instead.
RESERVED_FIELD_IDS = {"mode"}


def full_body(cap, mode, fields):
    body = {"lane": "t", "kind": cap, "mode": mode}
    for f in fields:
        if f["id"] in RESERVED_FIELD_IDS:
            continue
        body[f["id"]] = sample_value(f)
    return body


ALL_MODES = [(cap, mode) for cap in engines.caps() for mode in engines.modes_for(cap)]
check("at least one (cap, mode) exists", bool(ALL_MODES))


def build_cases():
    """[(cap, mode, fid, label, ftype, case_name, body), ...] -- the whole
    matrix, built once and reused against both modules."""
    out = []
    for cap, mode in ALL_MODES:
        fields = engines.fields(cap, mode)
        for f in fields:
            fid, ftype = f["id"], f["type"]
            if fid in RESERVED_FIELD_IDS:
                continue
            label = f.get("label", fid)
            base = full_body(cap, mode, fields)
            missing = dict(base)
            missing.pop(fid, None)
            out.append((cap, mode, fid, label, ftype, "missing", missing))
            empty = dict(base)
            empty[fid] = ""
            out.append((cap, mode, fid, label, ftype, "empty", empty))
            if ftype in ("int", "number"):
                bad_num = dict(base)
                bad_num[fid] = "not-a-number"
                out.append((cap, mode, fid, label, ftype, "not-a-number", bad_num))
            if ftype == "select":
                bad_sel = dict(base)
                bad_sel[fid] = "zzz-not-an-option"
                out.append((cap, mode, fid, label, ftype, "bad-select", bad_sel))
    return out


CASES = build_cases()
check("at least one field case built", bool(CASES))


def run(mod, cases):
    """[(cap, mode, fid, label, ftype, case_name, result, code), ...] --
    generate() itself must never raise (an uncaught exception here is
    exactly the class of bug this slice fixes; caught and recorded as a
    result rather than crashing the whole gate)."""
    out = []
    for cap, mode, fid, label, ftype, case_name, body in cases:
        try:
            result, code = mod.generate(body)
        except Exception as e:
            result, code = {"ok": False, "error": "UNCAUGHT %r: %s" % (type(e), e)}, -1
        out.append((cap, mode, fid, label, ftype, case_name, result, code))
    return out


# ---------------------------------------------------------------------------
if OLD is None:
    print("RED: needs the private commit this file was written against, "
          "not included in the public repo")
    print("  SKIP  RED proof (the pre-fix code no longer exists to reproduce against)")
    # GREEN below keys its "already existed unchanged on OLD" exemption off
    # old_results; empty means every coercion case is held to the naming bar,
    # which is the stricter and correct default when there is no OLD to compare against.
    old_results = []
else:
    print("RED: the two owner-observed cases, reproduced on the code as it stood at HEAD")
    red_missing_tags, red_missing_code = OLD.generate(
        {"lane": "t", "kind": "audio", "mode": "song", "bpm": 138})
    check("RED: audio/song missing tags -> the bare-quoted-key Python text",
          red_missing_code == 400 and red_missing_tags.get("error") == "'tags'",
          repr((red_missing_code, red_missing_tags)))
    red_empty_bpm, red_empty_code = OLD.generate(
        {"lane": "t", "kind": "audio", "mode": "song", "tags": "upbeat pop", "bpm": ""})
    check("RED: audio/song bpm='' -> int()'s own exception text",
          red_empty_code == 400
          and red_empty_bpm.get("error") == "invalid literal for int() with base 10: ''",
          repr((red_empty_bpm, red_empty_code)))

    print()
    print("RED: across the whole matrix, the old code's error text matches the banned pattern somewhere")
    old_results = run(OLD, CASES)
    red_bad = [(cap, mode, fid, case_name, result.get("error"))
               for cap, mode, fid, label, ftype, case_name, result, code in old_results
               if code not in (200, -1) and BAD_RE.search(str(result.get("error") or ""))]
    check("RED: at least one raw-Python-text error exists on the old code",
          bool(red_bad), "none found -- the RED case itself may be broken")
    print("  (%d matching cases on the old code, e.g. %s)" % (len(red_bad), red_bad[:3]))

# ---------------------------------------------------------------------------
print()
print("GREEN: the same whole matrix against the working tree -- no raw Python text, fields named")
new_results = run(NEW, CASES)
# Keyed the same way, so a message E1 never touched (an existing, already-
# plain, core-authored sentence like "Tell it what you want first." for a
# missing prompt -- present on OLD too, unchanged) is not held to the
# "names the field" bar rule 1/2 only asks of the coercion messages this
# slice actually rewrites.
old_by_key = {(cap, mode, fid, case_name): (result.get("error") if isinstance(result, dict) else None)
              for cap, mode, fid, label, ftype, case_name, result, code in old_results}
if OLD is None:
    print("  SKIP  'names the field' bar needs OLD to know which messages E1 actually "
          "rewrote (a pre-existing, already-plain sentence like 'Tell it what you want "
          "first.' is deliberately exempt) -- not included in the public repo")
errored = 0
for cap, mode, fid, label, ftype, case_name, result, code in new_results:
    if code == 200:
        continue   # a harmless default (e.g. an optional field's "missing" case) -- nothing to check
    errored += 1
    err = result.get("error")
    tag = "%s/%s %s (%s)" % (cap, mode, fid, case_name)
    check("%s: not an uncaught exception" % tag, code != -1, repr(result))
    check("%s: error is non-empty" % tag, bool(err), repr(result))
    check("%s: error is not raw Python text" % tag,
          bool(err) and not BAD_RE.search(err), repr(err))
    if OLD is not None:
        old_err = old_by_key.get((cap, mode, fid, case_name))
        rewritten = old_err != err or (old_err and BAD_RE.search(old_err))
        if case_name in ("missing", "empty", "not-a-number") and err and rewritten:
            check("%s: error names the field ('%s' in the message)" % (tag, label),
                  label in err, repr(err))
    if case_name == "bad-select" and err:
        # E2: an unrecognised select value -> exactly "<Label> must be one
        # of A, B, C." (the same sentence shape as E1's coercion messages),
        # listing the field's own declared options -- never a pack's own,
        # differently-worded validation (turntable's "size must be
        # ..., got 'x'.") reached first, and never a plain pass-through.
        opts = next((f.get("options") or [] for f in engines.fields(cap, mode) if f["id"] == fid), [])
        expected = "%s must be one of %s." % (label, ", ".join(str(o) for o in opts))
        check("%s: error is the E2 bad-select sentence" % tag, err == expected, repr(err))
check("GREEN: at least one case actually errored (the gate has something to check)", errored > 0)

print()
print("both observed cases give exactly the new sentences")
new_missing_tags, new_missing_code = NEW.generate(
    {"lane": "t", "kind": "audio", "mode": "song", "bpm": 138})
check("audio/song missing tags -> 'Style / genre is needed for this.'",
      new_missing_code == 400 and new_missing_tags.get("error") == "Style / genre is needed for this.",
      repr((new_missing_code, new_missing_tags)))
new_empty_bpm, new_empty_code = NEW.generate(
    {"lane": "t", "kind": "audio", "mode": "song", "tags": "upbeat pop", "bpm": ""})
check("audio/song bpm='' -> 'BPM needs a whole number.'",
      new_empty_code == 400 and new_empty_bpm.get("error") == "BPM needs a whole number.",
      repr((new_empty_bpm, new_empty_code)))

print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), FAILED[:20]))
    sys.exit(1)
print("All E1 error-message checks passed.")
