"""LORA-2B UI acceptance gate: the "Browse styles" drawer renders a card's
name/author/description/licence/size/trigger/strength, an NSFW pack is
listed WITH a badge (owner ruling 2026-09-28: no hiding, no "Advanced"
toggle), and "Get it" on a lane without opt-in shows the copy-paste command
-- against a REAL browser (Playwright/Chromium, SKIPs cleanly if not
installed) and a REAL server subprocess, with both the HF catalog call
(BWF_TEST_HF_API) and the README fetch (BWF_TEST_HF_WEB) redirected to a
local fixture server, so no live network happens.

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
        elif "/api/models/" in self.path:
            repo = self.path.split("/api/models/", 1)[1].split("?", 1)[0]
            body = json.dumps(FIXTURE["details"].get(repo, {})).encode()
        elif self.path.endswith("/raw/main/README.md"):
            repo = self.path[1:].split("/raw/main/README.md", 1)[0]
            text = FIXTURE.get("readmes", {}).get(repo, "")
            body = text.encode("utf-8")
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


hf = ThreadingHTTPServer(("127.0.0.1", 0), _HFFixture)
threading.Thread(target=hf.serve_forever, daemon=True).start()
os.environ["BWF_TEST_HF_API"] = "http://127.0.0.1:%d/api/models" % hf.server_address[1]
# LORA-2B: the README fetch is a SEPARATE host call from the catalog list/
# detail above -- must be redirected too, or the drawer would reach out to
# real huggingface.co during an "offline" suite.
os.environ["BWF_TEST_HF_WEB"] = "http://127.0.0.1:%d" % hf.server_address[1]

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
        page.wait_for_function("() => document.querySelectorAll('#lorasBody .credit-row').length === 3",
                                timeout=15000)
        check("owner ruling 2026-09-28: every pack is listed, including the NSFW-tagged one "
              "(no hiding, no Advanced toggle)",
              page.eval_on_selector_all("#lorasBody .credit-row", "e => e.length") == 3)
        check("the NSFW-tagged repo's card is present (its 'Get it' carries the real repo id)",
              page.query_selector('#lorasBody [data-get-repo="f23gg/NSFW-LORA-Qwen-Image-2.1"]') is not None)
        check("there is no Advanced toggle any more", page.query_selector("#lorasAdvanced") is None)

        body_text = page.inner_text("#lorasBody")
        check("a readable name (not the raw repo id) is shown",
              "Bfs Best Face Swap" in body_text or "BFS Best Face Swap" in body_text.replace("Bfs", "BFS"), body_text)
        check("the author is shown", "by Alissonerdx" in body_text, body_text)
        check("the description from the fixture README's first prose sentence is shown",
              "swaps faces cleanly" in body_text, body_text)
        check("the trigger word from the README is shown", "bfsface" in body_text, body_text)
        check("the recommended strength from the README is shown", "0.9" in body_text, body_text)
        check("licence unknown is stamped on the repo with no cardData.license",
              "licence unknown" in body_text)
        check("the NSFW pack carries a plain NSFW badge", "NSFW" in body_text)

        uif.shot(page, "drawer")

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
