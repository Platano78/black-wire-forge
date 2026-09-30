"""Browser walk of the Setup page (setup.html) at 1280 and 390 wide: a real server.py in
Setup mode (no config file), the fake ComfyUI and a FakeHelper with /v1/models. Each step:
"Step N of 5", focus on the step's heading, a label on every input, no sideways scroll;
Back works; step 1 finds the fake ComfyUI by address; step 2 (W2) shows the rooms as cards
with size, licence and a badge, a card opens by keyboard to per-file commands with the
<ComfyUI> placeholder, Copy puts the command on the clipboard, "not run by us" choices stay
folded, and Next works with no room chosen; step 3 tests the guide, step 4 shows the warning
for "other devices", step 5 shows the exact file, and "Save and start" lands in the app. No
page errors anywhere. SKIPs cleanly without Playwright/Chromium.

Set BWF_SETUP_SHOTS=<dir> to save a screenshot of every step at both widths.

Run: python3 tests/test_setup_ui.py
"""
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture  # noqa: E402,F401  (SKIPs cleanly without Playwright/Chromium)
import _setup_fixture as fx  # noqa: E402
from _setup_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = os.environ.get("BWF_SETUP_SHOTS")


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=True)


def text(page, sel):
    return page.locator(sel).text_content()


def step_ok(page, n, tag):
    check("%s: Step %d of 5 shown" % (tag, n), text(page, "#progress") == "Step %d of 5" % n, text(page, "#progress"))
    check("%s: focus is on step %d's heading" % (tag, n),
          page.evaluate("document.activeElement && document.activeElement.id") == "h%d" % n)
    unlabelled = page.evaluate("[...document.querySelectorAll('section:not([hidden]) input, section:not([hidden]) "
                               "select')].filter(e => !e.labels || !e.labels.length).map(e => e.id)")
    check("%s: step %d: every input has a label" % (tag, n), unlabelled == [], unlabelled)
    check("%s: step %d: no sideways scroll" % (tag, n),
          page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))


