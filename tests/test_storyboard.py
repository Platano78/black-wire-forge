"""Frozen acceptance gate for C3.5 -- storyboard beats
(the internal sequence/storyboard design spec Sections 1, 3, 7, 8 row C3.5).

Written AGAINST the frozen rules BEFORE the feature exists: server.py's SEQ_LATER_OPS
(around line 1147) still lists insert_beat/update_beat/delete_beat/import_script/
copy_beat_to_prompt, so seq_op() refuses every one of them with a fixed 400 sentence.
Every check below therefore drives the real op names through mod.seq_op() -- never a
raw-file shortcut for the feature itself -- so every rule-check is expected to FAIL on
the current tree (RED). A handful of pure sanity checks (pack discovery) are not rule
checks and may legitimately pass on HEAD; they are marked "sanity:" the same way
tests/test_cables.py marks its own.

DO NOT EDIT: tests/test_cut.py, tests/test_cut_ui.py, tests/fixtures/fake_comfy.py --
another agent owns those concurrently. This file and test_storyboard_ui.py are the
only two files this agent may create/touch.

ISOLATION: everything this test writes goes under a fresh tempfile.mkdtemp() directory;
GENCENTER_DATA/GENCENTER_CONFIG point there before server.py is ever imported, the same
way tests/test_sequences.py does it. No fake ComfyUI lane is needed -- every check here
is either pure sequence-store mechanics (engines.* is lane-independent, see engines/__init__.py
fields()/modes_for()/caps()) or a server-side writer (seq_add_take) that never contacts a lane.
The real data/ tree is never touched.

Run: python3 tests/test_storyboard.py
"""
import importlib.util, json, os, socket, sys, tempfile, time
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import engines

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port

SCRATCH = tempfile.mkdtemp(prefix="bwf_storyboard_")
print("scratch dir: %s (real data/ is never written)" % SCRATCH)

CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "c35 test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
           "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": free_port(),
                      "caps": ["image", "video", "audio"]}]}, open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG

DATA_DIR = os.path.join(SCRATCH, "data_main")


