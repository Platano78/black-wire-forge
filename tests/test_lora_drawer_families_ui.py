"""LORA-2B UI acceptance gate: with more than one style family present on a
lane, the "Browse styles" drawer shows tabs and switching one re-queries the
matching family's catalog -- against a REAL browser (Playwright/Chromium,
SKIPs cleanly if not installed) and a REAL server subprocess.

server.py runs as a subprocess (see tests/_ui_fixture.py), so slice A's real
multi-family engine packs can't be monkeypatched in-process the way the
non-UI catalog tests do; server.py's BWF_TEST_STYLE_FAMILIES env var (same
pattern as BWF_TEST_HF_API) substitutes a fixed v2-shaped family list for
engines.style_catalogs() instead. Both fixture families here match the same
qwen_unet role/model -- the Picture room is the only one with a real
pool_select style field before slice A lands its own fields on Video/Music,
so this drives the identical tab-building/re-query code the spec's "Video:
LTX-2.5 | MiniMax-H3" example describes, just over two same-cap families
rather than two different caps (see report-B.md's "Low-confidence rulings").

Run: python3 tests/test_lora_drawer_families_ui.py
"""
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _ui_fixture as uif  # noqa: E402 -- also does the Playwright availability check
from _ui_fixture import check, FAILED  # noqa: E402

FIXTURE = json.load(open(os.path.join(HERE, "fixtures", "hf_qwen_loras.json")))
# A second, distinct fixture catalog for the second family -- proves a tab
# switch actually re-queries (not just re-labels the same list).
FAMILY_B_LISTING = [{"downloads": 42, "id": "someone/other-pack", "likes": 3, "modelId": "someone/other-pack",
                     "tags": ["lora", "base_model:adapter:Qwen/Qwen-Image-2.1-OTHER"]}]
FAMILY_B_DETAIL = {"someone/other-pack": {"cardData": {"license": "apache-2.0"},
                    "siblings": [{"rfilename": "other.safetensors", "size": 999}]}}


class _HFFixture(BaseHTTPRequestHandler):
    def do_GET(self):
        family_b = "OTHER" in self.path or "other-pack" in self.path
        if "?filter=" in self.path:
            body = json.dumps(FAMILY_B_LISTING if family_b else FIXTURE["listing"]).encode()
        elif "/api/models/" in self.path:
            repo = self.path.split("/api/models/", 1)[1].split("?", 1)[0]
            src = FAMILY_B_DETAIL if family_b else FIXTURE["details"]
            body = json.dumps(src.get(repo, {})).encode()
        elif self.path.endswith("/raw/main/README.md"):
            body = b""
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
os.environ["BWF_TEST_HF_WEB"] = "http://127.0.0.1:%d" % hf.server_address[1]
os.environ["BWF_TEST_STYLE_FAMILIES"] = json.dumps([
    {"id": "qwen_a", "label": "Qwen Style A", "cap": "image", "modes": ["t2i", "edit", "pixelart"],
     "role": "qwen_unet", "match": {"any": ["qwen_image", "qwen-image"]},
     "hf_base": "Qwen/Qwen-Image-2.1", "folder": "qwen_a"},
    {"id": "qwen_b", "label": "Qwen Style B", "cap": "image", "modes": ["t2i", "edit", "pixelart"],
     "role": "qwen_unet", "match": {"any": ["qwen_image", "qwen-image"]},
     "hf_base": "Qwen/Qwen-Image-2.1-OTHER", "folder": "qwen_b"},
])

url = uif.start(jobs={})

from playwright.sync_api import sync_playwright  # noqa: E402

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url + "#room=picture", wait_until="networkidle")
        page.wait_for_function("() => STATE.lanes.some(l => l.id === 't' && l.up)", timeout=15000)
        page.wait_for_function("() => !document.querySelector('#browseStylesBtn').hidden", timeout=15000)

        page.click("#browseStylesBtn")
        page.wait_for_function("() => document.querySelector('#lorasDialog').open", timeout=5000)
        page.wait_for_function("() => document.querySelectorAll('#lorasFamilyTabs button').length === 2",
                                timeout=15000)
        check("two family tabs render when two families are present",
              page.eval_on_selector_all("#lorasFamilyTabs button", "e => e.length") == 2)
        labels = page.eval_on_selector_all("#lorasFamilyTabs button", "es => es.map(e => e.textContent)")
        check("the tabs are labelled from each family's own \"label\"",
              "Qwen Style A" in labels and "Qwen Style B" in labels, labels)
        check("the first family's tab starts pressed",
              page.get_attribute('#lorasFamilyTabs button[data-family="qwen_a"]', "aria-pressed") == "true")

        page.wait_for_function("() => document.querySelectorAll('#lorasBody .credit-row').length === 3",
                                timeout=15000)
        check("family A's catalog (3 packs, the single-family fixture) renders by default",
              page.eval_on_selector_all("#lorasBody .credit-row", "e => e.length") == 3)

        page.click('#lorasFamilyTabs button[data-family="qwen_b"]')
        page.wait_for_function(
            "() => document.querySelectorAll('#lorasBody [data-get-repo=\"someone/other-pack\"]').length === 1",
            timeout=15000)
        check("switching tabs re-queries: family B's DIFFERENT catalog now renders",
              page.eval_on_selector_all("#lorasBody .credit-row", "e => e.length") == 1)
        check("family B's tab is now the pressed one",
              page.get_attribute('#lorasFamilyTabs button[data-family="qwen_b"]', "aria-pressed") == "true"
              and page.get_attribute('#lorasFamilyTabs button[data-family="qwen_a"]', "aria-pressed") == "false")

        page.locator('#lorasBody [data-get-repo="someone/other-pack"]').first.click()
        page.wait_for_function("() => (document.querySelector('#lorasMsg') || {}).textContent.includes('hf download')",
                                timeout=5000)
        msg = page.inner_text("#lorasMsg")
        check("the copy-paste command names family B's own folder (\"qwen_b\"), not family A's",
              "--local-dir <ComfyUI>/models/loras/qwen_b" in msg, msg)

        page.close()
        browser.close()
finally:
    uif.finish()
