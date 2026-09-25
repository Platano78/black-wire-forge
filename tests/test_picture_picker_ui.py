"""Browser gate for the picture-picker UI: using a picture you already made
as a shot's picture.

Run: flock /models/scratch/bwf-personas/browser.lock timeout 600 python3 tests/test_picture_picker_ui.py
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
os.makedirs(os.path.join(DATA, "seq", "s_0000dddd", "takes"), exist_ok=True)
os.makedirs(os.path.join(DATA, "seq", "s_0000dddd", "refs"), exist_ok=True)

# Sequence directory tree
SEQ_DIR = os.path.join(DATA, "seq", "s_0000dddd")
os.makedirs(os.path.join(SEQ_DIR, "takes"), exist_ok=True)
os.makedirs(os.path.join(SEQ_DIR, "refs"), exist_ok=True)

with open(os.path.join(SEQ_DIR, "takes", "pic1.png"), "wb") as f:
    f.write(ui.gradient_png(160, 90))
with open(os.path.join(SEQ_DIR, "refs", "r1.png"), "wb") as f:
    f.write(ui.gradient_png(160, 90))

# Sequence JSON
seq_data = {
    "id": "s_0000dddd",
    "schema": 1,
    "rev": 1,
    "title": "Picker test",
    "mode": "sequence",
    "created": NOW,
    "updated": NOW,
    "canvas": {"width": 1024, "height": 576},
    "beats": [],
    "cables": [],
    "cuts": [],
    "refs": [
        {
            "id": "r1",
            "role": "set",
            "label": "Set plate",
            "job_id": "ref1",
            "output": 0,
            "file": "refs/r1.png",
            "recipe": None,
        }
    ],
    "slots": [
        {
            "id": "p1",
            "lane": "picture",
            "beat_id": None,
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
            "values": {"prompt": "a plane glides"},
            "refs": "auto",
            "takes": [],
            "pick": None,
            "trim": None,
            "title": None,
        },
    ],
}

# Write sequence JSON
with open(os.path.join(DATA, "sequences", "s_0000dddd.json"), "w") as f:
    json.dump(seq_data, f)

# Jobs
jobs = [
    {
        "id": "pic1",
        "lane": "t",
        "lane_name": "Fake lane",
        "kind": "image",
        "mode": "t2i",
        "status": "done",
        "seed": 1,
        "prompt": "picture shot one",
        "created": NOW - 30,
        "outputs": [
            {"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}
        ],
    },
    {
        "id": "ref1",
        "lane": "t",
        "lane_name": "Fake lane",
        "kind": "image",
        "mode": "t2i",
        "status": "done",
        "seed": 1,
        "prompt": "the set plate",
        "created": NOW - 20,
        "outputs": [
            {"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}
        ],
    },
    {
        "id": "hist1",
        "lane": "t",
        "lane_name": "Fake lane",
        "kind": "image",
        "mode": "t2i",
        "status": "done",
        "seed": 1,
        "prompt": "a history picture",
        "created": NOW - 10,
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
    return api("api/sequence?id=s_0000dddd")


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

        page.goto(URL + "#room=cutting&seq=s_0000dddd", wait_until="networkidle")
        page.wait_for_selector('[data-slot-id="v1"]', timeout=15000)
        page.click('[data-slot-id="v1"]')
        page.wait_for_selector("#roomForm", timeout=15000)
        time.sleep(1)

        # ---- Check 1: "a picture field offers pictures you made" ----
        pick_btn = page.query_selector("#inspector [data-pick-picture]")
        pick_btn_visible = pick_btn is not None
        fid = pick_btn.get_attribute("data-pick-picture") if pick_btn else ""
        check("a picture field offers pictures you made", pick_btn_visible, fid)

        # ---- Check 2: "the picker opens" ----
        if pick_btn:
            pick_btn.click()
            time.sleep(0.5)

        picker_id = "#picker_" + fid if fid else ""
        picker_visible = page.query_selector(picker_id) is not None if picker_id else False
        check("the picker opens", picker_visible)

        has_pic1 = page.query_selector(f"{picker_id} [data-picker-job=\"pic1\"]") is not None if picker_id else False
        check("it lists this film's picture", has_pic1)

        has_ref1 = page.query_selector(f"{picker_id} [data-picker-job=\"ref1\"]") is not None if picker_id else False
        check("…the REF ROOM", has_ref1)

        has_hist1 = page.query_selector(f"{picker_id} [data-picker-job=\"hist1\"]") is not None if picker_id else False
        check("…and History", has_hist1)

        for jid in ("pic1", "ref1", "hist1"):
            sel = f"{picker_id} [data-picker-job=\"{jid}\"] img"
            has_img = page.query_selector(sel) is not None
            check(f"every item has a thumbnail ({jid})", has_img)

        ui.shot(page, "picker-N-2")

        # ---- Check 3: picking pic1 loads the field ----
        if pick_btn and fid:
            pic1_item = page.query_selector(f"{picker_id} [data-picker-job=\"pic1\"]")
            if pic1_item:
                pic1_item.click()
                time.sleep(0.5)

                check(
                    "choosing it loads the field",
                    wait_for(
                        lambda: "1 loaded" in text_of(page, f'[data-ledger-row="{fid}"] .ref-tag'),
                        8,
                    ),
                )

                check(
                    "its thumbnail shows",
                    page.query_selector(f"#thumbs_{fid} img") is not None,
                )

                check(
                    "it is saved on the shot",
                    wait_for(lambda: bool(slot("v1") and slot("v1").get("values", {}).get(fid)), 8),
                )

                check(
                    "the picture went to the lane like an upload",
                    len(os.listdir(INPUTS)) > 0 if os.path.isdir(INPUTS) else False,
                )

        ui.shot(page, "picker-N-3")

        # ---- Check 4: reopening the shot keeps its picture ----
        page.click('[data-slot-id="p1"]')
        time.sleep(1)
        page.click('[data-slot-id="v1"]')
        time.sleep(1.5)

        check(
            "reopening the shot keeps its picture",
            "1 loaded" in text_of(page, f'[data-ledger-row="{fid}"] .ref-tag') if fid else False,
        )

        time.sleep(2)
        check(
            "…and the next save does not wipe it",
            bool(slot("v1") and slot("v1").get("values", {}).get(fid)) if fid else False,
        )

        # ---- Check 5: "the jacks have a one-line hint" ----
        check(
            "the jacks have a one-line hint",
            page.query_selector("#tlJackHint") is not None
            and "click" in text_of(page, "#tlJackHint"),
        )

        # ---- Check 6: Use as starting picture ----
        page.click('[data-slot-id="p1"]')
        time.sleep(1)

        use_btn_visible = page.query_selector("#useAsStartBtn") is not None
        check("a finished picture offers Use as starting picture", use_btn_visible)

        if use_btn_visible:
            before = [
                s["id"]
                for s in seq()["slots"]
                if s["lane"] == "video"
            ]

            page.click("#useAsStartBtn")

            _ctx = {"newv": None}

            def _check_new_video():
                after = [s["id"] for s in seq()["slots"] if s["lane"] == "video"]
                new_ids = set(after) - set(before)
                if new_ids:
                    for nid in sorted(new_ids):
                        slot_data = slot(nid)
                        if slot_data and slot_data.get("values"):
                            for k, v in slot_data["values"].items():
                                if k != "prompt" and v:
                                    _ctx["newv"] = nid
                                    return True
                return False

            if wait_for(_check_new_video, 8):
                NEWV = _ctx["newv"]
                check("Use as starting picture puts it into a video shot", True)

                # "…and opens that shot"
                tl_video = page.query_selector("#tlTrackVideo [aria-pressed='true']")
                opened_id = tl_video.get_attribute("data-slot-id") if tl_video else ""
                check("…and opens that shot", opened_id == NEWV)

                # "…and keeps it" — re-fetch after switching
                page.click('[data-slot-id="p1"]')
                time.sleep(1)
                # Check seq() still has NEWV
                after_seq = seq().get("slots", [])
                still_there = any(s.get("id") == NEWV for s in after_seq)
                check("…and keeps it", still_there)
            else:
                check("Use as starting picture puts it into a video shot", False)
                check("…and opens that shot", False)
                check("…and keeps it", False)

        ui.shot(page, "picker-N-6")

        # ---- Check 7: outside a sequence, no Use as starting picture ----
        page.goto(URL + "#room=picture", wait_until="networkidle")
        # Wait for bin to populate
        page.wait_for_selector('#binBody tr[data-job="hist1"]', timeout=15000)
        page.click('#binBody tr[data-job="hist1"]')
        time.sleep(1)

        use_btn_outside = page.query_selector("#useAsStartBtn")
        if use_btn_outside:
            hidden = use_btn_outside.get_attribute("hidden") is not None
            check("outside a sequence there is no Use as starting picture", hidden)
        else:
            check("outside a sequence there is no Use as starting picture", True)

        browser.close()

finally:
    ui.finish()
