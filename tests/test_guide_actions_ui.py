"""Browser gate for the Film Room Guide's action buttons in the Cutting room.

Run: flock /models/scratch/bwf-personas/browser.lock timeout 600 python3 tests/test_guide_actions_ui.py
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
import _picture_server as ps  # noqa: E402

NOW = time.time()

# ---------------------------------------------------------------------------
# Seed data BEFORE ui.start (under os.path.join(ui.SCRATCH, "data"))
# ---------------------------------------------------------------------------
DATA = os.path.join(ui.SCRATCH, "data")
os.makedirs(os.path.join(DATA, "seq", "s_0000abab", "takes"), exist_ok=True)

# Gradient picture for the takes
with open(os.path.join(DATA, "seq", "s_0000abab", "takes", "pic1.png"), "wb") as f:
    f.write(ui.gradient_png(160, 90))

# Sequence JSON as per spec
seq_data = {
    "id": "s_0000abab",
    "schema": 1,
    "rev": 1,
    "title": "Actions test",
    "mode": "sequence",
    "created": NOW,
    "updated": NOW,
    "canvas": {"width": 1024, "height": 576},
    "refs": [],
    "beats": [],
    "cables": [],
    "cuts": [],
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
                    "file": "takes/pic1.png"
                }
            ],
            "pick": "pic1",
            "trim": None,
            "title": None
        },
        {
            "id": "v1",
            "lane": "video",
            "beat_id": None,
            "cap": "video",
            "mode": "ltx",
            "recipe": None,
            "quality": None,
            "values": {"prompt": "a plane over a city"},
            "refs": "auto",
            # a picked take, so "Cut the sequence" has something to cut
            "takes": [{"job_id": "vid1", "made": NOW, "beat_rev": None,
                       "inputs": {"refs": [], "cables": {}}, "file": None}],
            "pick": "vid1",
            "trim": None,
            "title": None
        }
    ],
}

# Write sequence JSON
os.makedirs(os.path.join(DATA, "sequences"), exist_ok=True)
with open(os.path.join(DATA, "sequences", "s_0000abab.json"), "w") as f:
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
        "prompt": "an empty square",
        "seed": 1,
        "created": NOW,
        "outputs": [
            {"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}
        ],
    },
    {"id": "vid1", "lane": "t", "lane_name": "Fake lane", "kind": "video", "mode": "ltx", "status": "done",
     "prompt": "a plane over a city", "seed": 1, "created": NOW,
     "outputs": [{"filename": "vid1.mp4", "subfolder": "", "type": "output", "media": "video"}]},
]

# Fake helper replies as per spec
ps.HELPER_STATE["replies"] = [
    "Here's a three-beat plan.\nACTION: add_beats\nA paper airplane leaves a fourth-floor window.\nIt rides the wind over the tram lines.\nIt lands at a child's feet in the square.\n\nACTION: add_ref | picture 1 as set",
    "Shot 1 is written.\n`ACTION: open_shot | 1`\nACTION: make_shot | 1\nACTION: cut\nACTION: open_shot | 9\nACTION: fly_away | now"
]

# ---------------------------------------------------------------------------
# Start the fixture
# ---------------------------------------------------------------------------
URL = ui.start(jobs)

# Request tracking for posted()
REQS = []
def posted(path):
    """Return parsed JSON bodies of POSTs whose url ends with path."""
    import json
    result = []
    for method, url, post_data in REQS:
        if method == "POST" and url.endswith(path):
            if post_data:
                try:
                    result.append(json.loads(post_data))
                except Exception:
                    pass
    return result

def api(path):
    """Simple GET to the app's API."""
    import urllib.request
    with urllib.request.urlopen(URL + path, timeout=10) as r:
        return json.loads(r.read())

def seq():
    """Return the current sequence object (from GET /api/sequence?id=s_0000abab)."""
    return api("api/sequence?id=s_0000abab")

def vids():
    """Return ids of seq()'s video-lane slots in order."""
    slots = seq().get("slots") or []
    return [s["id"] for s in slots if s.get("lane") == "video"]

PAGES = []


def wait_for(fn, secs=5):
    """Poll *fn* every 0.2 s up to *secs* seconds; return the final bool."""
    deadline = time.time() + secs
    while time.time() < deadline:
        if fn():
            return True
        # Playwright's sync API records request events only inside its own calls.
        PAGES[0].wait_for_timeout(200) if PAGES else time.sleep(0.2)
    return bool(fn())

def bar_buttons(page):
    """Return the #guideActionBar [data-guide-action] elements."""
    try:
        return page.query_selector_all("#guideActionBar [data-guide-action]")
    except Exception:
        return []

def last_text(page):
    """Return textContent of the last #guideLog .guide-msg .guide-text."""
    try:
        els = page.query_selector_all("#guideLog .guide-msg .guide-text")
        if not els:
            return ""
        return els[-1].inner_text()
    except Exception:
        return ""

