"""Browser check for the opt-in "Audio-led" checkbox in the Cutting Room's cut bar.

Off by default and invisible to the classic wording: with the box unticked the bar says "Music bed: ..." exactly as
before. Ticking it sends op set_audio_led, the sequence stores audio_led, the bar says "Master sound: ...", and the
state survives a reload. Unticking removes the key again. Drives the real page in Playwright against its own fake lane
and server.py subprocess (scratch config and data, random ports); nothing reaches the live app.

The harness block is copied from tests/test_cut_ui.py (a frozen file that cannot be imported). Run:
python3 tests/test_audio_led_ui.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
LANE_ID = "t"

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

def http_json(url, timeout=5, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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

def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    # Finding #21: absence of an OPTIONAL dev dependency is a SKIP, not a
    # failure -- a clean clone with no playwright must not read as broken.
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

PROCS = []

def start_fake_lane(scratch, name, output_clip_path=None):
    store = os.path.join(scratch, "fake_%s" % name)
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    if output_clip_path:
        # Always served under "ui_ready.mp4" -- the one filename
        # build_sequence_with_a_picked_shot() arms /_control/accept with.
        shutil.copy(output_clip_path, os.path.join(store, "outputs", "ui_ready.mp4"))
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(port), "--store", store],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(proc)
    ok = wait_true("fake lane %s answers /system_stats" % name,
                    lambda: http_json("http://127.0.0.1:%d/system_stats" % port), 15)
    if not ok:
        raise SystemExit("fake lane %s did not come up" % name)
    return proc, port, store


def start_server(scratch, lane_port, name, path_value, cut_config=None):
    data_dir = os.path.join(scratch, "data_%s" % name)
    cfg_path = os.path.join(scratch, "config_%s.json" % name)
    port = free_port()
    cfg = {"title": "C3.6 UI test", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": LANE_ID, "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                      "caps": ["image", "video", "audio"]}],
           "timing": {"poll_seconds": 0.4, "job_poll_seconds": 1.0, "http_timeout": 4.0,
                      "free_settle_seconds": 1.0, "discover_seconds": 300.0}}
    if cut_config is not None:
        cfg["cut"] = cut_config
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    logf = open(os.path.join(scratch, "server_%s.log" % name), "w")
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data_dir)
    if path_value is not None:
        env["PATH"] = path_value
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                             stdout=logf, stderr=subprocess.STDOUT)
    PROCS.append(proc)
    url = "http://127.0.0.1:%d/" % port

    def lane_up():
        d = http_json(url + "api/lanes")
        lanes = d.get("lanes") if isinstance(d, dict) else d
        l = next((x for x in (lanes or []) if x.get("id") == LANE_ID), None)
        return bool(l and l.get("up") and l.get("discovered"))  # discovered: generate before discovery is refused (503)

    ok = wait_true("server %s is up and the fake lane is up" % name, lane_up, 30)
    if not ok:
        with open(os.path.join(scratch, "server_%s.log" % name)) as f:
            print("  -- server %s log tail: %s" % (name, f.read()[-1200:]))
    return proc, url, data_dir


def open_cutting_room_with_a_sequence(page, url, title):
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(800)
    page.click('#roomStrip [data-room-id="cutting"]')
    page.wait_for_timeout(300)
    page.fill("#seqNewTitle", title)
    page.click("#seqNewBtn")
    page.wait_for_timeout(500)
    h = page.evaluate("location.hash")
    check("[%s] URL carries #room=cutting&seq=<id> (really in the Cutting Room, not stuck on the picker)"
          % title, "room=cutting" in h and "seq=s_" in h, h)
    return h.split("seq=")[1].split("&")[0] if "seq=s_" in h else None


def http_post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c4944415478da6360606060000000050001a5f645400000000049454e44ae426082")


def make_tiny_mp4(path):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                     "-f", "lavfi", "-i", "color=c=red:size=320x240:rate=24:duration=1",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path], check=True)


def explain(e, name):
    """An HTTPError's own body and URL, plus the server's log tail -- so an
    intermittent setup failure says what the server refused and why."""
    body = ""
    if hasattr(e, "read"):
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
    tail = ""
    try:
        with open(os.path.join(SCRATCH, "server_%s.log" % name)) as f:
            tail = f.read()[-800:]
    except OSError:
        pass
    return "%r url=%s body=%s\n  -- server %s log tail:\n%s" % (e, getattr(e, "url", ""), body, name, tail)


def build_sequence_with_a_picked_shot(url, lane_port):
    """API-only setup (this file's own concern is the CUT button's state
    given a real picked shot, not the make-a-shot flow -- that is
    tests/test_sequence_ui.py's job): create a sequence, add one video slot,
    render it against the fake lane, pick it."""
    seq = http_post_json(url + "api/sequence", {"title": "UI ready", "mode": "sequence"})
    sid, rev = seq["id"], seq["rev"]
    body = http_post_json(url + "api/sequence/op",
                           {"id": sid, "rev": rev, "op": "add_slot", "lane": "video", "cap": "video",
                            "mode": "ltx", "values": {"prompt": "a shot", "length": 121}})
    rev = body["rev"]
    slot_id = body["slots"][0]["id"]
    http_post_json("http://127.0.0.1:%d/_control/accept" % lane_port,
                    {"outputs": [{"filename": "ui_ready.mp4", "subfolder": "", "type": "output"}]})
    gbody = http_post_json(url + "api/sequence/generate", {"id": sid, "slot_id": slot_id})
    job_id = gbody["job"]["id"]

    def harvested():
        d = http_json(url + "api/sequence?id=" + sid)
        slot = next(s for s in d["slots"] if s["id"] == slot_id)
        take = next((t for t in slot["takes"] if t["job_id"] == job_id), None)
        return take and take.get("file")

    wait_true("(setup) take harvested for the UI-ready sequence", harvested, 20)
    d = http_json(url + "api/sequence?id=" + sid)
    http_post_json(url + "api/sequence/op",
                    {"id": sid, "rev": d["rev"], "op": "pick_take", "slot_id": slot_id, "job_id": job_id})
    http_post_json("http://127.0.0.1:%d/_control/accept" % lane_port, {"outputs": None})
    return sid



SCRATCH = tempfile.mkdtemp(prefix="bwf-ledui-")
TINY_MP4 = os.path.join(SCRATCH, "tiny.mp4")
if not shutil.which("ffmpeg"):
    print("SKIP: ffmpeg not on PATH -- needed to build this suite's own fixture clip")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(0)
make_tiny_mp4(TINY_MP4)

try:
    fake_proc, fake_port, fake_store = start_fake_lane(SCRATCH, "led", output_clip_path=TINY_MP4)
    srv_proc, url, data_dir = start_server(SCRATCH, fake_port, "led", None)
    sid = build_sequence_with_a_picked_shot(url, fake_port)
    check("(setup) the sequence starts classic: no audio_led key", "audio_led" not in http_json(url + "api/sequence?id=" + sid))

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url + "#room=cutting&seq=%s" % sid, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1200)
        box = page.query_selector("#audioLedBox")
        check("the Audio-led checkbox is in the cut bar", box is not None)
        check("it is unticked by default", box is not None and not page.eval_on_selector("#audioLedBox", "el => el.checked"))
        bed = page.eval_on_selector("#cutBedNote", "el => el.innerText").strip()
        check("classic wording is unchanged: the bed note starts 'Music bed:'", bed.startswith("Music bed:"), bed)
        check("the cut button and its reason element are still there",
              page.query_selector("[data-cut-button]") is not None and page.query_selector("[data-cut-reason]") is not None)

        page.click("#audioLedBox")
        page.wait_for_timeout(1200)
        stored = http_json(url + "api/sequence?id=" + sid)
        check("ticking stores audio_led on the sequence", stored.get("audio_led") is True, stored.get("audio_led"))
        check("the box stays ticked after the re-render", page.eval_on_selector("#audioLedBox", "el => el.checked"))
        bed = page.eval_on_selector("#cutBedNote", "el => el.innerText").strip()
        check("the note now says 'Master sound:'", bed.startswith("Master sound:"), bed)

        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        check("the choice survives a reload", page.eval_on_selector("#audioLedBox", "el => el.checked"))

        page.click("#audioLedBox")
        page.wait_for_timeout(1200)
        stored = http_json(url + "api/sequence?id=" + sid)
        check("unticking removes the key (back to classic)", "audio_led" not in stored, stored.get("audio_led"))
        bed = page.eval_on_selector("#cutBedNote", "el => el.innerText").strip()
        check("and the wording returns to 'Music bed:'", bed.startswith("Music bed:"), bed)
        check("no page JS errors", not errors, errors)
        browser.close()
except Exception as e:
    check("the audio-led UI test ran without raising", False, explain(e, "led"))
finally:
    for p in PROCS:
        stop(p)
    shutil.rmtree(SCRATCH, ignore_errors=True)

print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
