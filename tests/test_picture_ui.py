"""Browser gate for P3d in the Picture room: "Edit this result" switches to
edit with the result as its first picture (carried onto the lane, sent on
Make), and the fixer's "Try it in Edit" does the same. BWF_P3D_SHOTS=<dir>
saves before/after screenshots at 1280x800 and 3440x1440.

Run: python3 tests/test_picture_ui.py
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

JOB = {"id": "fixjob1", "lane": "t", "lane_name": "Fake lane", "kind": "image", "mode": "t2i", "status": "done",
       "prompt": "a battle between Godzilla and MechaKing Ghidorah", "cfg": 3.0, "seed": 7, "created": time.time(),
       "outputs": [{"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}]}
URL = ui.start([JOB])
INPUTS = os.path.join(ui.SCRATCH, "lane", "inputs")
UPLOADED = "() => STATE.mode === 'edit' && (STATE.uploads.ref_images || []).length === 1"


def show_job(page):
    page.goto("about:blank")
    page.goto(URL + "#room=picture", wait_until="networkidle")
    page.wait_for_selector('#binBody tr[data-job="fixjob1"]', timeout=15000)
    page.click('#binBody tr[data-job="fixjob1"]')
    page.wait_for_selector("#monitorActions", timeout=15000)
    # the carry needs the lane up; the page's first lane poll may not have landed yet
    page.wait_for_function("() => STATE.lanes.some(l => l.id === 't' && l.up)", timeout=15000)


try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for w, h in ((1280, 800), (3440, 1440)):
            page = browser.new_page(viewport={"width": w, "height": h})
            sent = []
            page.on("request", lambda r: sent.append(json.loads(r.post_data or "{}"))
                    if r.url.endswith("/api/generate") and r.method == "POST" else None)
            show_job(page)
            page.wait_for_selector("#editResultBtn", timeout=15000)
            check("%d: Edit this result sits next to Use again" % w, page.evaluate(
                "() => document.querySelector('#useAgainBtn').nextElementSibling.id === 'editResultBtn'"))
            ui.shot(page, "edit-result-before-%dx%d" % (w, h))
            page.click("#editResultBtn")
            page.wait_for_function(UPLOADED, timeout=15000)
            name = page.evaluate("STATE.uploads.ref_images[0].name")
            check("%d: the result is on the lane as an input" % w, os.path.isfile(os.path.join(INPUTS, name)), name)
            check("%d: its thumb is the picture" % w, page.eval_on_selector_all("#thumbs_ref_images img", "e => e.length") == 1)
            check("%d: the form says it is picture 1" % w, "picture 1 under Pictures to work from"
                  in page.inner_text("#inspectorMsg"), page.inner_text("#inspectorMsg"))
            page.fill("#promptBox", "make the sky gold")
            page.locator("#thumbs_ref_images").scroll_into_view_if_needed()
            ui.shot(page, "edit-result-after-%dx%d" % (w, h))
            page.click("#makeBtn")
            page.wait_for_timeout(1500)
            check("%d: Make sends it as the edit's picture" % w, sent and sent[-1].get("mode") == "edit"
                  and name in json.dumps(sent[-1]), sent[-1:])
            page.close()

        print("Try it in Edit uses the same path")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        show_job(page)
        page.click("#notRightBtn")
        page.wait_for_selector("#guideChip:not([hidden])", timeout=15000)
        ui.ps.HELPER_STATE["replies"] = ["QUESTION:\nDIAGNOSIS: I can't see the picture, so going by what you say: "
                                         "no colour was given.\nFIX: edit\nPROMPT: Make the dragon gold.\nNOTE:\nTWEAK:"]
        page.fill("#guideInput", "the dragon should be gold")
        page.press("#guideInput", "Enter")
        page.wait_for_selector("#guideReviseEdit", timeout=15000)
        page.click("#guideReviseEdit")
        page.wait_for_function(UPLOADED, timeout=15000)
        check("Try it in Edit: the instruction is in the prompt", page.input_value("#promptBox") == "Make the dragon gold.")
        check("Try it in Edit: no 'add it yourself' line", "download it with Share" not in page.inner_text("#inspectorMsg"))
        page.close()
        browser.close()
finally:
    ui.finish()
