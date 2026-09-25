"""Browser gate for P3d in the 3D room: "Help me write this" on the picture
mode writes a source picture, its preview says it goes to the Picture room,
and "Use these" lands in Picture's text-to-picture mode with the prompt; the
turntable fixer's settings and new-picture fixes land where they belong.
BWF_P3D_SHOTS=<dir> saves the handoff at 1280x800 and 3440x1440.

Run: python3 tests/test_object_ui.py
"""
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check  # noqa: E402
from _picture_server import SRC  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

TJOB = {"id": "ttjob1", "lane": "cpu", "lane_name": "This machine", "kind": "3d", "mode": "turntable", "status": "done",
        "prompt": "", "args": {"frames": 48, "size": 384, "samples": 16}, "created": time.time(),
        "outputs": [{"filename": "poster.png", "subfolder": "ttjob1", "type": "local", "media": "image"}]}
URL = ui.start([TJOB], [("ttjob1", "poster.png", ui.gradient_png(160, 160))])
REPLIES = ui.ps.HELPER_STATE["replies"]
IN_PICTURE = "s => STATE.room === 'picture' && STATE.mode === 't2i' && document.querySelector('#promptBox').value === s"

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for w, h in ((1280, 800), (3440, 1440)):
            page = browser.new_page(viewport={"width": w, "height": h})
            page.goto(URL + "#room=3d", wait_until="networkidle")
            page.wait_for_function("() => STATE.mode === 'mesh' && !document.querySelector('#helperBar').hidden", timeout=15000)
            check("%d: the 3D picture mode shows a topic box, not a field" % w, page.is_visible("#promptBox")
                  and page.get_attribute("#promptBox", "data-field-id") is None
                  and page.get_attribute("#promptBox", "placeholder") == "What should the 3D model be of?")
            check("%d: its hint names the Picture room" % w, "Picture room" in page.inner_text("#promptHint"))
            page.fill("#promptBox", "a brass lantern")
            REPLIES[:] = ["NEGATIVE: hard shadows, reflections\nNOTE: The handle is thin.\nPROMPT: " + SRC]
            page.click("#helperWriteBtn")
            page.wait_for_selector("#guideSkillUse", timeout=15000)
            check("%d: the preview says it goes to the Picture room" % w,
                  "This goes to the Picture room" in page.text_content("#guideSkillTarget"), page.inner_text("#guideSkill"))
            check("%d: the preview shows the picture's own fields" % w, "Prompt" in page.inner_text("#guideSkill")
                  and "Things to avoid" in page.inner_text("#guideSkill"))
            page.locator("#guideSkill").scroll_into_view_if_needed()
            ui.shot(page, "handoff-preview-%dx%d" % (w, h))
            page.click("#guideSkillUse")
            page.wait_for_function(IN_PICTURE, arg=SRC, timeout=15000)
            check("%d: Use these lands in Picture / t2i with the prompt and what to avoid" % w,
                  page.input_value('#inspector [data-field-id="negative"]') == "hard shadows, reflections")
            ui.shot(page, "handoff-picture-%dx%d" % (w, h))
            page.close()

        print("the turntable fixer")
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        for reply, button, done in (
                ("DIAGNOSIS: too few frames.\nFIX: settings\nSETTINGS: frames = 120; samples = 64", "#guideReviseUseSettings",
                 "() => STATE.mode === 'turntable' && document.querySelector('[data-field-id=\"frames\"]').value === '120'"),
                ("DIAGNOSIS: the handle is fused.\nFIX: picture\nPROMPT: " + SRC, "#guideReviseTarget", None)):
            page.goto("about:blank")
            page.goto(URL + "#room=3d", wait_until="networkidle")
            page.wait_for_selector('#binBody tr[data-job="ttjob1"]', timeout=15000)
            page.click('#binBody tr[data-job="ttjob1"]')
            page.wait_for_selector("#notRightBtn", timeout=15000)
            page.click("#notRightBtn")
            page.wait_for_selector("#guideChip:not([hidden])", timeout=15000)
            REPLIES[:] = [reply]
            page.fill("#guideInput", "it's wrong")
            page.press("#guideInput", "Enter")
            page.wait_for_selector(button, timeout=15000)
            ui.shot(page, "turntable-fix-" + button.strip("#"))
            page.click(button)
            page.wait_for_function(done or IN_PICTURE, arg=None if done else SRC, timeout=15000)
            check("fixer: %s lands where it belongs" % button, True)
        browser.close()
finally:
    ui.finish()
