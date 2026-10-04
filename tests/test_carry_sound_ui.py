"""Browser gate for "Use one I already made" on a sound field: with a finished
sound in History the Producer room's sound field offers it, choosing it sends
one POST /api/carry/sound and the field shows the file the server stored; with
no finished sound in History the field is exactly as it was.

Same setup as tests/test_speech_ui.py: its own server.py subprocesses (scratch
config + scratch data, random free ports) with one process lane, and the
finished sound planted in the data dir the server picks up at start
("Picked up N earlier result(s)") as a `local` output under data/outputs/.

Run: python3 tests/test_carry_sound_ui.py
"""
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())

def wait_true(desc, fn, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(0.15)
    check(desc, False, "still false after %.0fs" % timeout)
    return False

def wav_bytes(seconds=0.5, rate=8000):
    pcm = bytes(int(rate * seconds) * 2)
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed. pip install -r requirements-dev.txt "
          "&& python3 -m playwright install --with-deps chromium")
    sys.exit(0)

try:
    with sync_playwright() as _pw_probe:
        _pw_probe.chromium.launch().close()
except Exception as _pw_err:
    print("SKIP: playwright's chromium browser is not installed (%s). "
          "Run: python3 -m playwright install --with-deps chromium" % _pw_err)
    sys.exit(0)


SCRATCH = tempfile.mkdtemp(prefix="bwf_carry_sound_ui_")
PROCS = []
WAV = wav_bytes()
JOBS = [{"id": "songjob1", "lane": "cpu", "lane_name": "This machine", "kind": "audio",
         "mode": "song", "status": "done", "prompt": "a slow country song with pedal steel",
         "seed": 3, "created": time.time() - 60,
         "outputs": [{"filename": "take.wav", "subfolder": "songjob1", "type": "local",
                      "media": "audio"}]}]


def start_server(name, jobs):
    port = free_port()
    data = os.path.join(SCRATCH, "data_" + name)
    os.makedirs(data, exist_ok=True)
    with open(os.path.join(data, "jobs.json"), "w") as f:
        json.dump(jobs, f)
    outdir = os.path.join(data, "outputs", "songjob1")
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "take.wav"), "wb") as f:
        f.write(WAV)
    cfg = {"title": "carry sound ui", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "cpu", "name": "This machine", "kind": "process",
                      "caps": ["producer"]}],
           "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0, "http_timeout": 5.0}}
    cfg_path = os.path.join(SCRATCH, "config_%s.json" % name)
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data)
    logf = open(os.path.join(SCRATCH, "server_%s.log" % name), "w")
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                  stdout=logf, stderr=subprocess.STDOUT))
    url = "http://127.0.0.1:%d/" % port
    if not wait_true("server %s is up" % name, lambda: http_json(url + "api/health").get("ok"), 30):
        with open(os.path.join(SCRATCH, "server_%s.log" % name)) as f:
            print("  -- server %s log tail: %s" % (name, f.read()[-1200:]))
        raise SystemExit("server %s did not come up" % name)
    return url


def open_producer(page, url):
    page.goto("about:blank")
    page.goto(url + "#room=producer", wait_until="networkidle", timeout=30000)
    page.wait_for_function("() => typeof STATE !== 'undefined' && !!STATE.activeLane", timeout=15000)
    page.wait_for_timeout(1500)
    if page.query_selector("#upload_track_1") is None:
        page.locator("#engineChipBtn").click()
        page.wait_for_timeout(1000)
        row = page.locator("#enginePicker label.engine-row", has_text="Mix tracks").first
        row.wait_for(state="visible", timeout=15000)
        try:
            page.wait_for_function(
                "() => { const r = [...document.querySelectorAll('#enginePicker label.engine-row')]"
                " .find(x => x.textContent.includes('Mix tracks'));"
                " return !!r && !r.querySelector('input').disabled; }", timeout=15000)
        except Exception:
            check("the Mix row becomes pickable", False, page.eval_on_selector_all(
                "#enginePicker label.engine-row", "els => els.map(e => e.textContent)"))
            raise
        row.click()
    page.wait_for_selector("#upload_track_1", timeout=15000)
    page.wait_for_timeout(500)


try:
    url_on = start_server("on", JOBS)
    url_off = start_server("off", [])
    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        print("a finished sound in History: the sound field offers it")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        posts = []
        page.on("request", lambda r: posts.append((r.url, r.post_data)) if r.url.endswith("/api/carry/sound") else None)
        open_producer(page, url_on)
        sel = "#reuse_track_1"
        check("the field has a 'Use one I already made' picker", page.is_visible(sel))
        opts = page.eval_on_selector_all(sel + " option", "els => els.map(e => [e.value, e.textContent])")
        check("its first option asks which one", opts and opts[0][0] == "", opts)
        check("the finished song is offered as job:output",
              ["songjob1:0"] == [o[0] for o in opts[1:]], opts)
        check("named the way its mode and prompt read",
              bool(opts[1][1]) and "a slow country song"[:40] in opts[1][1], opts)

        print("choosing it: one POST, and the file lands in the field")
        page.select_option(sel, "songjob1:0")
        page.wait_for_function(
            "() => document.querySelector('#inspectorMsg').textContent.startsWith('That sound is now under')",
            timeout=15000)
        check("exactly one POST /api/carry/sound went out",
              len(posts) == 1 and json.loads(posts[0][1] or "{}") == {"job_id": "songjob1", "output": 0,
                                                                       "lane": "cpu"}, posts)
        check("the page says the sound is under the field",
              page.inner_text("#inspectorMsg") == "That sound is now under Track 1.",
              page.inner_text("#inspectorMsg"))
        held = page.evaluate("() => STATE.uploads.track_1 || []")
        check("the field now holds the file the server stored, the way an upload shows",
              len(held) == 1 and held[0].get("original") == "take.wav"
              and page.inner_text('[data-ledger-row="track_1"] .ref-tag') == "1 loaded", held)
        check("the picker is back to asking", page.eval_on_selector(sel, "e => e.value") == "")
        check("no page JS errors", errors == [], errors)
        page.close()

        print("no finished sound in History: the field is exactly as it was")
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        off_errors = []
        page.on("pageerror", lambda e: off_errors.append(str(e)))
        open_producer(page, url_off)
        check("no reuse picker anywhere in the room",
              page.evaluate("() => document.querySelectorAll('[id^=\"reuse_\"]').length") == 0,
              page.evaluate("() => document.querySelectorAll('[id^=\"reuse_\"]').length"))
        check("the sound field's own file picker is untouched", page.is_visible("#upload_track_1"))
        check("no page JS errors", off_errors == [], off_errors)
        page.close()
        browser.close()
finally:
    for p in PROCS:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
