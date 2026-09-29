"""Browser gate for B2's client side: the guide conversation is pushed to the
server in the background after every local save, and read back from the
server on room entry -- but a server that is down (or the endpoint failing)
never breaks the guide: the page keeps working off localStorage alone,
exactly as it did before this landed.

Run: python3 tests/test_guide_history_ui.py
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
import _picture_server as ps  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

URL = ui.start([])
KEY = "bwf.guide.hist.picture"


def history():
    return json.loads(urllib.request.urlopen(URL + "api/guide/history?key=" + KEY, timeout=5).read())


def log_text(page):
    """All of #guideLog's messages, even ones the collapsed view hides visually
    (only the latest shows until "Show the whole conversation" is clicked)."""
    return " ".join(page.eval_on_selector_all("#guideLog .guide-msg", "els => els.map(e => e.textContent)"))


try:
    with sync_playwright() as pw:
        page = pw.chromium.launch().new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        print("round trip: a sent message reaches the server's own history file")
        # FB-2: t2i is a writer mode, so the box goes to its writer; SAY is the writer just talking.
        ps.HELPER_STATE["replies"][:] = ["SAY: Describe a picture and press Make."]
        page.goto(URL + "#room=picture", wait_until="networkidle")
        page.fill("#guideInput", "hello there")
        page.press("#guideInput", "Enter")
        page.wait_for_function("document.querySelectorAll('#guideLog .guide-msg').length >= 2", timeout=15000)
        deadline = time.time() + 5
        h = {"history": []}
        while time.time() < deadline and not h["history"]:
            time.sleep(0.2)
            h = history()
        check("the sent turn round-tripped through the server", any(m.get("content") == "hello there"
              for m in h["history"]), h)

        print("fallback: the history endpoint failing never breaks the guide")
        page.route("**/api/guide/history*", lambda r: r.abort())
        page.goto(URL + "#room=picture", wait_until="networkidle")
        page.wait_for_function("document.querySelectorAll('#guideLog .guide-msg').length >= 3", timeout=15000)
        check("the earlier conversation still shows, from localStorage",
              "hello there" in log_text(page), log_text(page))
        ps.HELPER_STATE["replies"][:] = ["SAY: Noted."]
        page.fill("#guideInput", "still works offline")
        page.press("#guideInput", "Enter")
        page.wait_for_function("document.querySelectorAll('#guideLog .guide-msg').length >= 4", timeout=15000)
        check("a new turn still saves locally and renders, with the endpoint down",
              "still works offline" in log_text(page), log_text(page))
        check("no console or page errors", errors == [], errors[:5])
finally:
    ui.finish()
