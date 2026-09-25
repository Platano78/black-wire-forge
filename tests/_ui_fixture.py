"""Shared by the P3d browser suites (tests/test_picture_ui.py, tests/test_object_ui.py):
a fake helper that answers from a reply queue, a fake ComfyUI lane holding one
finished picture, and server.py as a subprocess with a scratch config, scratch
data and the given saved jobs. SKIPs cleanly without Playwright/Chromium."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import _picture_server as ps  # noqa: E402
from _picture_server import FAILED, check, free_port  # noqa: E402,F401

try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    with sync_playwright() as _pw:
        _pw.chromium.launch().close()
except Exception as e:
    print("SKIP: playwright or its chromium is not installed (%s). pip install -r requirements-dev.txt "
          "&& python3 -m playwright install --with-deps chromium" % e)
    sys.exit(0)

PROCS = []
SCRATCH = tempfile.mkdtemp(prefix="bwf_p3d_ui_")
SHOTS = os.environ.get("BWF_P3D_SHOTS")


def wait_up(url, timeout=20):
    for _ in range(int(timeout * 10)):
        try:
            return urllib.request.urlopen(url, timeout=1)
        except Exception:
            time.sleep(0.1)
    raise SystemExit("did not come up: " + url)


def gradient_png(w, h):
    """A small RGB PNG (a colour gradient), so a result has a real picture to show."""
    import struct
    import zlib
    rows = b"".join(b"\x00" + bytes(v for x in range(w) for v in (x * 255 // w, y * 255 // h, 160)) for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def start(jobs, local_files=()):
    """-> the app's base URL. `local_files`: (job id, name, bytes) under data/outputs."""
    helper = ThreadingHTTPServer(("127.0.0.1", 0), ps.FakeHelper)
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    store = os.path.join(SCRATCH, "lane")
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    with open(os.path.join(store, "outputs", "fix.png"), "wb") as f:
        f.write(gradient_png(160, 120))
    lane_port, port = free_port(), free_port()
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port",
                                   str(lane_port), "--store", store], cwd=REPO,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    wait_up("http://127.0.0.1:%d/system_stats" % lane_port)
    data = os.path.join(SCRATCH, "data")
    os.makedirs(data, exist_ok=True)
    with open(os.path.join(data, "jobs.json"), "w") as f:
        json.dump(jobs, f)
    for jid, name, body in local_files:
        os.makedirs(os.path.join(data, "outputs", jid), exist_ok=True)
        with open(os.path.join(data, "outputs", jid, name), "wb") as f:
            f.write(body)
    cfg = os.path.join(SCRATCH, "config.json")
    with open(cfg, "w") as f:
        json.dump({"title": "p3d ui", "port": port, "bind": "127.0.0.1",
                   "lanes": [{"id": "t", "name": "Fake lane", "host": "127.0.0.1", "port": lane_port,
                              "caps": ["image", "video", "audio"]},
                             {"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}],
                   "helper": {"url": "http://127.0.0.1:%d/v1" % helper.server_address[1], "model": "test-model",
                              "timeout_s": 10, "vision": False},
                   "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0}}, f)
    env = dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=data)
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                  stdout=open(os.path.join(SCRATCH, "server.log"), "w"), stderr=subprocess.STDOUT))
    url = "http://127.0.0.1:%d/" % port
    wait_up(url + "api/health")
    return url


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, name + ".png"))


def finish():
    """Called from a `finally`: a crash counts as a failure, with its traceback."""
    if sys.exc_info()[0] is not None:
        import traceback
        traceback.print_exc()
        FAILED.append("the suite crashed")
    for p in PROCS:
        p.terminate()
    print("\nFAILED: %d" % len(FAILED) + (" checks: " + ", ".join(FAILED) if FAILED else ""))
    sys.exit(1 if FAILED else 0)
