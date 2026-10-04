"""API tests for POST /api/carry/sound -- "Use one I already made" for a
sound field: a finished sound output of a job goes onto a lane as a sound
field's input, stored the way an upload of that sound would be.

server.py runs in-process against a scratch config with a PROCESS lane (the
Producer room's own machine), seeded with a finished audio job whose output is
a real 0.5 s wav under data/outputs/<job id>/ (the shape a `local` output
takes, the one path _carry_source_bytes reads without a lane).

Run: python3 tests/test_carry_sound.py
"""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
import http.server

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import _scratch_config  # noqa: E402,F401  # GENCENTER_CONFIG/GENCENTER_DATA, before server.py

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (
        ("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wav_bytes(seconds=0.5, rate=8000):
    """A real 0.5 s 8 kHz mono 16-bit wav of silence."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
        path = tf.name
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(int(rate * seconds) * 2))
    with open(path, "rb") as f:
        data = f.read()
    os.unlink(path)
    return data


SCRATCH = tempfile.mkdtemp(prefix="bwf_carry_sound_")
app_port = free_port()
CFG_PATH = os.path.join(SCRATCH, "config.json")
json.dump({"port": app_port, "bind": "127.0.0.1", "title": "carry sound test",
           "lanes": [{"id": "cpu", "name": "This machine", "kind": "process",
                      "caps": ["producer", "audio"]}],
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0}},
          open(CFG_PATH, "w"))
os.environ["GENCENTER_CONFIG"] = CFG_PATH
DATA = os.path.join(SCRATCH, "data")
os.makedirs(DATA, exist_ok=True)
os.environ["GENCENTER_DATA"] = DATA

import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("srv", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

httpd = http.server.HTTPServer(("127.0.0.1", app_port), srv.Handler)
httpd.timeout = 0.5
threading.Thread(target=httpd.serve_forever, daemon=True).start()
for _ in range(50):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/api/health" % app_port, timeout=0.5)
        break
    except Exception:
        time.sleep(0.05)


def post(body, path="/api/carry/sound"):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (app_port, path),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode()), resp.getcode()
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode()), e.code


WAV = wav_bytes()
job_dir = os.path.join(DATA, "outputs", "song1")
os.makedirs(job_dir, exist_ok=True)
with open(os.path.join(job_dir, "take.wav"), "wb") as f:
    f.write(WAV)

with srv.JOBS_LOCK:
    srv.JOBS["song1"] = {"id": "song1", "lane": "cpu", "kind": "audio", "mode": "song",
                         "status": "done", "prompt": "a country song",
                         "outputs": [{"filename": "take.wav", "subfolder": "song1", "type": "local",
                                      "media": "audio"}],
                         "created": time.time()}
    srv.JOBS["pic1"] = {"id": "pic1", "lane": "cpu", "kind": "image", "mode": "t2i",
                        "status": "done", "prompt": "a picture",
                        "outputs": [{"filename": "p.png", "subfolder": "", "type": "output",
                                     "media": "image"}],
                        "created": time.time()}
    srv.JOBS["running1"] = {"id": "running1", "lane": "cpu", "kind": "audio", "mode": "song",
                            "status": "running", "outputs": [], "created": time.time()}

GOOD = {"job_id": "song1", "output": 0, "lane": "cpu"}

print("\n--- a finished sound goes into a sound field ---")
p, c = post(GOOD)
check("ok", c == 200 and p.get("ok") is True, (c, p))
files = p.get("files") or []
check("one file, the /api/upload shape", len(files) == 1 and set(files[0]) >= {"name", "original", "bytes"}, files)
name = files[0].get("name") or ""
path = os.path.join(srv.UPLOADS_DIR, "cpu", name)
check("the file landed under UPLOADS_DIR/<lane id>",
      bool(name) and os.path.isfile(path) and open(path, "rb").read() == WAV, path)
check("its original name is the output's own filename", files[0].get("original") == "take.wav", files[0])
check("it carries the sound's length", abs(float(files[0].get("seconds", 0)) - 0.5) < 0.05, files[0])

print("\n--- refusals ---")
p, c = post({"job_id": "pic1", "output": 0, "lane": "cpu"})
check("a picture is not a sound",
      c == 400 and p.get("error") == "Only a finished sound can go in a sound field.", (c, p))
p, c = post({"job_id": "running1", "output": 0, "lane": "cpu"})
check("a running job is refused",
      c == 400 and p.get("error") == "That result is not finished yet.", (c, p))
p, c = post({"job_id": "nope", "output": 0, "lane": "cpu"})
check("an unknown job is refused",
      c == 404 and p.get("error") == "I cannot find that result any more.", (c, p))
p, c = post({"job_id": "song1", "output": 5, "lane": "cpu"})
check("an output number out of range is refused", c == 400 and p.get("ok") is False, (c, p))
p, c = post({"job_id": "song1", "output": 0, "lane": "nosuch"})
check("an unknown lane is refused", c == 400 and "unknown lane" in p.get("error", ""), (c, p))
before = len(os.listdir(os.path.join(srv.UPLOADS_DIR, "cpu")))
post({"job_id": "pic1", "output": 0, "lane": "cpu"})
check("a refused sound stores nothing", len(os.listdir(os.path.join(srv.UPLOADS_DIR, "cpu"))) == before)

print()
if FAILED:
    print("FAILED (%d): %s" % (len(FAILED), ", ".join(FAILED)))
else:
    print("All tests passed.")
sys.exit(1 if FAILED else 0)
