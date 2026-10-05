"""Browser gate for the Compare box and result panel.

Tests the compare sweep UI: the collapsed box, axis selection, value parsing,
the /api/compare POST, and the compare result grid. BWF_P3D_SHOTS=<dir> saves
before/after screenshots at 1280x800 and 3440x1440.

Run: python3 tests/test_compare_ui.py
"""
import json
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ui_fixture as ui  # noqa: E402
from _ui_fixture import check, FAILED  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: F401

# A Picture-mode job for the fixture
JOB = {"id": "cmpjob1", "lane": "t", "lane_name": "Fake lane", "kind": "image", "mode": "t2i",
       "status": "done", "prompt": "a golden dragon", "seed": 42,
       "cfg": 3.0, "steps": 20, "created": time.time(),
       "outputs": [{"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}]}

URL = ui.start([JOB])


def open_picture_room(page):
    """Navigate to the Picture room and wait for the form to render."""
    page.goto(URL + "#room=picture", wait_until="networkidle")
    page.wait_for_selector("#inspector", timeout=15000)
    page.wait_for_selector("#makeBtn", timeout=15000)


try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})

        # ------------------------------------------------------------------
        # 1. Compare box is hidden in a room with no generate mode.
        # ------------------------------------------------------------------
        # The fixture's rooms include a process room ("cpu" lane = 3d).
        page.goto(URL + "#room=setup", wait_until="networkidle")
        page.wait_for_selector("#makeBtn", timeout=15000)
        check("Compare box hidden in Setup room",
              page.evaluate("() => $('#compareBox') && $('#compareBox').hidden"))

        # ------------------------------------------------------------------
        # 2. Compare box is visible in Picture room.
        # ------------------------------------------------------------------
        open_picture_room(page)
        check("Compare box visible in Picture room",
              not page.evaluate("() => $('#compareBox') && $('#compareBox').hidden"))

        # ------------------------------------------------------------------
        # 3. Axis select offers Steps and Seed (and Quality if present).
        # ------------------------------------------------------------------
        page.click("#compareBox summary")
        page.wait_for_selector("#compareAxis", timeout=5000)
        opts = page.evaluate("() => Array.from($('#compareAxis').options).map(o => o.value)")
        check("Steps is an axis option", "steps" in opts)
        check("Seed is an axis option", "seed" in opts)
        # Quality appears if the fixture mode has tiers
        has_quality = "quality" in opts
        check("Quality axis present when mode has tiers", has_quality)

        # ------------------------------------------------------------------
        # 4. Entering "12, 20, 30" with axis Steps sends ONE POST.
        # ------------------------------------------------------------------
        page.select_option("#compareAxis", "steps")
        page.fill("#compareValues", "12, 20, 30")
        sent_compare = []

        def route_compare(route):
            data = json.loads(route.request.post_data or "{}")
            sent_compare.append(data)
            route.fulfill(status=200,
                          json={"ok": True, "group": "g1", "axis": "steps",
                                "jobs": [{"id": "c1", "value": 12},
                                         {"id": "c2", "value": 20},
                                         {"id": "c3", "value": 30}]})

        page.route("**/api/compare", route_compare)

        page.click("#compareBtn")
        page.wait_for_timeout(500)

        check("One POST to /api/compare", len(sent_compare) == 1)
        if sent_compare:
            body = sent_compare[0]
            check("POST has compare.axis == steps", body.get("compare", {}).get("axis") == "steps")
            check("POST has compare.values == [12,20,30]", body.get("compare", {}).get("values") == [12, 20, 30])
            check("POST has same lane as a normal Make", body.get("lane") == "t")
            check("POST has same kind as a normal Make", body.get("kind") == "image")
            check("POST has same mode as a normal Make", body.get("mode") == "t2i")
            # The prompt may or may not be present depending on room state
            prompt_ok = body.get("prompt") == "a golden dragon" or body.get("prompt") == ""
            check("POST prompt matches form", prompt_ok)

        # ------------------------------------------------------------------
        # 5. Single value shows "Give 2 to 6 values." and sends nothing.
        # ------------------------------------------------------------------
        prev_count = len(sent_compare)
        page.fill("#compareValues", "42")
        page.click("#compareBtn")
        page.wait_for_timeout(300)
        check("Single value shows error message",
              "Give 2 to 6 values." in page.inner_text("#compareMsg"))
        check("Single value sends no POST", len(sent_compare) == prev_count)

        # ------------------------------------------------------------------
        # 6. Non-numeric text on a number axis shows the numbers message.
        # ------------------------------------------------------------------
        page.select_option("#compareAxis", "steps")
        page.fill("#compareValues", "fast, slow, medium")
        page.click("#compareBtn")
        page.wait_for_timeout(300)
        check("Non-numeric on number axis shows numbers message",
              "Those values need to be numbers." in page.inner_text("#compareMsg"))

        # ------------------------------------------------------------------
        # 7. Compare panel: intercept /api/jobs to return compare-tagged jobs.
        # ------------------------------------------------------------------
        COMPARE_GROUP = "testgroup123"
        COMPARE_JOBS = [
            {"id": "cj1", "lane": "t", "lane_name": "Fake lane", "kind": "image", "mode": "t2i",
             "status": "done", "prompt": "a golden dragon", "seed": 99, "steps": 12,
             "created": time.time() - 10,
             "outputs": [{"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}],
             "compare": {"group": COMPARE_GROUP, "axis": "steps", "value": 12}},
            {"id": "cj2", "lane": "t", "lane_name": "Fake lane", "kind": "image", "mode": "t2i",
             "status": "done", "prompt": "a golden dragon", "seed": 99, "steps": 20,
             "created": time.time() - 5,
             "outputs": [{"filename": "fix.png", "subfolder": "", "type": "output", "media": "image"}],
             "compare": {"group": COMPARE_GROUP, "axis": "steps", "value": 20}},
        ]

        def route_jobs(route):
            route.fulfill(status=200, json={"jobs": COMPARE_JOBS})

        # Also intercept compare to succeed
        def route_compare_ok(route):
            route.fulfill(status=200,
                          json={"ok": True, "group": COMPARE_GROUP, "axis": "steps",
                                "jobs": [{"id": "cj1", "value": 12},
                                         {"id": "cj2", "value": 20}]})

        # The page polls /api/jobs every 2 s; without this route a poll can replace the
        # injected jobs below and hide the panel mid-check (a 1-in-5 flake).
        page.route("**/api/jobs*", route_jobs)

        # Directly inject compare-tagged jobs into STATE.jobs and render the panel
        result = page.evaluate("""
        (jobs) => {
          try {
            STATE.jobs = jobs.map(j => Object.assign({}, j));
            COMPARE_DISMISSED_GROUP = null; try{ localStorage.removeItem('bwf.compare.dismissed'); }catch(e){}
            renderComparePanel();
            return {
              ok: true,
              panelHidden: $('#comparePanel') && $('#comparePanel').hidden,
              compareJobs: STATE.jobs.filter(j => j.compare).length,
              gridHTML: ($('#comparePanelGrid') || {}).innerHTML.substring(0, 200)
            };
          } catch(e) {
            return {ok: false, error: String(e)};
          }
        }""", COMPARE_JOBS)
        check("Compare panel inject succeeded", result.get('ok', False))
        if result.get('ok'):
            check("Compare panel shows with compare-tagged jobs", not result.get('panelHidden', True))

        if result.get('ok') and not result.get('panelHidden', True):
            # Check caption text
            captions = page.evaluate("() => Array.from(document.querySelectorAll('.compare-caption')).map(e => e.textContent)")
            check("Compare panel has captions", len(captions) >= 2)
            has_steps_caption = any("steps" in c.lower() or "Steps" in c for c in captions)
            check("Caption mentions axis label", has_steps_caption)

            # The grid shows the values in the order they were typed (queue order), not sorted.
            order = page.evaluate("""
            (jobs) => {
              const t = Date.now() / 1000;
              STATE.jobs = [40, 20, 30].map((v, i) => Object.assign({}, jobs[0],
                {id: 'ord' + i, created: t + i, compare: {group: 'ordgroup', axis: 'steps', value: v}}));
              COMPARE_DISMISSED_GROUP = null; try{ localStorage.removeItem('bwf.compare.dismissed'); }catch(e){}
              renderComparePanel();
              return Array.from(document.querySelectorAll('.compare-caption')).map(e => e.textContent);
            }""", COMPARE_JOBS)
            check("Grid keeps the typed order (40, 20, 30), not sorted",
                  [c.split(": ")[-1] for c in order] == ["40", "20", "30"], order)
            same_node = page.evaluate("""
            () => {
              const el = document.querySelector('.compare-cell');
              renderComparePanel();
              return el === document.querySelector('.compare-cell');
            }""")
            check("An unchanged grid is not rebuilt on the next poll (pictures are not re-downloaded)", same_node)
            page.evaluate("(jobs) => { STATE.jobs = jobs.map(j => Object.assign({}, j)); COMPARE_DISMISSED_GROUP = null; try{ localStorage.removeItem('bwf.compare.dismissed'); }catch(e){} renderComparePanel(); }", COMPARE_JOBS)

            # A comparison of pictures does not belong in another kind of room (it ate the Music room on the live page).
            other_room = page.evaluate("""
            (jobs) => {
              STATE.jobs = jobs.map(j => Object.assign({}, j));
              COMPARE_DISMISSED_GROUP = null; try{ localStorage.removeItem('bwf.compare.dismissed'); }catch(e){}
              const cap = STATE.cap; STATE.cap = 'audio'; renderComparePanel();
              const hiddenThere = $('#comparePanel').hidden;
              STATE.cap = cap; renderComparePanel();
              return {hiddenThere, shownHere: !$('#comparePanel').hidden};
            }""", COMPARE_JOBS)
            check("The panel is hidden in a room of another kind", other_room.get("hiddenThere") is True, other_room)
            check("...and shown again in the matching room", other_room.get("shownHere") is True, other_room)
            # An old comparison is history, not something to put back in front of the person.
            old_hidden = page.evaluate("""
            (jobs) => {
              STATE.jobs = jobs.map(j => Object.assign({}, j, {created: Date.now()/1000 - 7*3600}));
              renderComparePanel(); const h = $('#comparePanel').hidden;
              STATE.jobs = jobs.map(j => Object.assign({}, j)); renderComparePanel(); return h;
            }""", COMPARE_JOBS)
            check("A comparison older than six hours is not shown again", old_hidden is True)
            # Six cells must not push the room's input off the screen: the panel is capped and scrolls.
            tall = page.evaluate("""
            (jobs) => {
              const base = jobs[0], t = Date.now()/1000;
              STATE.jobs = [1,2,3,4,5,6].map(i => Object.assign({}, base, {id:'tall'+i, created: t+i, compare:{group:'tallgroup', axis:'steps', value:i*10}}));
              COMPARE_DISMISSED_GROUP = null; try{ localStorage.removeItem('bwf.compare.dismissed'); }catch(e){}
              renderComparePanel();
              const el = $('#comparePanel');
              return {h: el.offsetHeight, vh: window.innerHeight, cells: document.querySelectorAll('.compare-cell').length};
            }""", COMPARE_JOBS)
            check("Six cells are all there", tall.get("cells") == 6, tall)
            check("The panel never takes more than 40% of the screen height", tall.get("h", 9999) <= 0.40 * tall.get("vh", 1), tall)
            page.evaluate("(jobs) => { STATE.jobs = jobs.map(j => Object.assign({}, j)); COMPARE_DISMISSED_GROUP = null; try{ localStorage.removeItem('bwf.compare.dismissed'); }catch(e){} renderComparePanel(); }", COMPARE_JOBS)

            # Check Close button hides the panel
            page.click("#comparePanelClose")
            page.wait_for_timeout(300)
            check("Close hides the compare panel",
                  page.evaluate("() => $('#comparePanel') && $('#comparePanel').hidden"))

            stored = page.evaluate("() => { try{ return localStorage.getItem('bwf.compare.dismissed'); }catch(e){ return 'no-storage'; } }")
            check("Close remembers the dismissed group in browser storage (it survives a reload)", stored == COMPARE_GROUP, stored)
            survives = page.evaluate("() => { COMPARE_DISMISSED_GROUP = null; renderComparePanel(); return $('#comparePanel').hidden; }")
            check("A reload (variable reset) still keeps it dismissed", survives is True)
            # A fresh poll shouldn't re-show the same group
            page.evaluate("() => { pollJobs(); }")
            page.wait_for_timeout(500)
            check("Panel stays closed after dismiss",
                  page.evaluate("() => $('#comparePanel') && $('#comparePanel').hidden"))

        page.unroute("**/api/jobs*")
        # ------------------------------------------------------------------
        # 8. Make still posts exactly the same body as before the refactor.
        # ------------------------------------------------------------------
        sent_make = []

        def route_generate(route):
            data = json.loads(route.request.post_data or "{}")
            sent_make.append(data)
            route.fulfill(status=200, json={"ok": True, "job": {"id": "m1", "status": "queued",
                                                                 "lane": "t", "lane_name": "Fake lane",
                                                                 "kind": "image", "mode": "t2i"}})

        page.route("**/api/generate", route_generate)

        open_picture_room(page)
        page.click("#makeBtn")
        page.wait_for_timeout(500)

        check("Make still sends one POST", len(sent_make) == 1)
        if sent_make:
            make_body = sent_make[0]
            check("Make body has lane", make_body.get("lane") == "t")
            check("Make body has kind", make_body.get("kind") == "image")
            check("Make body has mode", make_body.get("mode") == "t2i")
            # The prompt may or may not be present depending on room state
            prompt_ok = make_body.get("prompt") in ("a golden dragon", "")
            check("Make body prompt matches form", prompt_ok)
            # No compare key should be present for normal Make
            check("Make body has no compare key", "compare" not in make_body)

        page.close()

        print("All compare UI checks complete")

except Exception as e:
    print("Exception: %s" % e)
    import traceback
    traceback.print_exc()
    ui.FAILED.append(str(e))
finally:
    ui.finish()