def load_server(data_dir, name):
    os.environ["GENCENTER_DATA"] = data_dir
    spec = importlib.util.spec_from_file_location("srv_storyboard_%s" % name, os.path.join(ROOT, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.JOBS_FILE = os.path.join(data_dir, "jobs.json")
    mod.SEQ_DIR = os.path.join(data_dir, "sequences")
    mod.SEQ_MEDIA_DIR = os.path.join(data_dir, "seq")
    mod.CHAIN_DIR = os.path.join(data_dir, "chain")
    mod.LOCAL_OUTPUTS_DIR = os.path.join(data_dir, "outputs")
    for d in (mod.SEQ_DIR, mod.SEQ_MEDIA_DIR, mod.CHAIN_DIR, mod.LOCAL_OUTPUTS_DIR):
        os.makedirs(d, exist_ok=True)
    return mod


mod = load_server(DATA_DIR, "main")


def op(sid, rev, name, **kw):
    return mod.seq_op(dict(kw, id=sid, rev=rev, op=name))


def get_beat(seq, bid):
    return next((b for b in seq.get("beats") or [] if b.get("id") == bid), None)


def get_slot(seq, sid_):
    return next((s for s in seq.get("slots") or [] if s.get("id") == sid_), None)


def find_prompt_field(cap, mode):
    """LOW-CONFIDENCE RULING (see delivered report): "the first textarea/prompt field
    of the slot's mode" is read as: the first field of type "textarea" in the pack's
    own field list, order as declared; if none, the first field whose id == "prompt"
    (covers audio "sfx", whose only prompt-shaped field is type "text")."""
    fs = engines.fields(cap, mode)
    ta = next((f for f in fs if f.get("type") == "textarea"), None)
    if ta:
        return ta
    return next((f for f in fs if f.get("id") == "prompt"), None)


def insert_beat(sid, rev, text, kind=None, at=None):
    kw = {"text": text}
    if kind is not None:
        kw["kind"] = kind
    if at is not None:
        kw["at"] = at
    return op(sid, rev, "insert_beat", **kw)


def update_beat(sid, rev, beat_id, text):
    return op(sid, rev, "update_beat", beat_id=beat_id, text=text)


def delete_beat(sid, rev, beat_id):
    return op(sid, rev, "delete_beat", beat_id=beat_id)


def import_script(sid, rev, text):
    return op(sid, rev, "import_script", text=text)


def copy_beat_to_prompt(sid, rev, beat_id):
    return op(sid, rev, "copy_beat_to_prompt", beat_id=beat_id)


KIND_LANE = {"film": "video", "picture": "picture", "sound": "sound"}
KIND_CAP = {"film": "video", "picture": "image", "sound": "audio"}
KIND_PREFIX = {"film": "v", "picture": "p", "sound": "s"}

check("sanity: an image mode declares a prompt-shaped field (picture beats need one to pre-fill)",
      any(find_prompt_field("image", m) for m in engines.modes_for("image")))
check("sanity: a video mode declares a prompt-shaped field (film beats need one to pre-fill)",
      any(find_prompt_field("video", m) for m in engines.modes_for("video")))
check("sanity: an audio mode declares a prompt-shaped field (sound beats need one to pre-fill)",
      any(find_prompt_field("audio", m) for m in engines.modes_for("audio")))


# ===========================================================================
# The TILL DELETE DO US PART beat sheet -- real text, not authored for this test.
#
# Source: an internal example film's nine shot files (shot0[1-9].txt), the
# `summary:` section of each of the 9 shot files, taken VERBATIM (including each
# file's own [text to video] / [reference generation] tag). shot01's summary is the
# only one describing an exterior ("A wide establishing exterior of a tiny illuminated
# habitat module... on the lunar surface"); shots 02-09 all describe <Subject 1> as
# the lunar apartment's interior per their own subject_definitions section. That is
# the one location change this beat sheet has, so it is bracketed with two sluglines
# I authored myself (the slugline/margin syntax this spec introduces did not exist in
# any prior document -- see the delivered report, section 3): one EXT. before shot01's
# paragraph, one INT. before shot02's.
#
# Counting: 9 shot0*.txt files -> 9 "film" beats (one paragraph each, in shot order).
# + 2 authored scene sluglines (EXT. before shot01, INT. before the shot02..09 block)
# = 11 beats total. Kinds, in paragraph order:
#   [scene, film, scene, film, film, film, film, film, film, film, film]
#   (2 scene, 9 film -- one film beat per shot0*.txt).
# ===========================================================================

TILL_DELETE_BEATS = [
    ("scene", "EXT. LUNAR HABITAT MODULE - NIGHT."),
    ("film", "[text to video] A wide establishing exterior of a tiny illuminated habitat "
             "module alone on the lunar surface beneath a black sky, with Earth enormous on "
             "the horizon. One continuous shot, no people, no dialogue."),
    ("scene", "INT. LUNAR APARTMENT - NIGHT."),
    ("film", "[reference generation] The target video pushes slowly into <Subject 1> as "
             "<Subject 2> sets two porcelain cups down on the steel table and offers tea to "
             "someone off screen. One continuous shot, one speaker."),
    ("film", "[reference generation] The target video holds a close-up on <Subject 2>, seated "
             "in <Subject 1>, as she refuses to look at someone off screen and tells them to "
             "stop using her dead husband's voice. One continuous shot, one speaker."),
    ("film", "[reference generation] The target video shows <Subject 3> kneeling beside "
             "<Subject 2> in <Subject 1> and asking her to confirm his own deletion. One "
             "continuous shot, two speakers, ending on her one-word answer."),
    ("film", "[reference generation] The target video opens on an extreme close-up of the two "
             "characters' hands touching on a steel table as a deletion process runs, then "
             "cuts twice into brief warm memory fragments and returns to the hands. Four "
             "shots, no dialogue."),
    ("film", "[reference generation] The target video holds on <Subject 3> as his deletion "
             "completes: he grips <Subject 2>'s hand tighter and tells her, with his voice "
             "failing, that he remembers her funeral. One continuous shot, one speaker."),
    ("film", "[reference generation] The target video holds on <Subject 2> as he straightens "
             "from kneeling, his glowing amber eyes turn flat sterile white, his posture "
             "becomes mechanical, and he announces that the resident profile has been "
             "removed. One continuous shot, one speaker."),
    ("film", "[reference generation] The target video holds on <Subject 2> as she recoils, a "
             "seam splits open across her own wrist to reveal machinery beneath the skin, and "
             "she whispers a question about her own identity. One continuous shot, one "
             "speaker."),
    ("film", "[reference generation] The final shot: a wide symmetrical frame of two machines "
             "seated opposite each other at the table with two untouched cups between them, "
             "as the android turns to the woman like a stranger and greets her as a tenant. "
             "One continuous shot, one speaker."),
]
TILL_DELETE_TEXT = "\n\n".join(text for _, text in TILL_DELETE_BEATS)
EXPECTED_KINDS = [k for k, _ in TILL_DELETE_BEATS]
check("sanity: the derived fixture has 11 beats (9 shot files + 2 authored sluglines)",
      len(TILL_DELETE_BEATS) == 11, len(TILL_DELETE_BEATS))
check("sanity: the derived fixture has exactly 2 scene beats and 9 film beats",
      EXPECTED_KINDS.count("scene") == 2 and EXPECTED_KINDS.count("film") == 9, EXPECTED_KINDS)


# ===========================================================================
print("\nRule 1: insert_beat creates a beat with a stable id; op bumps seq.rev by 1")

seq1, _ = mod.seq_create({"title": "Rule1", "mode": "storyboard"})
sid1, rev1 = seq1["id"], seq1["rev"]

before_rev = rev1
b, c = insert_beat(sid1, rev1, "A margin note with no slot.", kind="note")
check("Rule1: insert_beat -> 200", c == 200, b)
check("Rule1: rev increments by exactly 1", c == 200 and b.get("rev") == before_rev + 1,
      (b.get("rev"), before_rev))
new_beat1 = next((x for x in (b.get("beats") or []) if x.get("text") == "A margin note with no slot."), None) \
    if c == 200 else None
check("Rule1: the new beat has a non-empty string id",
      new_beat1 is not None and isinstance(new_beat1.get("id"), str) and new_beat1["id"], new_beat1)
rev1 = b.get("rev", rev1)

if new_beat1:
    b2, c2 = update_beat(sid1, rev1, new_beat1["id"], "A margin note, edited.")
    rev1 = b2.get("rev", rev1) if c2 == 200 else rev1
    still_there = get_beat(b2, new_beat1["id"]) if c2 == 200 else None
    check("Rule1: update_beat keeps the same beat id after an edit",
          c2 == 200 and still_there is not None and still_there.get("id") == new_beat1["id"], (c2, still_there))
else:
    check("Rule1: update_beat keeps the same beat id after an edit (skipped -- no beat from insert_beat)", False)


# ===========================================================================
print("\nRule 2: kind detection -- INT./EXT./INT.EXT. sluglines (case-sensitive, trimmed), else default 'film'")

seq2, _ = mod.seq_create({"title": "Rule2", "mode": "storyboard"})
sid2, rev2 = seq2["id"], seq2["rev"]

CASES = [
    ("INT. APARTMENT - NIGHT.", "scene", "INT. prefix"),
    ("EXT. STREET - DAY.", "scene", "EXT. prefix"),
    ("INT./EXT. CAR (MOVING) - CONTINUOUS.", "scene", "INT./EXT. combined prefix"),
    ("int. lowercase apartment - night.", "film", "lowercase int. is NOT a slugline (case-sensitive)"),
    ("   INT. LEADING WHITESPACE - NIGHT.", "scene", "leading whitespace is trimmed before the check"),
]
for text, expect_kind, label in CASES:
    b, c = insert_beat(sid2, rev2, text)
    ok = c == 200
    rev2 = b.get("rev", rev2) if ok else rev2
    # Looked up by STRIPPED text: the existing _text() validation helper (used by
    # every other text field on a sequence) already strips whitespace on the way
    # in, so a beat stored from "   INT. ..." legitimately comes back as "INT. ...".
    # That stripping is what makes "leading whitespace is trimmed before the
    # slugline check" true in the first place -- an exact-match lookup on the
    # UNtrimmed input would simply never find the beat.
    got = next((x for x in (b.get("beats") or []) if x.get("text") == text.strip()), None) if ok else None
    check("Rule2 (%s): kind == %r" % (label, expect_kind),
          ok and got is not None and got.get("kind") == expect_kind, (c, got))
    if ok and got is not None:
        if expect_kind == "scene":
            check("Rule2 (%s): a scene beat gets no slot (slot_id is null)" % label,
                  got.get("slot_id") is None, got)
        else:
            check("Rule2 (%s): a defaulted-film beat DOES get a slot" % label,
                  got.get("slot_id") is not None, got)


# ===========================================================================
print("\nRule 3: film/picture/sound each create exactly one slot in their own lane, linked both ways; note/scene get none")

for kind in ("film", "picture", "sound", "note"):
    seq3, _ = mod.seq_create({"title": "Rule3-%s" % kind, "mode": "storyboard"})
    sid3, rev3 = seq3["id"], seq3["rev"]
    before_slots = len(seq3.get("slots") or [])
    b, c = insert_beat(sid3, rev3, "A %s beat." % kind, kind=kind)
    ok = c == 200
    check("Rule3 (%s): insert_beat -> 200" % kind, ok, b)
    if not ok:
        continue
    after_slots = b.get("slots") or []
    new_beat = next((x for x in b["beats"] if x.get("text") == "A %s beat." % kind), None)
    check("Rule3 (%s): the beat's own kind is %r (not auto-detected as scene)" % (kind, kind),
          new_beat is not None and new_beat.get("kind") == kind, new_beat)
    if kind == "note":
        check("Rule3 (note): no slot created", len(after_slots) == before_slots, after_slots)
        check("Rule3 (note): beat.slot_id is null", new_beat is not None and new_beat.get("slot_id") is None, new_beat)
        continue
    check("Rule3 (%s): exactly one slot created" % kind, len(after_slots) == before_slots + 1, after_slots)
    new_slot = next((s for s in after_slots if s.get("id") not in
                      {s0.get("id") for s0 in (seq3.get("slots") or [])}), None)
    check("Rule3 (%s): the new slot is in the %r lane" % (kind, KIND_LANE[kind]),
          new_slot is not None and new_slot.get("lane") == KIND_LANE[kind], new_slot)
    check("Rule3 (%s): the new slot's cap is %r" % (kind, KIND_CAP[kind]),
          new_slot is not None and new_slot.get("cap") == KIND_CAP[kind], new_slot)
    check("Rule3 (%s): the new slot's id is prefixed %r, matching the schema example (v1/p.../s...)"
          % (kind, KIND_PREFIX[kind]),
          new_slot is not None and str(new_slot.get("id", "")).startswith(KIND_PREFIX[kind]), new_slot)
    check("Rule3 (%s): beat.slot_id == the new slot's id (linked forward)" % kind,
          new_beat is not None and new_slot is not None and new_beat.get("slot_id") == new_slot.get("id"),
          (new_beat, new_slot))
    check("Rule3 (%s): slot.beat_id == the beat's id (linked backward)" % kind,
          new_beat is not None and new_slot is not None and new_slot.get("beat_id") == new_beat.get("id"),
          (new_beat, new_slot))
    check("Rule3 (%s): the created slot's mode declares a prompt-shaped field, so it CAN pre-fill" % kind,
          new_slot is not None and find_prompt_field(new_slot.get("cap"), new_slot.get("mode")) is not None,
          new_slot)


# ===========================================================================
print("\nRule 4: the slot's prompt is pre-filled ONCE from the beat text at creation")

seq4, _ = mod.seq_create({"title": "Rule4", "mode": "storyboard"})
sid4, rev4 = seq4["id"], seq4["rev"]
BEAT4_TEXT = "An android pours tea for someone who is not there any more."
b, c = insert_beat(sid4, rev4, BEAT4_TEXT, kind="film")
ok4 = c == 200
rev4 = b.get("rev", rev4) if ok4 else rev4
beat4 = next((x for x in b.get("beats", [])), None) if ok4 else None
slot4 = get_slot(b, beat4["slot_id"]) if ok4 and beat4 else None
pf4 = find_prompt_field(slot4.get("cap"), slot4.get("mode")) if slot4 else None
check("Rule4: the resolved slot values carry the beat text under its mode's prompt field",
      ok4 and pf4 is not None and (slot4.get("values") or {}).get(pf4["id"]) == BEAT4_TEXT,
      (ok4, pf4, slot4.get("values") if slot4 else None))


# ===========================================================================
print("\nRule 5: update_beat bumps beat.rev, NEVER rewrites the slot prompt, and marks the picked take stale "
      "('script changed'); copy_beat_to_prompt is the only path that copies; a new take clears the stale flag")

seq5, _ = mod.seq_create({"title": "Rule5", "mode": "storyboard"})
sid5, rev5 = seq5["id"], seq5["rev"]
ORIG_TEXT = "The android sets down two cups."
b, c = insert_beat(sid5, rev5, ORIG_TEXT, kind="film")
ok5 = c == 200
rev5 = b.get("rev", rev5) if ok5 else rev5
beat5 = b.get("beats", [None])[0] if ok5 else None
BID5 = beat5["id"] if beat5 else None
SLOT5 = beat5["slot_id"] if beat5 else None
slot5_obj = get_slot(b, SLOT5) if ok5 else None
PF5 = find_prompt_field(slot5_obj.get("cap"), slot5_obj.get("mode")) if slot5_obj else None
check("Rule5 setup: beat + film slot created", ok5 and beat5 is not None and SLOT5 is not None, (ok5, beat5))

if ok5 and beat5 and PF5:
    BEAT_REV_AT_CREATION = beat5.get("rev")
    JOB5 = "storyboard_job5"
    with mod.JOBS_LOCK:
        mod.JOBS[JOB5] = {"id": JOB5, "lane": "t", "kind": slot5_obj.get("cap"), "mode": slot5_obj.get("mode"),
                          "status": "done", "outputs": []}
    mod.seq_add_take(sid5, SLOT5, JOB5, beat_rev=BEAT_REV_AT_CREATION)
    cur5 = mod.seq_get(sid5)[0]["rev"]
    bpk, cpk = op(sid5, cur5, "pick_take", slot_id=SLOT5, job_id=JOB5)
    rev5 = bpk.get("rev", cur5) if cpk == 200 else cur5
    check("Rule5 setup: pick_take on the fresh slot -> 200", cpk == 200, bpk)

    fetched_before_edit = mod.seq_get(sid5)[0]
    slot_before_edit = get_slot(fetched_before_edit, SLOT5)
    check("Rule5: freshly-picked take is NOT stale (beat_rev matches)",
          slot_before_edit is not None and slot_before_edit.get("stale") == [], slot_before_edit)

    NEW_TEXT = "The android sets down two cups and says nothing."
    b6, c6 = update_beat(sid5, rev5, BID5, NEW_TEXT)
    rev5 = b6.get("rev", rev5) if c6 == 200 else rev5
    beat5_after = get_beat(b6, BID5) if c6 == 200 else None
    check("Rule5: update_beat -> 200 and bumps beat.rev past its creation value",
          c6 == 200 and beat5_after is not None and beat5_after.get("rev") != BEAT_REV_AT_CREATION,
          (c6, beat5_after))

    slot_after_edit = get_slot(b6, SLOT5) if c6 == 200 else None
    check("Rule5: the slot's prompt is UNCHANGED by update_beat (still the ORIGINAL beat text)",
          c6 == 200 and slot_after_edit is not None
          and (slot_after_edit.get("values") or {}).get(PF5["id"]) == ORIG_TEXT,
          slot_after_edit.get("values") if slot_after_edit else None)
    check('Rule5: the slot is now stale with "script changed" (visible in one read, GET /api/sequence)',
          c6 == 200 and slot_after_edit is not None and "script changed" in (slot_after_edit.get("stale") or []),
          slot_after_edit.get("stale") if slot_after_edit else None)

    b7, c7 = op(sid5, rev5, "update_slot", slot_id=SLOT5, values={PF5["id"]: "A HAND-EDITED PROMPT"})
    rev5 = b7.get("rev", rev5) if c7 == 200 else rev5
    check("Rule5 setup: user hand-edits the prompt via update_slot -> 200", c7 == 200, b7)

    b8, c8 = copy_beat_to_prompt(sid5, rev5, BID5)
    rev5 = b8.get("rev", rev5) if c8 == 200 else rev5
    slot_after_copy = get_slot(b8, SLOT5) if c8 == 200 else None
    check("Rule5: copy_beat_to_prompt -> 200 and overwrites the prompt with the CURRENT beat text "
          "(even over a manual edit)",
          c8 == 200 and slot_after_copy is not None
          and (slot_after_copy.get("values") or {}).get(PF5["id"]) == NEW_TEXT,
          slot_after_copy.get("values") if slot_after_copy else None)
    check("Rule5: copying to the prompt alone does NOT clear staleness (no new take was made)",
          c8 == 200 and slot_after_copy is not None and "script changed" in (slot_after_copy.get("stale") or []),
          slot_after_copy.get("stale") if slot_after_copy else None)

    JOB5B = "storyboard_job5b"
    beat_rev_now = get_beat(b8, BID5).get("rev") if c8 == 200 else None
    with mod.JOBS_LOCK:
        mod.JOBS[JOB5B] = {"id": JOB5B, "lane": "t", "kind": slot5_obj.get("cap"), "mode": slot5_obj.get("mode"),
                           "status": "done", "outputs": []}
    mod.seq_add_take(sid5, SLOT5, JOB5B, beat_rev=beat_rev_now)
    cur5b = mod.seq_get(sid5)[0]["rev"]
    bpk2, cpk2 = op(sid5, cur5b, "pick_take", slot_id=SLOT5, job_id=JOB5B)
    slot_after_new_take = get_slot(bpk2, SLOT5) if cpk2 == 200 else None
    check("Rule5: a NEW take made after the edit clears the staleness",
          cpk2 == 200 and slot_after_new_take is not None and slot_after_new_take.get("stale") == [],
          slot_after_new_take.get("stale") if slot_after_new_take else None)
else:
    for label in ("freshly-picked take is NOT stale", "update_beat bumps rev", "prompt unchanged by update_beat",
                  '"script changed" stale', "copy_beat_to_prompt overwrites", "copy alone does not clear stale",
                  "new take clears stale"):
        check("Rule5: %s (skipped -- setup failed)" % label, False)


# ===========================================================================
print("\nRule 6: delete_beat leaves its slot in place, unlinked; the slot's takes and pick survive")

seq6, _ = mod.seq_create({"title": "Rule6", "mode": "storyboard"})
sid6, rev6 = seq6["id"], seq6["rev"]
b, c = insert_beat(sid6, rev6, "A beat about to be deleted.", kind="picture")
ok6 = c == 200
rev6 = b.get("rev", rev6) if ok6 else rev6
beat6 = b.get("beats", [None])[0] if ok6 else None
BID6 = beat6["id"] if beat6 else None
SLOT6 = beat6["slot_id"] if beat6 else None

if ok6 and beat6 and SLOT6:
    JOB6 = "storyboard_job6"
    with mod.JOBS_LOCK:
        mod.JOBS[JOB6] = {"id": JOB6, "lane": "t", "kind": "image", "mode": "t2i", "status": "done", "outputs": []}
    mod.seq_add_take(sid6, SLOT6, JOB6, beat_rev=beat6.get("rev"))
    cur6 = mod.seq_get(sid6)[0]["rev"]
    bpk, cpk = op(sid6, cur6, "pick_take", slot_id=SLOT6, job_id=JOB6)
    rev6 = bpk.get("rev", cur6) if cpk == 200 else cur6
    check("Rule6 setup: pick_take before deleting the beat -> 200", cpk == 200, bpk)

    b9, c9 = delete_beat(sid6, rev6, BID6)
    check("Rule6: delete_beat -> 200", c9 == 200, b9)
    if c9 == 200:
        check("Rule6: the beat is gone from beats[]", get_beat(b9, BID6) is None, b9.get("beats"))
        slot_after_delete = get_slot(b9, SLOT6)
        check("Rule6: the slot is STILL in slots[] (never deleted)", slot_after_delete is not None, b9.get("slots"))
        check("Rule6: the slot is now unlinked (beat_id is null)",
              slot_after_delete is not None and slot_after_delete.get("beat_id") is None, slot_after_delete)
        check("Rule6: the slot's take is NOT deleted",
              slot_after_delete is not None
              and any(t.get("job_id") == JOB6 for t in slot_after_delete.get("takes") or []),
              slot_after_delete.get("takes") if slot_after_delete else None)
        check("Rule6: the slot's pick is NOT cleared",
              slot_after_delete is not None and slot_after_delete.get("pick") == JOB6, slot_after_delete)
else:
    check("Rule6: delete_beat leaves an unlinked slot with its take/pick intact (skipped -- setup failed)", False)


# ===========================================================================
print("\nRule 7: import_script -- only into a zero-beat storyboard; splits on blank lines; N beats, correct kinds, "
      "a slot per film/picture/sound beat")

seq7, _ = mod.seq_create({"title": "Rule7-ok", "mode": "storyboard"})
sid7, rev7 = seq7["id"], seq7["rev"]
check("Rule7 setup: fresh storyboard starts with zero beats", (seq7.get("beats") or []) == [], seq7.get("beats"))

before_slots7 = len(seq7.get("slots") or [])
b, c = import_script(sid7, rev7, TILL_DELETE_TEXT)
check("Rule7: import_script into a zero-beat storyboard -> 200", c == 200, b)
if c == 200:
    got_kinds = [x.get("kind") for x in b.get("beats") or []]
    got_texts = [x.get("text") for x in b.get("beats") or []]
    check("Rule7: exactly %d beats were created" % len(TILL_DELETE_BEATS),
          len(b.get("beats") or []) == len(TILL_DELETE_BEATS), len(b.get("beats") or []))
    check("Rule7: the beat texts match the pasted paragraphs, in order",
          got_texts == [t for _, t in TILL_DELETE_BEATS], got_texts)
    check("Rule7: the beat kinds match (scene detected automatically, else 'film')",
          got_kinds == EXPECTED_KINDS, got_kinds)
    n_film = sum(1 for k in got_kinds if k == "film")
    slots_created = len(b.get("slots") or []) - before_slots7
    check("Rule7: exactly one slot was created per film beat (%d)" % n_film,
          slots_created == n_film, (slots_created, n_film))
    film_beats = [x for x in b.get("beats") or [] if x.get("kind") == "film"]
    check("Rule7: every film beat has a non-null slot_id, and every scene beat has none",
          all(x.get("slot_id") is not None for x in film_beats)
          and all(x.get("slot_id") is None for x in b.get("beats") or [] if x.get("kind") == "scene"),
          [(x.get("kind"), x.get("slot_id")) for x in b.get("beats") or []])
    rev7 = b.get("rev", rev7)
else:
    for label in ("N beats created", "texts match", "kinds match", "slot per film beat", "links"):
        check("Rule7: %s (skipped -- import_script refused)" % label, False)

# Blind-gate guard: import_script is ALSO in SEQ_LATER_OPS today, so a bare
# "-> 400" check here would false-PASS on HEAD even with zero beats present
# (it is unconditionally refused regardless of beat count, by the generic
# "not available yet" stub). This is gated on insert_beat's OWN setup having
# actually created a beat, and the error text must NOT be that generic stub
# sentence -- only the real "not empty" refusal counts.
LATER_OPS_STUB = "is not available yet; it arrives with a later part of sequences."
seq7b, _ = mod.seq_create({"title": "Rule7-refuse", "mode": "storyboard"})
sid7b, rev7b = seq7b["id"], seq7b["rev"]
b, c = insert_beat(sid7b, rev7b, "Already one beat here.", kind="note")
rev7b = b.get("rev", rev7b) if c == 200 else rev7b
beats_before_refused_import = len(b.get("beats") or []) if c == 200 else 0
check("Rule7-refuse setup: a real beat exists before the refusal is tested", c == 200 and beats_before_refused_import >= 1, b)

if c == 200:
    b2, c2 = import_script(sid7b, rev7b, TILL_DELETE_TEXT)
    check("Rule7: import_script into a storyboard that already has a beat is refused (400, a sentence, "
          "NOT the generic 'not available yet' stub)",
          c2 == 400 and isinstance(b2.get("error"), str) and b2.get("error")
          and LATER_OPS_STUB not in b2.get("error", ""), b2)
    check("Rule7: the refused import left the beat count unchanged",
          c2 == 400 and len((mod.seq_get(sid7b)[0].get("beats") or [])) == beats_before_refused_import,
          (beats_before_refused_import, mod.seq_get(sid7b)[0].get("beats")))
else:
    check("Rule7: import_script refused once a beat already exists (skipped -- setup failed)", False)
    check("Rule7: the refused import left the beat count unchanged (skipped -- setup failed)", False)


# ===========================================================================
print("\nRule 8: insert_beat 'at' places the beat by position; order survives a server restart")

seq8, _ = mod.seq_create({"title": "Rule8", "mode": "storyboard"})
sid8, rev8 = seq8["id"], seq8["rev"]
labels = ["first", "second", "third"]
for lab in labels:
    b, c = insert_beat(sid8, rev8, "Beat %s." % lab, kind="note")
    rev8 = b.get("rev", rev8) if c == 200 else rev8
seq_before_mid = mod.seq_get(sid8)[0]
ok8 = [x.get("text") for x in seq_before_mid.get("beats") or []] == ["Beat first.", "Beat second.", "Beat third."]
check("Rule8 setup: three appended beats are in insertion order", ok8, seq_before_mid.get("beats"))

if ok8:
    b, c = insert_beat(sid8, rev8, "Beat inserted-at-1.", kind="note", at=1)
    rev8 = b.get("rev", rev8) if c == 200 else rev8
    order_now = [x.get("text") for x in b.get("beats") or []] if c == 200 else None
    check("Rule8: 'at=1' places the new beat between the first and second existing ones",
          c == 200 and order_now == ["Beat first.", "Beat inserted-at-1.", "Beat second.", "Beat third."],
          order_now)

    mod2 = load_server(DATA_DIR, "restart8")
    after_restart = mod2.seq_get(sid8)[0]
    order_after_restart = [x.get("text") for x in after_restart.get("beats") or []]
    check("Rule8: beat order is byte-identical after reloading the module against the same data dir",
          order_after_restart == ["Beat first.", "Beat inserted-at-1.", "Beat second.", "Beat third."],
          order_after_restart)
else:
    check("Rule8: 'at' positions the beat correctly (skipped -- setup failed)", False)
    check("Rule8: order survives a restart (skipped -- setup failed)", False)


# ===========================================================================
print("\nRule 9: set_mode storyboard -> sequence keeps beats in the data (server-side; DOM absence is the UI gate)")

seq9, _ = mod.seq_create({"title": "Rule9", "mode": "storyboard"})
sid9, rev9 = seq9["id"], seq9["rev"]
b, c = insert_beat(sid9, rev9, "INT. KEPT ACROSS MODE SWITCH.", kind=None)
rev9 = b.get("rev", rev9) if c == 200 else rev9
b2, c2 = insert_beat(sid9, rev9, "A film beat kept across the mode switch.", kind="film")
rev9 = b2.get("rev", rev9) if c2 == 200 else rev9
beats_before_switch = (b2.get("beats") or []) if c2 == 200 else None
check("Rule9 setup: storyboard has 2 beats before the switch", c2 == 200 and len(beats_before_switch or []) == 2,
      beats_before_switch)

if c2 == 200:
    b3, c3 = op(sid9, rev9, "set_mode", mode="sequence")
    check("Rule9: set_mode -> sequence succeeds (this op already exists, not in SEQ_LATER_OPS)", c3 == 200, b3)
    check("Rule9: the beats array is untouched by the mode switch",
          c3 == 200 and b3.get("beats") == beats_before_switch, b3.get("beats") if c3 == 200 else None)
else:
    check("Rule9: beats survive set_mode storyboard->sequence (skipped -- setup failed)", False)


print("\n%s" % ("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED)))
sys.exit(1 if FAILED else 0)
