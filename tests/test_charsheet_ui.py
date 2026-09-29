"""Browser gate for CHARS-1: the Characters room renders with its fields in
order, a single engine chip (no caret), and its two-step empty line. The
guide fill (a fixture reply lands in the Sheet prompt box) is added below.
BWF_P3D_SHOTS=<dir> saves screenshots.

Run: python3 tests/test_charsheet_ui.py
"""
import json
import os
import struct
import sys
import zlib

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

URL = ui.start([])
SHEET = "\n".join(("%d. Section %d of the sheet for \"ROOKIE\": %s" % (n, n, "the rust-red scarf knots at the left of the neck. " * 12)).rstrip()
                  for n in range(1, 11))
PNG = os.path.join(ui.SCRATCH, "hero.png")
with open(PNG, "wb") as f:
    f.write(ui.gradient_png(96, 128))
EMPTY = ("Add a picture of your character and a name — the guide writes the sheet prompt, "
         "then Make draws the sheet.")

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(URL + "#room=characters", wait_until="networkidle")
        page.wait_for_function("() => STATE.lanes.some(l => l.id === 't' && l.up)", timeout=15000)
        made = []
        page.on("request", lambda r: made.append(r.url) if r.url.endswith("/api/generate") else None)
        page.reload(wait_until="networkidle")   # first paint predates the lane poll: reload for a settled form
        page.wait_for_selector("#promptBox", timeout=15000)
        page.wait_for_timeout(1000)
        check("the Characters room is selected", page.evaluate("() => currentRoom() && currentRoom().id") == "characters")
        check("its mode is the sheet", page.evaluate("() => STATE.mode") == "charsheet")
        rail = page.evaluate("() => Array.from(document.querySelectorAll('#roomList, #rooms, nav')).map(n => n.innerText).join('|')")
        check("Characters sits right after Picture in the room list",
              "Picture" in rail and "Characters" in rail and rail.index("Picture") < rail.index("Characters") < rail.index("Pixel Art"),
              rail)
        check("single-engine chip: named, no caret, disabled",
              page.evaluate("() => document.querySelector('#engineChipCaret').hidden") is True
              and page.get_attribute("#engineChipBtn", "disabled") is not None
              and "Characters" in page.inner_text("#enginePickerCurrent"), page.inner_text("#enginePickerCurrent"))
        check("the empty-state line explains the two steps", EMPTY in page.inner_text("#monitor") if page.query_selector("#monitor")
              else EMPTY in page.inner_text("body"), page.inner_text("body")[:400])
        text = page.inner_text("#inspector") + " " + page.inner_text("#promptLabel")
        check("the picture, name, sheet prompt and size are all on the form",
              all(w.lower() in text.lower() for w in ("Reference picture", "Name", "Sheet prompt", "Sheet size")), text[:600])
        pos = page.evaluate("""() => { const top = t => { const n = Array.from(document.querySelectorAll('#inspector *, #promptLabel'))
            .find(e => e.children.length === 0 && e.textContent.trim() === t); return n ? n.getBoundingClientRect().top : null; };
            return ['Reference picture', 'Name', 'Sheet prompt', 'Sheet size'].map(top); }""")
        check("fields top to bottom: picture, then prompt box and name, then size; all shown",
              all(p is not None for p in pos) and pos[0] < pos[3] and pos[2] < pos[3] and pos[1] < pos[3], pos)
        check("the size defaults to Balanced", page.evaluate(
            "() => document.querySelector('#inspector [data-field-id=size]').value") == "Balanced (3.4 MP)")
        check("the size options are the three tiers", page.evaluate(
            "() => Array.from(document.querySelector('#inspector [data-field-id=size]').options).map(o => o.value)")
              == ["Quick (1 MP)", "Balanced (3.4 MP)", "Large (6 MP)"])
        ui.shot(page, "chars-empty")

        print("the guide fills the Sheet prompt from the picture and the name")
        check("no picture yet: Help me write this is offered, Describe this picture is not",
              page.is_visible("#helperWriteBtn") and not page.is_visible("#helperDescribeBtn"))
        check("the caption says it writes the Sheet prompt", "Sheet prompt" in page.inner_text("#helperWriteCaption"),
              page.inner_text("#helperWriteCaption"))
        page.set_input_files("#upload_reference", PNG)
        page.wait_for_function("() => (STATE.uploads.reference || []).length === 1", timeout=15000)
        page.fill('#inspector [data-field-id="name"]', "Rookie")
        check("the box is empty when the guide is asked", page.input_value("#promptBox") == "")
        ui.ps.HELPER_STATE["requests"].clear()
        ui.ps.HELPER_STATE["replies"] = ["NOTE: read as a scout.\nPROMPT: " + SHEET]
        page.click("#helperWriteBtn")
        page.wait_for_selector("#guideSkillUse", timeout=20000)
        check("the writer got the name in its room line",
              'Name: \"Rookie\"' in ui.ps.HELPER_STATE["requests"][0]["messages"][1]["content"])
        check("nothing was sent to Make", made == [], made)
        check("the draft is shown before it is used, the box still empty", page.input_value("#promptBox") == "")
        page.click("#guideSkillUse")
        page.wait_for_function("() => document.querySelector('#promptBox').value.length > 200", timeout=15000)
        filled = page.input_value("#promptBox")
        check("Use these puts all ten sections in the Sheet prompt box",
              filled == SHEET and all(("\n%d. " % n) in ("\n" + filled) for n in range(1, 11)), filled[:80])
        page.locator("#promptBox").scroll_into_view_if_needed()
        check("...and still nothing was sent to Make", made == [], made)
        ui.shot(page, "chars-filled")
        page.close()
        browser.close()
finally:
    ui.finish()
