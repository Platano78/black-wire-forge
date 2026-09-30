"""W3 in the browser (gate 7): a real server.py in Setup mode, the fake ComfyUI, and a local fake Hugging Face
(tests/_fake_hf.py, through server.py's test-only 127.0.0.1-pinned hook) that trickles the file.

Step 1: "ComfyUI runs on this computer" + its models folder, a wrong folder says why, a right one is used.
Step 2: the Cleanup card says what Download would fetch (files, size, destination) and shows the licences
before anything starts, nothing is fetched until the button is pressed; then progress appears in the card;
Cancel stops it and offers "Discard partial files"; Download again carries on. After "Save and start" the
normal page shows the one-line progress ("Downloading models: 1 of 1, N%") with a plain list behind Details,
and the line goes away when the file lands. No page errors. SKIPs cleanly without Playwright/Chromium.

Set BWF_SETUP_SHOTS=<dir> to save screenshots. Needs port 3998 free.
Run: python3 tests/test_setup_downloads_ui.py
"""
import os
import shutil
import subprocess
import sys
import time
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _ui_fixture  # noqa: E402,F401  (SKIPs cleanly without Playwright/Chromium)
import _fake_hf  # noqa: E402

HF = _fake_hf.serve()
HF.slow_sleep = 0.1                       # ~2.6 MB/s: the 67 MB file takes ~25 s
os.environ["BWF_TEST_HF_DOWNLOAD"] = HF.origin
os.environ.pop("HF_TOKEN", None)

import _setup_fixture as fx  # noqa: E402
from _setup_fixture import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = os.environ.get("BWF_SETUP_SHOTS")
UP = ("schwgHao/RealESRGAN_x4plus", "RealESRGAN_x4plus.pth", 67_040_989)
UP_PATH = _fake_hf.file_path(UP[0], UP[1])
DRIVE = os.path.join(fx.SCRATCH, "other-drive", "upscale_models")
HF.sizes[UP_PATH], HF.routes[UP_PATH] = UP[2], "slow"


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=True)


def models_folder():
    m = os.path.join(fx.SCRATCH, "models")
    for s in ("checkpoints", "vae", "loras", "background_removal"):
        os.makedirs(os.path.join(m, s), exist_ok=True)
    os.makedirs(DRIVE, exist_ok=True)            # REV-A: upscale_models is the user's own link to another drive
    os.symlink(DRIVE, os.path.join(m, "upscale_models"))
    with open(os.path.join(m, "background_removal", "model.safetensors"), "wb") as f:
        f.truncate(444_473_596)              # sparse, at its exact size: Installed
    return m


def fetched():
    return [r for r in HF.log if r["path"] == UP_PATH]


