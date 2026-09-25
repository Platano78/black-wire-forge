"""Browser gate for the set-plate notes: testing the warnings and actions in the cutting room.

Run: flock /models/scratch/bwf-personas/browser.lock timeout 600 python3 tests/test_set_plate_note_ui.py
"""
import json
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

NOW = time.time()

# ---------------------------------------------------------------------------
# Seed data BEFORE ui.start (under the scratch data dir that ui.start will use)
# ---------------------------------------------------------------------------
DATA = os.path.join(ui.SCRATCH, "data")
os.makedirs(os.path.join(DATA, "sequences"), exist_ok=True)
os.makedirs(os.path.join(DATA, "seq", "s_0000ffff", "takes"), exist_ok=True)

# Sequence directory tree
SEQ_DIR = os.path.join(DATA, "seq", "s_0000ffff")
os.makedirs(os.path.join(SEQ_DIR, "takes"), exist_ok=True)

# Write the take picture
with open(os.path.join(SEQ_DIR, "takes", "pic1.png"), "wb") as f:
    f.write(ui.gradient_png(160, 90))

# Sequence JSON
seq_data = {
    "id": "s_0000ffff",
    "schema": 1,
    "rev": 1,
    "title": "Set plate test",
    "mode": "sequence",
    "created": NOW,
    "updated": NOW,
    "canvas": {"width": 1024, "height": 576},
    "refs": [],
    "beats": [{"id": "b1", "kind": "picture", "text": "An empty rooftop at dawn.", "rev": 1, "slot_id": "p1"},
              {"id": "b2", "kind": "film", "text": "A plane lands on the rooftop.", "rev": 1, "slot_id": "v3"}],
    "cables": [],
    "cuts": [],
    "slots": [
        {
            "id": "p1",
            "lane": "picture",
            "beat_id": "b1",
            "cap": "image",
            "mode": "t2i",
            "recipe": None,
            "quality": None,
            "values": {},
            "refs": "auto",
            "takes": [
                {
                    "job_id": "pic1",
                    "made": NOW,
                    "beat_rev": None,
                    "inputs": {"refs": [], "cables": {}},
                    "file": "takes/pic1.png",
                }
            ],
            "pick": "pic1",
            "trim": None,
            "title": None,
        },
        {
            "id": "v1",
            "lane": "video",
            "beat_id": None,
            "cap": "video",
            "mode": "ltx",
            "recipe": None,
            "quality": None,
            "values": {"prompt": "a plane"},
            "refs": "auto",
            "takes": [],
            "pick": None,
            "trim": None,
            "title": None,
        },
        {
            "id": "v2",
            "lane": "video",
            "beat_id": None,
            "cap": "video",
            "mode": "ref2v",
            "recipe": None,
            "quality": None,
            "values": {"prompt": "a plane"},
            "refs": "auto",
            "takes": [],
            "pick": None,
            "trim": None,
            "title": None,
        },
        {"id": "s1", "lane": "sound", "beat_id": None, "cap": "audio", "mode": "song", "recipe": None, "quality": None,
         "values": {}, "refs": "auto", "takes": [{"job_id": "snd1", "made": NOW, "beat_rev": None,
         "inputs": {"refs": [], "cables": {}}, "file": None}], "pick": "snd1", "trim": None, "title": None},
        {"id": "v3", "lane": "video", "beat_id": "b2", "cap": "video", "mode": "ltx", "recipe": None, "quality": None,
         "values": {"prompt": "A plane lands on the rooftop."}, "refs": "auto", "takes": [], "pick": None,
         "trim": None, "title": None},
    ],
}

# Write sequence JSON
with open(os.path.join(DATA, "sequences", "s_0000ffff.json"), "w") as f:
    json.dump(seq_data, f)

