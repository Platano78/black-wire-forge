"""Browser gate for removing shots in the Cutting Room: an empty shot gets a
remove × (which drops it with no confirmation), a shot with takes must be
deleted twice from the inspector ("Click again"), its take stays in History,
and a brand-new shot that was never touched is dropped when you leave the room
(or open another shot). The feature is being built in parallel, so most of
this FAILS right now by design -- the suite must still run to the end and
report one line per check, never a traceback.

Run (browser lock: only one Playwright suite at a time on this box):
  flock /models/scratch/bwf-personas/browser.lock timeout 600 python3 tests/test_slot_remove_ui.py
"""
import json
import os
import sys
import time
import urllib.request

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SEQ = "s_0000cccc"
NOW = time.time()
DIALOGS = []

# --- seed, BEFORE ui.start (SCRATCH exists at import time) ------------------
DATA = os.path.join(ui.SCRATCH, "data")
os.makedirs(os.path.join(DATA, "seq", SEQ, "takes"), exist_ok=True)
with open(os.path.join(DATA, "seq", SEQ, "takes", "pic1.png"), "wb") as f:
    f.write(ui.gradient_png(160, 90))

def _slot(sid, lane, cap, mode, values, takes, pick):
    return {"id": sid, "lane": lane, "beat_id": None, "cap": cap, "mode": mode,
            "recipe": None, "quality": None, "values": values, "refs": "auto",
            "takes": takes, "pick": pick, "trim": None, "title": None}

P1 = _slot("p1", "picture", "image", "t2i", {},
           [{"job_id": "pic1", "made": NOW, "beat_rev": None,
             "inputs": {"refs": [], "cables": {}}, "file": "takes/pic1.png"}],
           "pic1")
V1 = _slot("v1", "video", "video", "ltx", {"prompt": "x"}, [], None)
os.makedirs(os.path.join(DATA, "sequences"), exist_ok=True)
with open(os.path.join(DATA, "sequences", SEQ + ".json"), "w") as f:
    json.dump({"id": SEQ, "schema": 1, "rev": 1, "title": "Remove test", "mode": "sequence",
               "created": NOW, "updated": NOW, "canvas": {"width": 1024, "height": 576},
               "refs": [], "beats": [], "cables": [], "cuts": [], "slots": [P1, V1]}, f)

