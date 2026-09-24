"""Browser acceptance gate for portability fix P2 (D2 bug): the active lane
must follow the room, not stay wherever a previous room left it. Before this
fix, Make in Picture (or any room) blamed whatever lane was active last
(e.g. "Video Lane A"), even claiming lanes were "glowing green" when none
were.

Same shape as tests/test_sequence_ui.py (a real server.py subprocess,
scratch config/data, driven with a real browser) but with THREE lanes, one
per cap, ALL unreachable (nothing listening on their ports) -- the "lanes
down, one per cap" pattern -- and no lane at all declaring cap "3d", so all
three refusal shapes D2 describes are exercised:
  - a room whose only cap has an unreachable (but configured) lane
  - a DIFFERENT room, same shape, with a DIFFERENT cap word
  - a room whose cap no lane in config.json declares at all

RED on the pre-fix tree: the Picture room's refusal would still name
whatever lane the page happened to load with (e.g. "Video lane"), not
"image", and STATE.activeLane never changes on a plain room click.

Run: python3 tests/test_room_lane_follow.py
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

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http_json(url, timeout=3):
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


def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


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

# Three lanes, one per cap, each pointed at a port nothing listens on --
# "up" reads False for every one of them (the lane's own /system_stats
# never answers), but each still DECLARES its own cap in config.json.
pic_lane = {"id": "pic", "name": "Picture lane", "host": "127.0.0.1", "port": free_port(), "caps": ["image"]}
vid_lane = {"id": "vid", "name": "Video lane", "host": "127.0.0.1", "port": free_port(), "caps": ["video"]}
snd_lane = {"id": "snd", "name": "Sound lane", "host": "127.0.0.1", "port": free_port(), "caps": ["audio"]}
# Deliberately NO lane declares "3d" -- the third refusal shape.

tmp = tempfile.mkdtemp(prefix="bwf-room-lane-")
cfg_path = os.path.join(tmp, "config.json")
server_port = free_port()
with open(cfg_path, "w") as f:
    json.dump({
        "title": "Room Lane Follow Test",
        "port": server_port,
        "bind": "127.0.0.1",
        "lanes": [pic_lane, vid_lane, snd_lane],
        "timing": {"poll_seconds": 0.4, "job_poll_seconds": 5.0,
                   "http_timeout": 1.0, "discover_seconds": 300.0},
    }, f)
URL = "http://127.0.0.1:%d/" % server_port
logf = open(os.path.join(tmp, "server.log"), "w")
server = subprocess.Popen(
    [sys.executable, os.path.join(REPO, "server.py")],
    cwd=REPO,
    env=dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=os.path.join(tmp, "data")),
    stdout=logf, stderr=subprocess.STDOUT)


def server_answers():
    return bool(http_json(URL + "api/lanes").get("lanes"))


if not wait_true("server is up and lists its 3 lanes", server_answers, 30):
    stop(server); logf.close()
    with open(os.path.join(tmp, "server.log")) as f:
        print("  -- server.py said: %s" % f.read()[-800:])
    shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1)


def tab(page, rid):
    return '#roomStrip [data-room-id="%s"]' % rid


try:
    print("all 3 lanes read as down (nothing listens on any of their ports)")
    lanes = http_json(URL + "api/lanes")["lanes"]
    check("all 3 configured lanes are 'up': false", all(l["up"] is False for l in lanes), lanes)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append("PAGEERROR: %s" % e))
        page.goto(URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(800)

        print("Picture room: refusal names 'image', never a wrong-cap lane, and switches the active lane")
        page.click(tab(page, "picture"))
        page.wait_for_timeout(400)
        note = page.inner_text("#roomPlainNote")
        check("Picture refusal mentions 'image'", "image" in note, note)
        check("Picture refusal never names the video lane", vid_lane["name"] not in note, note)
        check("Picture refusal never names the sound lane", snd_lane["name"] not in note, note)
        check("Picture refusal never claims a lane is reachable ('glowing green'/'ready')",
              "green" not in note.lower() and "ready" not in note.lower(), note)
        active = page.evaluate("STATE.activeLane")
        check("active lane switched to the image-cap lane ('pic'), not left on a stale lane",
              active == "pic", active)

        print("Video room: refusal names 'video', never the picture/sound lane, and switches the active lane")
        page.click(tab(page, "video"))
        page.wait_for_timeout(400)
        note = page.inner_text("#roomPlainNote")
        check("Video refusal mentions 'video'", "video" in note, note)
        check("Video refusal never names the picture lane", pic_lane["name"] not in note, note)
        check("Video refusal never names the sound lane", snd_lane["name"] not in note, note)
        active = page.evaluate("STATE.activeLane")
        check("active lane switched to the video-cap lane ('vid')", active == "vid", active)

        print("3D room: no lane in config.json declares this cap at all -- the third refusal shape")
        page.click(tab(page, "3d"))
        page.wait_for_timeout(400)
        note = page.inner_text("#roomPlainNote")
        check("3D refusal says no machine in config.json can make this",
              "config.json" in note and "3d" in note, note)
        check("3D refusal never names any configured lane",
              all(l["name"] not in note for l in (pic_lane, vid_lane, snd_lane)), note)

        check("no page errors", not errors, errors)
        browser.close()
finally:
    stop(server)
    logf.close()
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILED:
    print("FAILED: %d: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All P2 (room-follows-lane) checks passed.")