# Jobs
jobs = [
    {"id": "snd1", "lane": "t", "lane_name": "Fake lane", "kind": "audio", "mode": "song", "status": "done",
     "prompt": "Soft guitar & glockenspiel. No vocals.", "seed": 1, "created": NOW,
     "outputs": [{"filename": "snd1.flac", "subfolder": "", "type": "output", "media": "audio"}]},
    {
        "id": "pic1",
        "lane": "t",
        "lane_name": "Fake lane",
        "kind": "image",
        "mode": "t2i",
        "status": "done",
        "prompt": "an empty rooftop",
        "seed": 1,
        "created": NOW,
        "outputs": [
            {"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}
        ],
    },
]

# ---------------------------------------------------------------------------
# Start the fixture
# ---------------------------------------------------------------------------
URL = ui.start(jobs)
INPUTS = os.path.join(ui.SCRATCH, "lane", "inputs")


def api(path):
    """Simple GET to the app's API."""
    import urllib.request
    with urllib.request.urlopen(URL + path, timeout=10) as r:
        return json.loads(r.read())


def seq():
    """Return the current sequence object (from GET /api/sequence)."""
    return api("api/sequence?id=s_0000ffff")


def slot(sid):
    """Return a single slot dict from the sequence, or None."""
    s = seq().get("slots") or []
    for x in s:
        if x.get("id") == sid:
            return x
    return None


def wait_for(fn, secs=5):
    """Poll *fn* every 0.2 s up to *secs* seconds; return the final bool."""
    deadline = time.time() + secs
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.2)
    return bool(fn())