def rooms_step(page, tag):
    """W2's step 2: cards, a card opened by keyboard, Copy, folded alternatives."""
    page.wait_for_function("/Checked against your ComfyUI/.test(document.getElementById('r-status').textContent)",
                           timeout=30000)
    groups = page.evaluate("[...document.querySelectorAll('#r-list h2.group')].map(h => h.textContent)")
    check("%s: rooms grouped like rooms.json" % tag, groups == ["SOUND", "PICTURE", "MOTION", "OBJECT"], groups)
    card = page.locator("details.room[data-room=pixelart]")
    summary = card.locator(":scope > summary").text_content()
    check("%s: a card shows its total size, licence in plain words and a badge" % tag,
          "16.3 GB in all" in summary and "Qwen Research License: Non-commercial use only." in summary
          and ("Installed" in summary or "Needs " in summary), summary)
    check("%s: a free-to-use room says so" % tag,
          "MIT: Free to use commercially." in page.locator("details.room[data-room=cleanup] summary").text_content())
    video_sum = page.locator("details.room[data-room=video] > summary").text_content()
    check("%s: a revenue-threshold licence shows its own terms, not a blanket sentence" % tag,
          "below $10M a year in revenue" in video_sum and "research use" not in video_sum, video_sum)
    card.locator(":scope > summary").focus()
    page.keyboard.press("Enter")
    check("%s: Enter on a card's heading opens it" % tag, card.evaluate("d => d.open"))
    cmds = card.locator("code.cmd").all_text_contents()
    check("%s: each file has an hf download command with the <ComfyUI> placeholder" % tag,
          len(cmds) == 4 and all(c.startswith("hf download ") and ' --local-dir "<ComfyUI>/models/' in c
                                 for c in cmds), cmds)
    card.get_by_role("button", name="Copy").first.click()
    page.wait_for_function("document.querySelector('details.room[data-room=pixelart] .file .btn').textContent "
                           "=== 'Copied'", timeout=5000)
    check("%s: Copy puts the command on the clipboard" % tag,
          page.evaluate("navigator.clipboard.readText()") == cmds[0], cmds[0])
    video = page.locator("details.room[data-room=video]")
    video.locator(":scope > summary").click()
    check("%s: open cards fit: no sideways scroll" % tag,
          page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
    alt = video.locator("details.alt")
    check("%s: 'not run by us' choices are folded and labelled" % tag,
          alt.count() >= 1 and not alt.first.evaluate("d => d.open")
          and "Not run by us" in alt.first.text_content(), alt.count())
    check("%s: add-ons are links" % tag, video.locator(".body ul a[href^='https://github.com/']").count() >= 1)
    shot(page, "setup-%s-step2" % page.viewport_size["width"])
    page.evaluate("document.querySelectorAll('details.room').forEach(d => d.open = false)")


def walk(browser, width, height, lane_port, helper_port):
    tag = str(width)
    proc, cfg, logp = fx.boot("ui" + tag)
    up = fx.wait_health(proc, want_setup=True) is not None
    check("%s: Setup server up" % tag, up, open(logp).read()[-400:])
    if not up:
        return
    ctx = browser.new_context(viewport={"width": width, "height": height},
                              permissions=["clipboard-read", "clipboard-write"])
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(fx.BASE + "/")
    step_ok(page, 1, tag)
    check("%s: Back is hidden on step 1" % tag, not page.locator("#back").is_visible())
    page.get_by_role("button", name="Not yet").click()
    hrefs = page.evaluate("[...document.querySelectorAll('#c-notyet a')].map(a => a.href)")
    check("%s: Not yet links only to ComfyUI's own install pages" % tag,
          len(hrefs) == 3 and all(h.startswith(("https://comfy.org/", "https://docs.comfy.org/")) for h in hrefs)
          and page.get_by_role("button", name="Check again").is_visible(), hrefs)
    shot(page, "setup-%s-step1-notyet" % tag)
    page.get_by_role("button", name="No graphics card").click()
    page.wait_for_function("document.getElementById('t-ffmpeg').textContent !== ''", timeout=10000)
    check("%s: No graphics card says what Blender and ffmpeg look like here" % tag,
          text(page, "#t-blender") in ("found", "not found") and "turntable" in text(page, "#c-nogpu"))
    check("%s: Next stays off until something can make things" % tag, page.locator("#next").is_disabled())
    shot(page, "setup-%s-step1-nogpu" % tag)
    page.get_by_role("button", name="Yes, find it").click()
    page.wait_for_function("!/^Looking/.test(document.getElementById('c-status').textContent)", timeout=20000)
    page.get_by_label("Address", exact=True).fill("127.0.0.1")
    page.get_by_label("Port", exact=True).fill(str(lane_port))
    page.get_by_role("button", name="Check this address").click()
    page.wait_for_function("document.getElementById('c-gpu').textContent === 'fake gpu'", timeout=10000)
    check("%s: GPU, memory and version shown" % tag,
          text(page, "#c-vram") == "16 GB" and text(page, "#c-version") == "fake", text(page, "#c-found"))
    page.get_by_label("Its name in Black Wire Forge (you can change it)").fill("Studio")
    shot(page, "setup-%s-step1" % tag)
    page.get_by_role("button", name="Next").click()
    step_ok(page, 2, tag)
    rooms_step(page, tag)
    page.get_by_role("button", name="Back").click()
    check("%s: Back returns to step 1, answers kept" % tag,
          text(page, "#progress") == "Step 1 of 5" and page.locator("#c-name").input_value() == "Studio")
    page.get_by_role("button", name="Next").click()
    step_ok(page, 2, tag)
    check("%s: Next works on step 2 with no room chosen" % tag, page.locator("#next").is_enabled())
    page.get_by_role("button", name="Next").click()
    step_ok(page, 3, tag)
    page.wait_for_function("!/^Looking/.test(document.getElementById('g-status').textContent)", timeout=20000)
    page.get_by_label("Or type the guide's address").fill("http://127.0.0.1:%d" % helper_port)
    page.get_by_role("button", name="Check this address").click()
    page.wait_for_selector("#g-found:not([hidden])", timeout=10000)
    # A real guide already running on this machine (llama.cpp on :8080, say) is
    # found by step 2's own look-around first, so #g-found is visible before the
    # typed address's answer lands: wait for that answer, not for visibility.
    try:
        page.wait_for_function("document.getElementById('g-model').textContent.includes('fake-model')",
                               timeout=10000)
    except Exception:
        pass
    check("%s: the guide's model is listed" % tag, "fake-model" in text(page, "#g-model"))
    check("%s: Skip for now is offered" % tag, page.get_by_role("button", name="Skip for now").is_visible())
    fx.HELPER_STATE["replies"].append("ready")
    page.get_by_role("button", name="Test it").click()
    page.wait_for_function("/It works/.test(document.getElementById('g-result').textContent)", timeout=15000)
    check("%s: Test it shows the reply" % tag, "ready" in text(page, "#g-result"), text(page, "#g-result"))
    shot(page, "setup-%s-step3" % tag)
    page.get_by_role("button", name="Next").click()
    step_ok(page, 4, tag)
    check("%s: only this computer is the default" % tag, page.locator("#who-local").is_checked())
    page.get_by_label("Other devices on my home network").check()
    check("%s: the network choice shows the warning" % tag,
          page.locator("#who-warn").is_visible() and "no password" in text(page, "#who-warn"))
    shot(page, "setup-%s-step4-network" % tag)
    page.get_by_label("Only this computer").check()
    check("%s: back to this computer hides it" % tag, not page.locator("#who-warn").is_visible())
    page.get_by_role("button", name="Next").click()
    step_ok(page, 5, tag)
    page.wait_for_function("document.getElementById('f-text').textContent.length > 0", timeout=10000)
    shown = json.loads(text(page, "#f-text"))
    check("%s: step 5 shows the exact file" % tag,
          shown == {"bind": "127.0.0.1",
                    "lanes": [{"id": "comfy", "name": "Studio", "host": "127.0.0.1", "port": lane_port}],
                    "helper": {"url": "http://127.0.0.1:%d/v1" % helper_port, "model": "fake-model"}}, shown)
    shot(page, "setup-%s-step5" % tag)
    with page.expect_navigation(timeout=30000):
        page.get_by_role("button", name="Save and start").click()
    page.wait_for_load_state("load")
    check("%s: Save and start lands in the app" % tag,
          page.title() == "Black Wire Forge" and page.locator("#progress").count() == 0, page.title())
    check("%s: the server is configured now" % tag, fx.http("GET", "/api/health")[1].get("setup") is False)
    check("%s: config.json written" % tag, os.path.exists(cfg) and json.load(open(cfg)) == shown)
    page.wait_for_timeout(1500)
    shot(page, "setup-%s-done" % tag)
    check("%s: no page errors" % tag, errors == [], errors)
    ctx.close()
    fx.stop(proc)


try:
    if not fx.port_free(fx.SETUP_PORT):
        check("port %d is free for the Setup server" % fx.SETUP_PORT, False, "something else is listening on it")
        fx.finish()
    lane_port, helper_port = fx.start_fakes()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            walk(browser, 1280, 800, lane_port, helper_port)
            walk(browser, 390, 844, lane_port, helper_port)
        finally:
            browser.close()
finally:
    fx.finish()