def walk(browser, lane_port):
    m = models_folder()
    proc, cfg, logp = fx.boot("dlui")
    up = fx.wait_health(proc, want_setup=True) is not None
    check("Setup server up", up, open(logp).read()[-400:])
    if not up:
        return
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(fx.BASE + "/")
    page.get_by_role("button", name="Yes, find it").click()
    page.wait_for_function("!/^Looking/.test(document.getElementById('c-status').textContent)", timeout=20000)
    page.get_by_label("Address", exact=True).fill("127.0.0.1")
    page.get_by_label("Port", exact=True).fill(str(lane_port))
    page.get_by_role("button", name="Check this address").click()
    page.wait_for_function("document.getElementById('c-gpu').textContent === 'fake gpu'", timeout=10000)
    same = page.get_by_label("ComfyUI runs on this computer")
    check("step 1 offers the same-machine declaration, off by default", same.is_visible() and not same.is_checked())
    check("the models folder field shows only once it is declared", not page.locator("#m-path").is_visible())
    same.check()
    page.get_by_label("Its models folder").fill(os.path.join(fx.SCRATCH, "not-there"))
    page.get_by_role("button", name="Use this folder").click()
    page.wait_for_function("/Nothing is at that path/.test(document.getElementById('m-status').textContent)",
                           timeout=10000)
    check("a wrong folder says which check failed", "Nothing is at that path" in page.locator("#m-status").text_content())
    page.get_by_label("Its models folder").fill(m)
    page.get_by_role("button", name="Use this folder").click()
    page.wait_for_function("/^Using /.test(document.getElementById('m-status').textContent)", timeout=10000)
    check("a right folder is used, by its real path", ("Using " + os.path.realpath(m)) in
          page.locator("#m-status").text_content(), page.locator("#m-status").text_content())
    unlabelled = page.evaluate("[...document.querySelectorAll('section:not([hidden]) input')]"
                               ".filter(e => !e.labels || !e.labels.length).map(e => e.id)")
    check("step 1: every input has a label", unlabelled == [], unlabelled)
    shot(page, "w3-step1")
    page.get_by_role("button", name="Next").click()
    page.wait_for_function("/models folder/.test(document.getElementById('r-status').textContent)", timeout=30000)
    card = page.locator("details.room[data-room=cleanup]")
    card.locator(":scope > summary").click()
    panel = card.locator(".dl")
    btn = panel.get_by_role("button", name="Download 1 file (0.1 GB)")
    text = panel.text_content()
    check("the card's Download button shows the files and size before starting", btn.is_visible(), text)
    check("... with each file's real destination (through the link)",
          os.path.join(os.path.realpath(DRIVE), UP[1]) in text, text)
    check("... and the licences, each with its summary", "MIT: Free to use commercially." in text, text)
    check("nothing is fetched before the button is pressed", fetched() == [])
    check("no sideways scroll with the card open", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
    shot(page, "w3-card-before")
    btn.click()
    page.wait_for_selector("details.room[data-room=cleanup] .dl-prog .progress-bar", timeout=15000)
    page.wait_for_function("/Downloading: [1-9]/.test((document.querySelector("
                           "'details.room[data-room=cleanup] .progress-label') || {}).textContent || '')",
                           timeout=30000)
    check("progress appears in the card", "Downloading:" in card.locator(".progress-label").text_content())
    check("... the file is being fetched now", len(fetched()) == 1)
    shot(page, "w3-card-progress")
    card.get_by_role("button", name="Cancel").click()
    page.wait_for_selector("details.room[data-room=cleanup] .dl-discard", timeout=20000)
    check("Cancel stops it and offers to discard the partial file",
          "cancelled" in card.locator(".dl-files").text_content().lower() or
          "Cancelled" in card.locator(".dl-files").text_content(), card.locator(".dl-files").text_content())
    check("the card stays open through the refresh", card.evaluate("d => d.open"))
    check("no empty progress bar for a cancelled file (no '0.0 GB of 0.0 GB')",
          card.locator(".progress-label").count() == 0, card.locator(".dl-prog").text_content())
    check("a file found in the models folder is 'already installed', not claimed to be in ComfyUI",
          "already installed" in card.text_content() and "already in your ComfyUI" not in card.text_content())
    part = os.path.join(m, "upscale_models", UP[1] + ".part")
    check("the .part stays for a later resume", os.path.exists(part) and os.path.getsize(part) > 0)
    shot(page, "w3-card-cancelled")
    card.get_by_role("button", name="Download 1 file (0.1 GB)").click()
    page.wait_for_selector("details.room[data-room=cleanup] .dl-cancel", timeout=15000)
    check("Download again carries on from the partial file",
          (fetched()[-1]["headers"].get("range") or "").startswith("bytes="), fetched()[-1]["headers"])
    page.get_by_role("button", name="Next").click()
    page.get_by_role("button", name="Skip for now").click()
    page.get_by_role("button", name="Next").click()
    page.wait_for_function("document.getElementById('f-text').textContent.length > 0", timeout=10000)
    with page.expect_navigation(timeout=30000):
        page.get_by_role("button", name="Save and start").click()
    page.wait_for_load_state("load")
    check("Save and start lands in the app", page.title() == "Black Wire Forge", page.title())
    page.wait_for_function("!document.getElementById('dlLine').hidden", timeout=20000)
    line = page.locator("#dlText").text_content()
    check("the normal page shows the one-line progress", line.startswith("Downloading models: 1 of 1, ")
          and line.endswith("%"), line)
    page.locator("#dlMore summary").click()
    items = page.locator("#dlList li").all_text_contents()
    check("Details opens a plain list of the files", items and items[0].startswith(UP[1] + " (upscale_models)"), items)
    shot(page, "w3-app-progress")
    HF.slow_sleep = 0.0
    page.wait_for_function("document.getElementById('dlLine').hidden", timeout=90000)
    dest = os.path.join(m, "upscale_models", UP[1])
    check("the line goes away when the file lands, at its size, in the link's target",
          os.path.isfile(os.path.join(DRIVE, UP[1])) and os.path.getsize(dest) == UP[2] and not os.path.exists(part))
    check("no page errors", errors == [], errors)
    ctx.close()
    fx.stop(proc)


try:
    if not fx.port_free(fx.SETUP_PORT):
        check("port %d is free for the Setup server" % fx.SETUP_PORT, False, "something else is listening on it")
        fx.finish()
    # A ComfyUI with no model files at all (fake_comfy --pools {}): the Cleanup room's files come from the
    # models folder alone.
    pools = os.path.join(fx.SCRATCH, "empty-pools.json")
    with open(pools, "w") as f:
        f.write("{}")
    lane_port = fx.free_port()
    store = os.path.join(fx.SCRATCH, "lane")
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    fx.PROCS.append(subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port",
                                      str(lane_port), "--store", store, "--pools", pools], cwd=fx.REPO,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for _ in range(100):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % lane_port, timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            walk(browser, lane_port)
        finally:
            browser.close()
finally:
    shutil.rmtree(os.path.join(fx.SCRATCH, "models"), ignore_errors=True)
    shutil.rmtree(os.path.join(fx.SCRATCH, "other-drive"), ignore_errors=True)
    fx.finish()