def text_of(page, sel):
    """Return element text or "" if the selector is not found."""
    try:
        el = page.query_selector(sel)
        return el.inner_text() if el else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})

        # Navigate to the cutting room with our sequence
        page.goto(URL + "#room=cutting&seq=s_0000ffff", wait_until="networkidle")
        # Wait for v2 slot to be present (as per instructions)
        page.wait_for_selector('[data-slot-id="v2"]', timeout=15000)
        check("(setup) v2 sees the REF ROOM and v1 does not",
              (slot("v2") or {}).get("sees_refs") is True and (slot("v1") or {}).get("sees_refs") is False)

        # the music bed line: the prompt once, its own full stop kept, never a second one
        # and never escaped twice (walk-2 finding 3: "No vocals..")
        check("the music bed line reads the prompt as written",
              text_of(page, "#cutBedNote") == "Music bed: Soft guitar & glockenspiel. No vocals.",
              text_of(page, "#cutBedNote"))
        # ---- Check group 1: open_slot v1 ----
        page.click('[data-slot-id="v1"]')
        # Wait for the slot to settle (as per open_slot helper: wait 1.2 s)
        time.sleep(1.2)
        W = text_of(page, "#slotWarnings")
        # 1a: "the old unexplained sentence is gone" — "invent its own room" not in W.
        check("old unexplained sentence gone", "invent its own room" not in W)
        # 1b: "a shot with no picture to start from says it draws its place from words" — "words alone" in W.
        check("draws place from words", "words alone" in W)
        # 1c: picture shot 1 is not this shot's picture (no beat or cable ties them), so no
        # "Start it from picture shot 1" -- the note points at the picker instead (walk-2 finding 2).
        check("an unrelated picture is not offered as this shot's start",
              page.query_selector('[data-warn-action="start-from-picture"]') is None)
        check("...the note points at the picker instead", "Choose a picture you made" in W, W)
        ui.shot(page, "setplate-1")
        # 1d: v3's beat comes right after picture shot 1's beat: that picture is this shot's.
        page.click('[data-slot-id="v3"]')
        time.sleep(1.2)
        start_from_pic_btn = page.query_selector('[data-warn-action="start-from-picture"]')
        check("start-from-picture button visible", start_from_pic_btn is not None)
        if start_from_pic_btn:
            btn_text = start_from_pic_btn.inner_text()
            check("start-from-picture button text contains 'picture shot 1'", "picture shot 1" in btn_text)

        # ---- Check group 2: open_slot v2 ----
        page.click('[data-slot-id="v2"]')
        time.sleep(1.2)
        W = text_of(page, "#slotWarnings")
        # 2a: "no set plate: says what that means" — W contains "no set plate in the REF ROOM" and "draw its own version of the place".
        check("no set plate in REF ROOM mentioned", "no set plate in the REF ROOM" in W)
        check("draw its own version of the place", "draw its own version of the place" in W)
        # 2b: "...and offers picture shot 1 as the set plate" — [data-warn-action="set-plate"] visible and its text contains "picture shot 1".
        set_plate_btn = page.query_selector('[data-warn-action="set-plate"]')
        check("set-plate button visible", set_plate_btn is not None)
        if set_plate_btn:
            btn_text = set_plate_btn.inner_text()
            check("set-plate button text contains 'picture shot 1'", "picture shot 1" in btn_text)
            # Click it.
            set_plate_btn.click()
            # 2c: "the button makes picture shot 1 the set plate" — wait <= 8 s until seq()'s refs has one with role "set" and job_id "pic1".
            def refs_have_set_pic1():
                s = seq()
                refs = s.get("refs", [])
                for r in refs:
                    if r.get("role") == "set" and r.get("job_id") == "pic1":
                        return True
                return False
            check("button makes picture shot 1 the set plate", wait_for(refs_have_set_pic1, 8))
            # 2d: "...and the no-set note goes" — wait <= 5 s until #slotWarnings text lacks "no set plate".
            page.wait_for_timeout(100)  # Let UI update
            check("no-set note disappears", wait_for(lambda: "no set plate in the REF ROOM" not in text_of(page, "#slotWarnings"), 5))
            # 2e: "...and the REF ROOM shows it" — #refTrack .tl-ref-chip exists.
            check("REF ROOM shows set plate chip", page.query_selector("#refTrack .tl-ref-chip") is not None)
        ui.shot(page, "setplate-2")

        # ---- Check group 3: open_slot v1 again ----
        page.click('[data-slot-id="v1"]')
        time.sleep(1.2)
        W = text_of(page, "#slotWarnings")
        # 3a: "a set the recipe can't take: says so plainly" — W contains "can't take the REF ROOM" and "start it from the set plate".
        check("can't take REF ROOM mentioned", "can't take the REF ROOM" in W)
        check("start it from the set plate mentioned", "start it from the set plate" in W)
        # 3b: "...with a button to start from the set plate" — [data-warn-action="start-from-set"] visible.
        start_from_set_btn = page.query_selector('[data-warn-action="start-from-set"]')
        check("start-from-set button visible", start_from_set_btn is not None)
        if start_from_set_btn:
            # Click it.
            start_from_set_btn.click()
            # FID = the `field` of the first jack of type "image" in seq()'s v1 `jacks`.
            # We need to get the v1 slot and its jacks from the sequence.
            # GET /api/sequence derives each slot's jacks (its image/video inputs).
            FID = next((j["field"] for j in (slot("v1") or {}).get("jacks") or [] if j.get("type") == "image"), None)
            check("(setup) v1 has a starting-picture input", FID is not None)
            # Now wait for v1 values[FID] to be truthy (i.e., set to the set plate's job id or something truthy).
            def v1_has_image_set():
                s = slot("v1")
                if s and s.get("values"):
                    return bool(s["values"].get(FID))
                return False
            check("button starts the shot from the set plate", wait_for(v1_has_image_set, 8))
            # 3d: "once it starts from a picture, the note goes" — wait <= 5 s until #slotWarnings text lacks "can't take".
            page.wait_for_timeout(100)
            check("cannot take note disappears", wait_for(lambda: "can't take the REF ROOM" not in text_of(page, "#slotWarnings"), 5))
        ui.shot(page, "setplate-3")

        browser.close()

finally:
    ui.finish()