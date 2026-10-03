"""Grid check's result reaches the page: the one-line summary and a Download for grid.json,
done by clicking (Cover -> Grid check -> upload -> Make), not through the API. A stand-in
"producer Python" writes both outputs and prints the line; a stand-in ffmpeg is only found on
PATH. Real server.py, real browser; SKIPs without Playwright/Chromium.

Run: python3 tests/test_grid_result_ui.py
"""
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _ui_fixture as ui  # noqa: E402  (SKIPs cleanly without Playwright/Chromium)
from _portable_exec import make_program  # noqa: E402
from _ui_fixture import check, free_port, wait_up  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

REPO = os.path.dirname(HERE)
SUMMARY = "4/4, 111.5 BPM (asked 110), drift +0.4%"
S = tempfile.mkdtemp(prefix="bwf_grid_ui_")
# the shebang line is for POSIX only; the Python itself is the py_body
STUB = ("#!%s\n" % sys.executable +
        "import json, os, sys\na = sys.argv[2:]\nif '--check' in a: sys.exit(0)\n"
        "out = a[a.index('--out') + 1]; os.makedirs(out, exist_ok=True)\n"
        "json.dump({'summary': %r}, open(os.path.join(out, 'grid.json'), 'w'))\n"
        "open(os.path.join(out, 'grid-check.mp3'), 'wb').write(b'ID3' + bytes(64))\n"
        "print('PROGRESS 4/4', flush=True); print(%r, flush=True)\n") % (SUMMARY, SUMMARY)


os.makedirs(os.path.join(S, "bin"))
os.makedirs(os.path.join(S, "data"))
stub = make_program(os.path.join(S, "producer-python"), STUB, py_body=STUB.split("\n", 1)[1])
make_program(os.path.join(S, "bin", "ffmpeg"), "#!/bin/sh\nexit 0\n", windows_cmd="@exit /b 0\r\n")
song = os.path.join(S, "song.wav")
pcm = bytes(16000)
with open(song, "wb") as f:
    f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)
port = free_port()
cfg = os.path.join(S, "config.json")
with open(cfg, "w") as f:
    json.dump({"title": "grid ui", "port": port, "bind": "127.0.0.1",
               "lanes": [{"id": "cpu", "name": "This machine", "kind": "process", "caps": ["producer"]}],
               "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0}}, f)
env = dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=os.path.join(S, "data"), BWF_PRODUCER_PYTHON=stub,
           PATH=os.path.join(S, "bin") + os.pathsep + os.environ.get("PATH", ""))
ui.PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                 stdout=open(os.path.join(S, "server.log"), "w"), stderr=subprocess.STDOUT))
url = "http://127.0.0.1:%d/" % port
try:
    wait_up(url + "api/health")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url + "?t=%f" % time.time())
        page.wait_for_selector('#roomStrip [data-room-id="cover"]', timeout=15000)
        page.click('#roomStrip [data-room-id="cover"]')
        page.click("#engineChipBtn")
        page.locator("#enginePicker").get_by_text("Grid check", exact=False).first.click()
        page.locator("#roomForm input[type=file]").first.set_input_files(song)
        page.wait_for_timeout(1500)
        page.get_by_role("button", name="Make").first.click()
        summary = page.locator("#jobSummary")
        try:
            summary.wait_for(state="visible", timeout=20000)
        except Exception:
            pass
        ui.shot(page, "grid-result")
        check("the finished Grid check shows its one-line summary on the page",
              summary.count() == 1 and summary.text_content() == SUMMARY,
              summary.text_content() if summary.count() else page.locator("#monitor").inner_text()[:300])
        link = page.locator("#jobExtras a", has_text="grid.json")
        check("... and a Download for grid.json", link.count() == 1)
        if link.count() == 1:
            with urllib.request.urlopen(url.rstrip("/") + link.get_attribute("href"), timeout=10) as r:
                check("that Download serves the job's grid.json", json.loads(r.read()).get("summary") == SUMMARY)
        check("the mp3 still plays on the stage", page.locator("#monitor audio").count() == 1)
        check("no page errors", not errors, errors)
        browser.close()
finally:
    ui.finish()
