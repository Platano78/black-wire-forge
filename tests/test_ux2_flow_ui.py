"""Browser gate for UX-2 items 1 (engine chip), 2 (Lyrics under Style), 4
(explain the boxes), 6 (double-Make guard) and 9 (progress percentage) --
against the real page, the real Music/Video rooms and the real /api/engines
data (the fake lane's golden pools include the real audio role filenames, so
song/music/yue2 are genuinely "available", not stubbed). /api/generate and
/api/jobs are intercepted (test_ui_smoke.py's own pattern) so items 6/9 are
exercised with full control over job state, with no real dispatch needed.

Run: python3 tests/test_ux2_flow_ui.py
"""
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

URL = ui.start([])

# Mutable job state the /api/generate and /api/jobs route handlers share.
JOB = {"job": None}


def fake_generate(route, request):
    jid = "job-ux2-1"
    job = {"id": jid, "lane": "t", "lane_name": "Fake lane", "kind": "audio", "mode": "song",
           "status": "queued", "step": 0, "total": 0, "created": time.time(), "outputs": [],
           "progress": {"state": "loading", "stage": 0, "stages": 1, "percent": 0.0, "elapsed": 0.0}}
    JOB["job"] = job
    route.fulfill(status=200, content_type="application/json",
                 body='{"ok": true, "job": %s}' % __import__("json").dumps(job))


def fake_jobs(route, request):
    jobs = [JOB["job"]] if JOB["job"] else []
    route.fulfill(status=200, content_type="application/json",
                 body=__import__("json").dumps({"jobs": jobs, "log": [], "now": time.time()}))


def guard(page):
    page.route("**/api/generate", fake_generate)
    page.route("**/api/jobs*", fake_jobs)


def wait_text(page, selector, needle, timeout=6000):
    """True once `selector`'s innerText contains `needle`, polling (never a
    fixed sleep) up to `timeout` ms -- the client's own /api/jobs poll runs
    every 2000ms (index.html's setInterval(pollJobs, 2000)), so a fixed
    wait shorter than that races it. False (not an exception) on timeout,
    so the caller's check() reports it the same way as any other failure."""
    try:
        page.wait_for_function(
            "([sel, needle]) => (document.querySelector(sel) || {}).innerText?.includes(needle)",
            arg=[selector, needle], timeout=timeout)
        return True
    except Exception:
        return False