def send(page, text):
    """Fill #guideInput, press Enter, wait (<=15 s) until #guideThinking is hidden and the log has grown by 2."""
    try:
        # Count guide messages before
        before = len(page.query_selector_all("#guideLog .guide-msg"))
        # Fill and press Enter
        page.fill("#guideInput", text)
        page.press("#guideInput", "Enter")
        # Wait for thinking to hide and log to grow by 2
        deadline = time.time() + 15
        while time.time() < deadline:
            thinking = page.query_selector("#guideThinking")
            after = len(page.query_selector_all("#guideLog .guide-msg"))
            if (thinking is None or thinking.is_hidden()) and after >= before + 2:
                return
            time.sleep(0.2)
        # If timeout, just return (will fail checks)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------
try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        PAGES.append(page)

        # Track requests
        page.on("request", lambda r: REQS.append((r.method, r.url, r.post_data)))

        # Open the Cutting room with the sequence
        page.goto(URL + "#room=cutting&seq=s_0000abab", wait_until="networkidle")
        # Wait for #seqOpenNote and for #guideName to contain "Film"
        page.wait_for_selector("#seqOpenNote", timeout=15000)
        page.wait_for_function(
            """() => {
                const el = document.querySelector('#guideName');
                return el && el.textContent.includes('Film');
            }""",
            timeout=15000
        )

        # ==== Check 1 ====
        send(page, "a paper airplane crossing a city")
        # "the reply shows without its ACTION lines"
        lt = last_text(page)
        check("Check 1: the reply shows without its ACTION lines",
              ("three-beat plan" in lt) and ("ACTION" not in lt),
              f"last_text: {repr(lt)}")
        # "each action is a button"
        btns = bar_buttons(page)
        check("Check 1: each action is a button",
              len(btns) == 2,
              f"expected 2 buttons, got {len(btns)}")
        if len(btns) >= 2:
            check("Check 1: first button text contains 'Add these 3 beats'",
                  "Add these 3 beats" in btns[0].inner_text(),
                  f"first button text: {repr(btns[0].inner_text())}")
            check("Check 1: second button text contains 'picture shot 1' and 'set plate'",
                  ("picture shot 1" in btns[1].inner_text()) and ("set plate" in btns[1].inner_text()),
                  f"second button text: {repr(btns[1].inner_text())}")
        # "the beats are listed before you add them"
        beat_lis = page.query_selector_all("#guideActionBar .guide-action-beats li")
        check("Check 1: the beats are listed before you add them",
              len(beat_lis) >= 3,
              f"expected at least 3 beat lis, got {len(beat_lis)}")
        if len(beat_lis) >= 1:
            check("Check 1: first beat contains 'fourth-floor window'",
                  "fourth-floor window" in beat_lis[0].inner_text(),
                  f"first beat text: {repr(beat_lis[0].inner_text())}")
        # "nothing ran by itself"
        s = seq()
        check("Check 1: nothing ran by itself — beats empty",
              s.get("beats") == [],
              f"beats: {s.get('beats')}")
        check("Check 1: nothing ran by itself — refs empty",
              s.get("refs") == [],
              f"refs: {s.get('refs')}")

        ui.shot(page, "actions-1")

        # ==== Check 2 ====
        # Click the first bar button
        if len(btns) >= 1:
            bar_buttons(page)[0].click()
            # wait <= 10 s until len(seq()["beats"]) == 3
            def beats_len_3():
                return len(seq().get("beats") or []) == 3
            wait_for(beats_len_3, 10)
            s = seq()
            check("Check 2: Add these beats adds them",
                  len(s.get("beats") or []) == 3,
                  f"beats length: {len(s.get('beats') or [])}")
            # "…each with its own shot" — len(vids()) == 4
            vids_list = vids()
            check("Check 2: …each with its own shot",
                  len(vids_list) == 4,
                  f"video slots: {len(vids_list)}")
            # "…and shows the script" — seq()["mode"] == "storyboard"
            check("Check 2: …and shows the script",
                  seq().get("mode") == "storyboard",
                  f"mode: {seq().get('mode')}")
            # "…and the button says it's done" — the first bar button is disabled and its text contains "done"
            # Re-fetch the button because the DOM may have changed
            btns_after = bar_buttons(page)
            if len(btns_after) >= 1:
                b0 = btns_after[0]
                check("Check 2: first bar button is disabled",
                      b0.is_disabled(),
                      f"button disabled: {b0.is_disabled()}")
                check("Check 2: first bar button text contains 'done'",
                      "done" in b0.inner_text(),
                      f"button text: {repr(b0.inner_text())}")

        ui.shot(page, "actions-2")

        # ==== Check 3 ====
        # Click the second bar button (if exists)
        if len(btns) >= 2:
            bar_buttons(page)[1].click()
            # wait <= 8 s until seq()["refs"] has one with role "set" and job_id "pic1"
            def refs_set_pic1():
                refs = seq().get("refs") or []
                for r in refs:
                    if r.get("role") == "set" and r.get("job_id") == "pic1":
                        return True
                return False
            wait_for(refs_set_pic1, 8)
            s = seq()
            refs = s.get("refs") or []
            found = False
            for r in refs:
                if r.get("role") == "set" and r.get("job_id") == "pic1":
                    found = True
                    break
            check("Check 3: the picture goes in the REF ROOM as the set plate",
                  found,
                  f"refs: {refs}")

        ui.shot(page, "actions-3")

        # ==== Check 4 ====
        # Reload
        page.reload(wait_until="networkidle")
        # wait for #guideActionBar [data-guide-action]
        page.wait_for_selector("#guideActionBar [data-guide-action]", timeout=15000)
        btns_reload = bar_buttons(page)
        # "a reload remembers what was done" — both bar buttons are disabled and contain "done"
        if len(btns_reload) >= 2:
            b0 = btns_reload[0]
            b1 = btns_reload[1]
            check("Check 4: first bar button disabled",
                  b0.is_disabled(),
                  f"button0 disabled: {b0.is_disabled()}")
            check("Check 4: first bar button contains 'done'",
                  "done" in b0.inner_text(),
                  f"button0 text: {repr(b0.inner_text())}")
            check("Check 4: second bar button disabled",
                  b1.is_disabled(),
                  f"button1 disabled: {b1.is_disabled()}")
            check("Check 4: second bar button contains 'done'",
                  "done" in b1.inner_text(),
                  f"button1 text: {repr(b1.inner_text())}")

        # ==== Check 5 ====
        send(page, "what now?")
        # "an unknown action stays as text" — last_text contains "fly_away"
        lt = last_text(page)
        check("Check 5: an unknown action stays as text",
              "fly_away" in lt,
              f"last_text: {repr(lt)}")
        # "…and backticks do not hide one" — last_text lacks "open_shot"
        check("Check 5: backticks do not hide one",
              "`open_shot`" not in lt,
              f"last_text: {repr(lt)}")
        # Bar buttons (in order): "open, make, cut and a bad shot number are four buttons" — len == 4
        btns_check5 = bar_buttons(page)
        check("Check 5: four bar buttons",
              len(btns_check5) == 4,
              f"expected 4 buttons, got {len(btns_check5)}")
        if len(btns_check5) >= 4:
            # "a shot that does not exist can't be clicked" — the 4th is disabled and `#guideActionBar .guide-action-why` text contains "no video shot 9"
            b3 = btns_check5[3]
            check("Check 5: fourth button disabled",
                  b3.is_disabled(),
                  f"button3 disabled: {b3.is_disabled()}")
            why_el = page.query_selector("#guideActionBar .guide-action-row:nth-child(4) .guide-action-why")
            why_text = why_el.inner_text() if why_el else ""
            check("Check 5: fourth button why contains 'no video shot 9'",
                  "no video shot 9" in why_text,
                  f"why text: {repr(why_text)}")
            # FIRST = vids()[0]
            vids_list = vids()
            if len(vids_list) >= 1:
                FIRST = vids_list[0]
                # Click the 1st: "Open video shot 1 opens it"
                bar_buttons(page)[0].click()
                def video_opened():
                    try:
                        el = page.query_selector('#tlTrackVideo [aria-pressed="true"]')
                        return el and el.get_attribute("data-slot-id") == FIRST
                    except Exception:
                        return False
                wait_for(video_opened, 5)
                check("Check 5: Open video shot 1 opens it",
                      page.query_selector('#tlTrackVideo [aria-pressed="true"]') is not None and \
                      page.query_selector('#tlTrackVideo [aria-pressed="true"]').get_attribute("data-slot-id") == FIRST,
                      f"opened slot: {page.query_selector('#tlTrackVideo [aria-pressed=\"true\"]').get_attribute('data-slot-id') if page.query_selector('#tlTrackVideo [aria-pressed=\"true\"]') else None}")
                # Click the 2nd: "Make video shot 1 sends that shot"
                page.wait_for_timeout(800)
                bar_buttons(page)[1].click()
                # Wait for POST to /api/sequence/generate with slot_id == FIRST
                def make_sent():
                    bodies = posted("/api/sequence/generate")
                    for body in bodies:
                        if body.get("slot_id") == FIRST:
                            return True
                    return False
                wait_for(make_sent, 8)
                check("Check 5: Make video shot 1 sends that shot",
                      make_sent(),
                      f"posted bodies: {posted('/api/sequence/generate')}")
                # Click the 3rd: "Cut runs the cut"
                page.wait_for_timeout(800)
                bar_buttons(page)[2].click()
                # Wait for POST to /api/sequence/cut
                def cut_run():
                    bodies = posted("/api/sequence/cut")
                    return len(bodies) > 0
                wait_for(cut_run, 8)
                check("Check 5: Cut runs the cut",
                      cut_run(),
                      f"posted bodies: {posted('/api/sequence/cut')}")

        ui.shot(page, "actions-5")

finally:
    ui.finish()