"""A recording chosen for a shot reports its length and sizes the clip.

Server: an uploaded SOUND file's response carries `seconds` (additive; a picture's response is unchanged).
Page: choosing a recording on the Talking Head form's "Your own recording" field sets Length to the smallest 8n+1 frame
count that holds it (capped at 993), and a picture upload leaves Length alone.

Drives the real page in Playwright against its own fake lane and server.py subprocess; nothing reaches the live app.
Harness copied from the frozen tests/test_cut_ui.py. Run: python3 tests/test_recording_length_ui.py
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



import uuid

def make_wav(path, seconds, freq=440):
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=%d:sample_rate=44100:duration=%s" % (freq, seconds), path], check=True)


def api_upload(url, lane, filename, data, ctype):
    b = uuid.uuid4().hex
    body = (("--%s\r\nContent-Disposition: form-data; name=\"lane\"\r\n\r\n%s\r\n--%s\r\nContent-Disposition: form-data; name=\"file\"; "
             "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n" % (b, lane, b, filename, ctype)).encode() + data + ("\r\n--%s--\r\n" % b).encode())
    req = urllib.request.Request(url + "api/upload", data=body, headers={"Content-Type": "multipart/form-data; boundary=" + b})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


SCRATCH = tempfile.mkdtemp(prefix="bwf-reclen-")
if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
    print("SKIP: ffmpeg/ffprobe not on PATH")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(0)
WAV6, WAV2, WAV50 = (os.path.join(SCRATCH, n) for n in ("six.wav", "two.wav", "fifty.wav"))
make_wav(WAV6, 6); make_wav(WAV2, 2); make_wav(WAV50, 50)
FACE = os.path.join(SCRATCH, "face.png")
open(FACE, "wb").write(PNG_1X1)

try:
    fake_proc, fake_port, fake_store = start_fake_lane(SCRATCH, "rec")
    srv_proc, url, data_dir = start_server(SCRATCH, fake_port, "rec", None)

    print("server: sound uploads report their length; picture uploads are unchanged")
    r = api_upload(url, LANE_ID, "six.wav", open(WAV6, "rb").read(), "audio/wav")
    check("a 6 s wav upload reports seconds close to 6", r.get("ok") and abs(float(r["files"][0].get("seconds", 0)) - 6.0) < 0.05, r)
    r = api_upload(url, LANE_ID, "face.png", PNG_1X1, "image/png")
    check("a picture upload has no seconds key (response unchanged)", r.get("ok") and "seconds" not in r["files"][0]
          and set(r["files"][0]) == {"name", "original", "bytes"}, r)
    r = api_upload(url, LANE_ID, "notes.txt", b"hello", "text/plain")
    check("a non-sound file has no seconds key", r.get("ok") and "seconds" not in r["files"][0], r)

    print("page: choosing a recording sets Length")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1000)
        page.click('#roomStrip [data-room-id="talking"]')
        page.wait_for_timeout(800)
        check("the Talking Head form has the recording field", page.query_selector("#upload_audio_slice") is not None)
        length0 = page.eval_on_selector('#inspector [data-field-id="length"]', "el => Number(el.value)")
        check("Length starts at the 97-frame default", length0 == 97, length0)

        page.set_input_files("#upload_audio_slice", WAV6)
        page.wait_for_timeout(1500)
        l6 = page.eval_on_selector('#inspector [data-field-id="length"]', "el => Number(el.value)")
        check("a 6 s recording sets Length to 145 frames (6 s x 24 -> next 8n+1)", l6 == 145, l6)

        page.set_input_files("#upload_audio_slice", WAV2)
        page.wait_for_timeout(1500)
        l2 = page.eval_on_selector('#inspector [data-field-id="length"]', "el => Number(el.value)")
        check("a 2 s recording sets Length to 49 frames", l2 == 49, l2)

        page.set_input_files("#upload_audio_slice", WAV50)
        page.wait_for_timeout(1500)
        l50 = page.eval_on_selector('#inspector [data-field-id="length"]', "el => Number(el.value)")
        check("a 50 s recording is capped at 993 frames", l50 == 993, l50)
        msg = page.eval_on_selector("#inspectorMsg", "el => el.textContent")
        check("and says the rest is left out", "left out" in msg and "993 frames" in msg, msg)

        page.set_input_files("#upload_face", FACE)
        page.wait_for_timeout(1000)
        lf = page.eval_on_selector('#inspector [data-field-id="length"]', "el => Number(el.value)")
        check("choosing a picture leaves Length alone", lf == 993, lf)
        check("no page JS errors", not errors, errors)
        browser.close()
except Exception as e:
    check("the recording-length test ran without raising", False, explain(e, "rec"))
finally:
    for p in PROCS:
        stop(p)
    shutil.rmtree(SCRATCH, ignore_errors=True)

print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