try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append("PAGEERROR: %s" % e))
        guard(page)

        print("Music room: empty state, item 1 chip, item 2 field order, item 4 explanation")
        page.goto(URL + "#room=music", wait_until="networkidle", timeout=30000)
        page.wait_for_selector("#engineChipBtn", state="visible", timeout=10000)
        # Discovery against the fake lane is async; the fixture's golden
        # pools DO satisfy song/music/yue2/cover, so wait for at least one
        # to actually report available rather than assume it's instant --
        # the picker legitimately auto-opens (by design) while the picked
        # engine still reads as unavailable, same as before this slice.
        page.wait_for_function(
            "() => { const r = document.querySelector('#enginePicker input[data-mode=\"song\"]'); "
            "return r && !r.disabled; }", timeout=15000)
        page.wait_for_function("() => typeof laneUp === 'function' && laneUp('t') === true", timeout=15000)
        page.evaluate("() => { if(typeof setEngineChipOpen === 'function') setEngineChipOpen(false); }")
        ui.shot(page, "ux2-01-music-empty")

        chip_text = page.inner_text("#enginePickerCurrent")
        check("item 1: the chip names the room ('Music') beside the engine label",
              chip_text.startswith("Music"), chip_text)
        caret_hidden = page.eval_on_selector("#engineChipCaret", "e => e.hidden")
        n_engines = len(page.query_selector_all("#enginePicker input[type=radio]"))
        if n_engines >= 2:
            check("item 1: 2+ engines -> the caret shows", caret_hidden is False)
            expanded_before = page.get_attribute("#engineChipBtn", "aria-expanded")
            check("item 1: popover starts closed (forced above, now the picked engine is available)",
                  expanded_before == "false", expanded_before)
            page.click("#engineChipBtn")
            check("item 1: clicking the chip opens the popover (aria-expanded)",
                  page.get_attribute("#engineChipBtn", "aria-expanded") == "true")
            check("item 1: the radio list is now visible", page.is_visible("#enginePicker"))
            # "song" is already the picked mode (checked above); pick a
            # DIFFERENT one so the radio's change event actually fires.
            other_radio = page.query_selector('#enginePicker input[data-mode="music"]') \
                or page.query_selector('#enginePicker input:not(:checked)')
            if other_radio and not other_radio.is_disabled():
                other_radio.check()
                page.wait_for_timeout(400)
                check("item 1: picking an engine closes the popover again",
                      page.get_attribute("#engineChipBtn", "aria-expanded") == "false")
                # Back to song: items 2/4/6/9 below assume song's fields.
                song_radio = page.query_selector('#enginePicker input[data-mode="song"]')
                if song_radio and not song_radio.is_disabled():
                    page.click("#engineChipBtn")
                    song_radio.check()
                    page.wait_for_timeout(300)
        else:
            check("item 1: a single-engine room's chip has no caret", caret_hidden is True)

        # FB-2: with a helper, song is a writer mode -- Style and Lyrics sit under
        # "What the guide filled in", closed while empty; open it to reach them by hand.
        page.evaluate("() => { const d = document.querySelector('#guideFilled'); if(d && !d.hidden) d.open = true; }")
        if page.is_visible("#f_lyrics"):
            check("item 2: Lyrics sits BEFORE Quality in the DOM (Content group, not several groups down)",
                  page.eval_on_selector(
                      "#f_lyrics",
                      "e => !!(e.compareDocumentPosition(document.querySelector('#qualityWrap')) "
                      "& Node.DOCUMENT_POSITION_FOLLOWING)"))
            empty_text = page.inner_text("#monitor")
            check("item 4: the empty-state Style/Lyrics explanation shows for a mode with both boxes",
                  "Lyrics are the words sung" in empty_text, empty_text[:200])

        print()
        print("item 6: a same-tab double press of Make shows the inline dupe notice; 'Make another' proceeds")
        if page.is_visible("#promptBox") and page.get_attribute("#promptBox", "data-field-id"):
            page.fill("#promptBox", "a test song about the sea")
            check("dupe notice starts hidden", page.is_hidden("#makeDupeNotice"))
            page.click("#makeBtn")
            page.wait_for_timeout(300)
            check("first Make: the job lands in History (no dupe notice yet)",
                  page.is_hidden("#makeDupeNotice"))
            page.click("#makeBtn")
            page.wait_for_timeout(200)
            check("item 6: a second press of Make with the SAME fields shows the inline notice",
                  page.is_visible("#makeDupeNotice"))
            notice_text = page.inner_text("#makeDupeNotice")
            check("the notice names what happened, in plain words",
                  "still rendering" in notice_text.lower(), notice_text)
            page.click("#makeDupeAnywayBtn")
            page.wait_for_timeout(200)
            check("item 6: 'Make another' proceeds -- the notice closes",
                  page.is_hidden("#makeDupeNotice"))

            print()
            print("item 9: the running job shows a percentage + stage label, not a raw step counter")
            JOB["job"]["progress"] = {"state": "sampling", "stage": 1, "stages": 2, "percent": 45.0, "elapsed": 12.0}
            JOB["job"]["status"] = "running"
            # WAIT for the condition (the client's own poll is every 2000ms
            # -- setInterval(pollJobs, 2000) -- so a fixed sleep shorter
            # than that races it), never a fixed sleep. Same assertion,
            # just proven at the moment it's actually true instead of
            # guessed at a moment it might not be yet.
            got_stage = wait_text(page, "#bin", "stage 1 of 2")
            bin_text = page.inner_text("#bin")
            check("item 9: History shows a stage + percent (\"stage 1 of 2\" / \"45%\"), never \"step N / M\" as the label",
                  got_stage and "45%" in bin_text, bin_text)
            check("item 9: a percentage bar element is actually rendered (not just text)",
                  page.query_selector(".progress-bar") is not None)
            page.click('#binBody tr[data-job="job-ux2-1"]')
            got_monitor = wait_text(page, "#monitor", "stage 1 of 2")
            monitor_text = page.inner_text("#monitor")
            check("item 9: the Monitor shows the same stage/percent for the selected running job",
                  got_monitor and "45%" in monitor_text, monitor_text)
            ui.shot(page, "ux2-02-music-running")

            JOB["job"]["progress"] = {"state": "loading", "stage": 0, "stages": 2, "percent": 0.0, "elapsed": 1.0}
            got_loading = wait_text(page, "#bin", "Loading models")
            bin_text2 = page.inner_text("#bin")
            check("item 9: no step data yet -> \"Loading models...\", never \"0%\" dressed up as real progress",
                  got_loading, bin_text2)
        else:
            check("song mode's prompt box is available on the fake lane (needed for items 6/9)", False)

        print()
        print("item 1: the Video room's chip (single- or multi-engine, whichever this room declares)")
        page.goto(URL + "#room=video", wait_until="networkidle", timeout=30000)
        page.wait_for_selector("#engineChipBtn", state="visible", timeout=10000)
        video_chip = page.inner_text("#enginePickerCurrent")
        check("item 1: the Video room's chip also names the room", video_chip.startswith("Video"), video_chip)
        ui.shot(page, "ux2-03-video-chip")

        check("no console/page errors across the whole run", errors == [], errors)
finally:
    ui.finish()
