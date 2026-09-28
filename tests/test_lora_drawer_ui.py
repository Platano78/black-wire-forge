"""LORA-1 Build B UI acceptance gate: the "Browse styles" drawer renders,
its Advanced toggle re-queries, and "Get it" on a lane without opt-in shows
the copy-paste command -- against a REAL browser (Playwright/Chromium, SKIPs
cleanly if not installed) and a REAL server subprocess, with the HF catalog
call redirected to a local fixture server (BWF_TEST_HF_API) so no live
network happens.

Run: python3 tests/test_lora_drawer_ui.py
"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _ui_fixture as uif  # noqa: E402 -- also does the Playwright availability check
from _ui_fixture import check, FAILED  # noqa: E402

FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "hf_qwen_loras.json")))


class _HFFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if "?filter=" in self.path:
            body = json.dumps(FIXTURE["listing"]).encode()
        else:
            repo = self.path.split("/api/models/", 1)[1].split("?", 1)[0]
            body = json.dumps(FIXTURE["details"].get(repo, {})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


hf = ThreadingHTTPServer(("127.0.0.1", 0), _HFFixture)
threading.Thread(target=hf.serve_forever, daemon=True).start()
os.environ["BWF_TEST_HF_API"] = "http://127.0.0.1:%d/api/models" % hf.server_address[1]

url = uif.start(jobs={})

from playwright.sync_api import sync_playwright  # noqa: E402

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url + "#room=picture", wait_until="networkidle")
        page.wait_for_function("() => STATE.lanes.some(l => l.id === 't' && l.up)", timeout=15000)
        page.wait_for_function("() => !document.querySelector('#browseStylesBtn').hidden", timeout=15000)
        check("Browse styles is offered on a mode with a style picker",
              not page.is_hidden("#browseStylesBtn"))

        page.click("#browseStylesBtn")
        page.wait_for_function("() => document.querySelector('#lorasDialog').open", timeout=5000)
        page.wait_for_function("() => document.querySelectorAll('#lorasBody .credit-row').length > 0", timeout=15000)
        check("the drawer renders the fixture catalog (2 packs, nsfw one hidden by default)",
              page.eval_on_selector_all("#lorasBody .credit-row", "e => e.length") == 2)
        check("the NSFW-tagged pack is hidden by default",
              "NSFW-LORA-Qwen-Image-2.1" not in page.inner_text("#lorasBody"))
        check("licence unknown is stamped on the repo with no cardData.license",
              "licence unknown" in page.inner_text("#lorasBody"))

        page.check("#lorasAdvanced")
        page.wait_for_function("() => document.querySelectorAll('#lorasBody .credit-row').length === 3", timeout=15000)
        check("Advanced shows the NSFW-tagged pack too",
              "NSFW-LORA-Qwen-Image-2.1" in page.inner_text("#lorasBody"))

        page.uncheck("#lorasAdvanced")
        page.wait_for_function("() => document.querySelectorAll('#lorasBody .credit-row').length === 2", timeout=15000)

        page.locator('#lorasBody [data-get-repo="Alissonerdx/BFS-Best-Face-Swap"]').first.click()
        page.wait_for_function("() => (document.querySelector('#lorasMsg') || {}).textContent.includes('hf download')",
                                timeout=5000)
        msg = page.inner_text("#lorasMsg")
        check("lane 't' has no \"downloads\" opt-in: 'Get it' shows the copy-paste command, not a download",
              "hf download Alissonerdx/BFS-Best-Face-Swap" in msg
              and "--local-dir <ComfyUI>/models/loras" in msg, msg)

        page.click('[data-close="lorasDialog"]')
        check("the dialog closes", not page.evaluate("() => document.querySelector('#lorasDialog').open"))
        page.close()
        browser.close()
finally:
    uif.finish()