JOBS = [{"id": "pic1", "lane": "t", "lane_name": "Fake lane", "kind": "image", "mode": "t2i",
         "status": "done", "prompt": "a paper plane", "seed": 1, "created": NOW,
         "outputs": [{"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}]}]

URL = ui.start(JOBS)

# --- helpers: a missing element is ""/None, never a traceback --------------
def api(path):
    with urllib.request.urlopen(URL + path, timeout=5) as r:
        return json.loads(r.read())

def slot_ids():
    return [s["id"] for s in api("api/sequence?id=" + SEQ)["slots"]]

def wait_for(fn, secs):
    """Poll every 0.2 s; return the final bool (never raises)."""
    ok = False
    end = time.time() + secs
    while time.time() < end:
        try:
            ok = bool(fn())
        except Exception:
            ok = False
        if ok:
            return True
        time.sleep(0.2)
    try:
        ok = bool(fn())
    except Exception:
        ok = False
    return ok

def text(page, sel):
    try:
        return page.inner_text(sel)
    except Exception:
        return ""

def attr(page, sel, name):
    try:
        return page.eval_on_selector(sel, "el => el.getAttribute(%r)" % name) or None
    except Exception:
        return None

def has(page, sel):
    try:
        return page.query_selector(sel) is not None
    except Exception:
        return False

def click(page, sel, timeout=4000):
    try:
        page.click(sel, timeout=timeout)
        return True
    except Exception:
        return False

def pressed_slot(page):
    return attr(page, '#tlTrackVideo [aria-pressed="true"][data-slot-id]', "data-slot-id")

def add_video_slot(page):
    """Click '+ generate here' on the video track; return the new slot's id.
    The new slot is the pressed tile once it appears -- but a previously
    selected tile is pressed too, so the pressed id must be NEW to this click,
    not whatever was pressed a moment ago. None if no new slot appeared."""
    try:
        before = set(slot_ids())
    except Exception:
        before = set()
    click(page, '#tlTrackVideo [data-add-lane="video"]')
    def fresh():
        p = pressed_slot(page)
        return p if p is not None and p not in before else None
    wait_for(lambda: fresh() is not None, 5)
    return fresh()

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        page.on("dialog", lambda d: (DIALOGS.append(d.message), d.dismiss()))
        page.goto(URL + "#room=cutting&seq=" + SEQ, wait_until="networkidle")
        try:
            page.wait_for_selector('#tlTrackVideo [data-slot-id="v1"]', timeout=15000)
        except Exception:
            check("the sequence opened with the video slot on the timeline", False)

        # 1 -- the × is on the empty shot only
        check("an empty shot has a remove ×", has(page, '[data-slot-remove="v1"]'))
        check("a made shot has no ×", not has(page, '[data-slot-remove="p1"]'))

        # 2 -- × drops the empty shot, no confirmation
        click(page, '[data-slot-remove="v1"]')
        check("× removes the empty shot",
              wait_for(lambda: "v1" not in slot_ids() and not has(page, '[data-slot-id="v1"]'), 5))
        check("× asks nothing first", not DIALOGS, DIALOGS)
        ui.shot(page, "slotremove-2")

        # 3 -- '+ generate here' makes a shot; opening another shot drops it
        #      if you did nothing in it
        NEW1 = add_video_slot(page)
        check("(setup) + generate here made a shot",
              NEW1 is not None and NEW1 in slot_ids(), NEW1)
        click(page, '[data-slot-id="p1"]')
        check("an opened but untouched shot is not kept",
              NEW1 is not None and wait_for(lambda: NEW1 not in slot_ids(), 5), NEW1)

        # 4 -- typing into the new shot keeps it (and the text lands server-side)
        NEW2 = add_video_slot(page)
        try:
            page.fill("#promptBox", "a plane over the roofs")
        except Exception:
            pass
        page.wait_for_timeout(1500)
        click(page, '[data-slot-id="p1"]')
        page.wait_for_timeout(1000)
        kept, vals = False, ""
        try:
            slot = next((s for s in api("api/sequence?id=" + SEQ)["slots"] if s["id"] == NEW2), None)
            kept = slot is not None
            if kept:
                vals = slot.get("values") or {}
        except Exception:
            pass
        check("a shot you typed into is kept",
              NEW2 is not None and kept and any("a plane over the roofs" in str(v)
                                                for v in vals.values()),
              (NEW2, vals))

        # 5 -- deleting a shot with takes: confirm once more, take stays
        check("(setup) delete button shows for a picked shot", page.is_visible("#slotDeleteBtn"))
        click(page, "#slotDeleteBtn")
        label = text(page, "#slotDeleteBtn")
        try:
            still = "p1" in slot_ids()
        except Exception:
            still = False
        check("deleting a shot with takes asks once more",
              "Click again" in label and "1 take" in label and still, label)
        click(page, "#slotDeleteBtn")
        check("…then deletes it", wait_for(lambda: "p1" not in slot_ids(), 5))
        msg = text(page, "#seqMsg")
        check("…and says its takes stay in History", "History" in msg, msg)
        jobs, jerr, raw = [], "", ""
        try:
            raw = urllib.request.urlopen(URL + "api/jobs?limit=50", timeout=5).read().decode()
            jobs = json.loads(raw)["jobs"]
        except Exception as e:
            jerr = repr(e)
        check("the take itself is still in History", any(j.get("id") == "pic1" for j in jobs),
              (jerr, raw[:200]))
        ui.shot(page, "slotremove-5")

        # 6 -- a shot with no takes deletes at once, no second click
        click(page, '[data-slot-id="%s"]' % NEW2)
        click(page, "#slotDeleteBtn")
        check("a shot with no takes deletes at once",
              NEW2 is not None and wait_for(lambda: NEW2 not in slot_ids(), 5), NEW2)

        # 7 -- leaving the room drops a brand-new untouched shot
        NEW3 = add_video_slot(page)
        click(page, '#roomStrip [data-room-id="picture"]')
        try:
            page.wait_for_load_state("networkidle")
        except Exception:
            pass
        page.wait_for_timeout(1000)
        check("leaving the room drops an untouched new shot",
              NEW3 is not None and NEW3 not in slot_ids(), NEW3)
        ui.shot(page, "slotremove-7")

        # 8 -- the whole run asked nothing of the user
        check("no dialogs anywhere", not DIALOGS, DIALOGS)
        browser.close()
finally:
    ui.finish()
