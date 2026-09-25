"""Shared by the P3d suites (tests/test_picture_*.py): a fake OpenAI-compatible
helper that records every request, a fake ComfyUI lane holding one finished
picture, and server.py loaded in process against both (helper vision off)."""
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                    "0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082")
HELPER_STATE = {"requests": [], "replies": []}
FAILED = []
# A t2i prompt and a 3D source-picture prompt that meet every rule.
GOOD = ("A realistic cinematic photograph of exactly two giant monsters fighting in a ruined city at night. On the left "
        "a huge upright grey reptilian kaiju with jagged pale dorsal plates roars; on the right a golden three-headed "
        "mechanical dragon with metal bat wings, armour plates and glowing red eyes rears up. Rubble and fire fill the "
        "foreground. Harsh orange fire-glow lights both from below.")
SRC = ("A clean product photograph of one brass lantern, the whole lantern in frame with space all round it, seen from "
       "a three-quarter view. Soft, even studio light. A plain dark grey background.")


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


class FakeHelper(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(404, {"error": "no"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        HELPER_STATE["requests"].append(json.loads(self.rfile.read(n) or b"{}"))
        reply = HELPER_STATE["replies"].pop(0) if HELPER_STATE["replies"] else "PROMPT: x"
        self._send(200, {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}]})

    def log_message(self, *a):
        pass


def start(name):
    """-> (server module, fake lane's store dir, fake lane process)."""
    helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelper)
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    scratch = tempfile.mkdtemp(prefix="bwf_%s_" % name)
    store = os.path.join(scratch, "fake_lane")
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    with open(os.path.join(store, "outputs", "result.png"), "wb") as f:
        f.write(PNG)
    lane_port = free_port()
    lane = subprocess.Popen([sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port",
                             str(lane_port), "--store", store], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % lane_port, timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    cfg_path = os.path.join(scratch, "config.json")
    with open(cfg_path, "w") as f:
        json.dump({"port": free_port(), "bind": "127.0.0.1", "title": name,
                   "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
                   "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": lane_port,
                              "caps": ["image", "video", "audio"]},
                             {"id": "off", "name": "Off lane", "host": "127.0.0.1", "port": 1, "caps": ["image"]},
                             {"id": "cpu", "name": "This machine", "kind": "process", "caps": ["3d"]}],
                   "helper": {"url": "http://127.0.0.1:%d/v1" % helper.server_address[1],
                              "model": "test-model", "timeout_s": 5, "vision": False}}, f)
    os.environ["GENCENTER_CONFIG"] = cfg_path
    os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
    spec = importlib.util.spec_from_file_location("srv_" + name, os.path.join(ROOT, "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    return srv, store, lane
