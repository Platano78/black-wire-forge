"""WB: the Workflows tab in a real browser, against server.py and
tests/fixtures/fake_comfy.py --workflows (plus one ComfyUI lane that is down).

Checks: the WORKFLOWS tab sits in the top strip beside the Cutting Room and opens the
view; cloud (paid API) templates are hidden until the toggle; the media filter and
search narrow the cards; badges appear (Ready first); details list a missing model with
its source link and missing nodes with the ComfyUI-Manager sentence; "Save to ComfyUI
and open" shows the saved name and a link, and the file lands in the lane's userdata;
Your workflows lists it; a down lane says so; a room tab closes the view; keyboard
reachable, every control labelled; 390 px wide has no sideways scroll; no page errors;
an XSS string in a template title renders as text. SKIPs without Playwright/Chromium.

Run: python3 tests/test_workflows_ui.py   (BWF_WB_SHOTS=<dir> saves screenshots)
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures", "workflows")
FAILED = []
PROCS = []
SHOTS = os.environ.get("BWF_WB_SHOTS")

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _pw:
        _pw.chromium.launch().close()
except Exception as e:
    print("SKIP: playwright or its chromium is not installed (%s). pip install -r requirements-dev.txt "
          "&& python3 -m playwright install --with-deps chromium" % e)
    sys.exit(0)


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=True)


def titles(page):
    return page.eval_on_selector_all("#wfGrid .wf-title", "els => els.map(e => e.textContent)")


def badges(page):
    return page.eval_on_selector_all("#wfGrid .wf-badge", "els => els.map(e => e.textContent)")


def settle(page):
    """Wait until no card says Checking… (readiness answered for everything on screen)."""
    page.wait_for_function("() => document.querySelectorAll('#wfGrid .wf-card').length > 0 && "
                           "![...document.querySelectorAll('#wfGrid .wf-badge')].some(b => b.textContent === 'Checking…')",
                           timeout=20000)


SCRATCH = tempfile.mkdtemp(prefix="bwf_wb_ui_")
STORE = os.path.join(SCRATCH, "lane")
MINE = os.path.join(STORE, "userdata", "workflows")
os.makedirs(MINE)
with open(os.path.join(FIX, "templates", "wf_cloud_api.json")) as f, open(os.path.join(MINE, "Portrait pass.json"), "w") as g:
    g.write(f.read())
with open(os.path.join(MINE, "Widget missing.json"), "w") as g:   # REV-1: a file named only in a widget
    json.dump({"nodes": [{"id": 1, "type": "UNETLoader", "widgets_values": ["wf_absent_unet.safetensors", "default"]},
                         {"id": 2, "type": "SaveImage"}]}, g)

try:
    lane_port, port = free_port(), free_port()
    PROCS.append(subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(lane_port),
         "--store", STORE, "--workflows", FIX, "--comfy-version", "0.37.0"],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    cfg = os.path.join(SCRATCH, "config.json")
    with open(cfg, "w") as f:
        json.dump({"title": "wb ui", "port": port, "bind": "127.0.0.1", "timing": {"poll_seconds": 0.5},
                   "lanes": [{"id": "wb", "name": "Box A", "host": "127.0.0.1", "port": lane_port,
                              "caps": ["image", "video", "audio"]},
                             {"id": "off", "name": "Box B", "host": "127.0.0.1", "port": free_port(),
                              "caps": ["image"]}]}, f)
    env = dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=os.path.join(SCRATCH, "data"))
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                  stdout=open(os.path.join(SCRATCH, "server.log"), "w"),
                                  stderr=subprocess.STDOUT))
    URL = "http://127.0.0.1:%d/" % port
    for _ in range(200):
        try:
            with urllib.request.urlopen(URL + "api/lanes", timeout=1) as r:
                st = {l["id"]: l for l in json.load(r)["lanes"]}
            if st["wb"]["up"] and st["off"]["checked"]:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        raise SystemExit("server or fake lane did not come up")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        errors = []

        def watch(page):
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error"
                    and "Failed to load resource" not in m.text else None)

        page = browser.new_page(viewport={"width": 1280, "height": 900})
        watch(page)
        page.goto(URL, wait_until="networkidle", timeout=30000)

        print("the tab")
        check("WORKFLOWS is in the top strip, beside the Cutting Room",
              page.is_visible("#roomStrip #workflowsBtn")
              and page.eval_on_selector("#workflowsBtn", "b => b.parentElement.contains(document.querySelector('.room-cutting'))"
                                        " && getComputedStyle(b).textTransform === 'uppercase'"))
        check("same button style as the Cutting Room", page.eval_on_selector(
            "#workflowsBtn", "b => { const c = document.querySelector('.room-cutting'), x = getComputedStyle(b), "
            "y = getComputedStyle(c); return x.borderTopStyle === y.borderTopStyle && x.fontSize === y.fontSize "
            "&& x.minHeight === y.minHeight && x.letterSpacing === y.letterSpacing; }"))
        page.click("#workflowsBtn")
        page.wait_for_selector("#workflowsView:not([hidden])")
        settle(page)
        shot(page, "01-templates")
        check("the view replaces the rooms; the tab reads pressed, the rooms do not",
              page.is_visible("#workflowsView") and not page.is_visible("#layout")
              and page.get_attribute("#workflowsBtn", "aria-pressed") == "true"
              and page.eval_on_selector_all("#roomStrip [data-room-id]",
                                            "bs => bs.every(b => b.getAttribute('aria-pressed') === 'false')"))
        t, b = titles(page), badges(page)
        check("cloud (paid API) templates are hidden by default", "Cloud: Paid Image" not in t and len(t) == 7, t)
        check("badges appear, Ready first", b[:4] == ["Ready"] * 4 and "Ready" not in b[4:]
              and "Needs 1 model file" in b and "Needs nodes" in b and "Needs a newer ComfyUI" in b, b)
        check("Ready sorted by usage, the most used first", t[0] == "Ready: Text to Image [Base]", t)
        check("size of weights in GB", "2.1 GB of model files" in page.inner_text("#wfGrid"))
        check("a lane that is down says so", "Box B isn't answering right now" in page.inner_text("#wfLaneNote"))
        check("one lane up: no lane picker", not page.is_visible("#wfLanePick"))

        print("the XSS string")
        xss_title = '<img src=x onerror="window.__wfXss=1">Tricky <b>title</b>'
        check("renders as text", xss_title in t and page.evaluate("typeof window.__wfXss") == "undefined"
              and page.eval_on_selector_all("#wfGrid .wf-title img, #wfGrid .wf-title b", "e => e.length") == 0, t)

        print("filters")
        page.check("#wfCloud")
        check("the cloud toggle shows the paid template", "Cloud: Paid Image" in titles(page) and len(titles(page)) == 8)
        page.uncheck("#wfCloud")
        page.click('#wfMedia [data-wf-media="video"]')
        check("Video", titles(page) == ["Needs Model: Picture to Video"] and badges(page) == ["Needs 1 model file"],
              titles(page))
        page.click('#wfMedia [data-wf-media="audio"]')
        check("Audio", titles(page) == ["Needs Node: Song"], titles(page))
        page.click('#wfMedia [data-wf-media="3d"]')
        check("3D", titles(page) == [xss_title], titles(page))
        page.click('#wfMedia [data-wf-media="other"]')
        check("Other (a category type that is none of the four)",
              sorted(titles(page)) == ["Newer: Needs a Newer ComfyUI", "Thumb Big", "Thumb HTML"], titles(page))
        page.click('#wfMedia [data-wf-media="all"]')
        page.fill("#wfSearch", "tricky")
        check("search by title", titles(page) == [xss_title], titles(page))
        page.fill("#wfSearch", "not markup")
        check("search by description", titles(page) == [xss_title], titles(page))
        page.fill("#wfSearch", "text to image")
        check("search by tag", titles(page) == ["Ready: Text to Image [Base]"], titles(page))
        page.fill("#wfSearch", "")

        print("details")
        page.click('#wfGrid .wf-card[data-wf-name="wf_needs_node_audio"]')
        page.wait_for_selector("#wfDialog[open]")
        body = page.inner_text("#wfDialogBody")
        shot(page, "02-needs-nodes")
        check("missing nodes: class, pack ids and the REV-2 sentence",
              "comfyui-wf-fixture-pack" in body
              and ("WfFixturePackNode — not found on this ComfyUI. If the workflow opens and runs fine there, these "
                   "are screen-only helper nodes and nothing is missing; otherwise install them with ComfyUI-Manager.")
              in body, body)
        page.keyboard.press("Escape")
        page.click('#wfGrid .wf-card[data-wf-name="wf_needs_model_video"]')
        page.wait_for_selector("#wfDialog[open]")
        body = page.inner_text("#wfDialogBody")
        link = page.eval_on_selector("#wfDialogBody ul a", "a => ({href: a.href, rel: a.rel, text: a.textContent})")
        check("a missing model: folder + file and its source link (rel=noopener)",
              "diffusion_models/wf_missing_video.safetensors" in body
              and link["href"] == "https://example.com/wf_missing_video.safetensors" and "noopener" in link["rel"],
              (body, link))
        page.keyboard.press("Escape")

        print("open")
        page.focus('#wfGrid .wf-card[data-wf-name="wf_ready_image"]')
        page.keyboard.press("Enter")
        page.wait_for_selector("#wfDialog[open]")
        body = page.inner_text("#wfDialogBody")
        check("reachable by keyboard; tutorial link", "How to use it (tutorial)" in body and page.eval_on_selector(
            "#wfDialogBody a", "a => a.href === 'https://example.com/tutorial' && a.rel.includes('noopener')"), body)
        check("the button says Save to ComfyUI and open", page.inner_text("#wfOpenBtn") == "Save to ComfyUI and open")
        page.click("#wfOpenBtn")
        page.wait_for_selector("#wfOpenResult a")
        res = page.inner_text("#wfOpenResult")
        link = page.eval_on_selector("#wfOpenResult a", "a => ({href: a.href, rel: a.rel})")
        shot(page, "03-saved")
        check("shows the saved name and a link to the lane",
              "Ready Text to Image Base.json" in res and link["href"] == "http://127.0.0.1:%d/" % lane_port
              and "noopener" in link["rel"], (res, link))
        check("the file landed in the lane's userdata", os.path.isfile(os.path.join(MINE, "Ready Text to Image Base.json")))
        page.keyboard.press("Escape")

        print("your workflows")
        page.click('#wfTabs [data-wf-tab="mine"]')
        page.wait_for_function("() => [...document.querySelectorAll('#wfGrid .wf-title')]"
                               ".some(e => e.textContent === 'Ready Text to Image Base')", timeout=10000)
        settle(page)
        shot(page, "04-mine")
        check("lists the saved copy and the seeded one, with badges",
              sorted(titles(page)) == ["Portrait pass", "Ready Text to Image Base", "Widget missing"]
              and badges(page) == ["Ready", "Ready", "Needs 1 model file"], (titles(page), badges(page)))
        check("no media filter or cloud toggle there", not page.is_visible("#wfMedia") and not page.is_visible("#wfCloud"))
        page.click('#wfGrid .wf-card[data-wf-name="Widget missing.json"]')
        page.wait_for_selector("#wfDialog[open]")
        body = page.inner_text("#wfDialogBody")
        check("REV-1: a widget-only file says whose list it is missing from",
              "wf_absent_unet.safetensors - not in this ComfyUI's list for UNETLoader" in body, body)
        page.keyboard.press("Escape")
        page.click('#wfGrid .wf-card[data-wf-name="Portrait pass.json"]')
        page.wait_for_selector("#wfDialog[open]")
        check("a saved workflow's button says Open in ComfyUI", page.inner_text("#wfOpenBtn") == "Open in ComfyUI")
        page.click("#wfOpenBtn")
        page.wait_for_selector("#wfOpenResult a")
        check("and just links to the lane", "Workflows panel" in page.inner_text("#wfOpenResult"))
        page.keyboard.press("Escape")

        print("labels")
        unlabelled = page.evaluate("""() => [...document.querySelectorAll(
            '#workflowsView button, #workflowsView input, #workflowsView select, #wfDialog button')]
          .filter(e => !((e.labels && e.labels.length) || e.getAttribute('aria-label') || e.textContent.trim()))
          .map(e => e.outerHTML.slice(0, 80))""")
        check("every control has a label", unlabelled == [], unlabelled)

        print("leaving")
        page.click('#roomStrip [data-room-id]')
        page.wait_for_selector("#workflowsView", state="hidden")
        check("a room tab closes the view", page.is_visible("#layout")
              and page.get_attribute("#workflowsBtn", "aria-pressed") == "false")
        page.close()

        print("390 px")
        small = browser.new_page(viewport={"width": 390, "height": 844})
        watch(small)
        small.goto(URL + "#workflows", wait_until="networkidle", timeout=30000)
        small.wait_for_selector("#workflowsView:not([hidden])")
        settle(small)
        shot(small, "05-390")
        sw = small.evaluate("document.documentElement.scrollWidth")
        check("#workflows opens the tab on load; no sideways scroll", sw <= 390, sw)
        small.click('#wfGrid .wf-card[data-wf-name="wf_needs_node_audio"]')
        small.wait_for_selector("#wfDialog[open]")
        sw = small.evaluate("document.documentElement.scrollWidth")
        check("nor with the details open", sw <= 390, sw)
        small.close()

        check("no page errors", errors == [], errors)
        browser.close()
finally:
    if sys.exc_info()[0] is not None:
        import traceback
        traceback.print_exc()
        FAILED.append("the suite crashed")
    for p in PROCS:
        p.terminate()
    print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
    sys.exit(1 if FAILED else 0)
