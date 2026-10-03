"""Speech source (opt-in) route: POST /api/speech + GET /api/speech/status.

Off by default: with no speech config the server 404s POST /api/speech,
GET /api/speech/status reports enabled: false, and /api/upload is
unchanged. With a speech config the route calls an OpenAI-style
/audio/speech endpoint and stores the wav like /api/upload does.

Runs a tiny fake TTS server (127.0.0.1, random port) that records the
request it got and returns a real 1 s wav generated with wave.
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave as _wave

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Finding #21: ffmpeg/ffprobe are optional deps of the app itself --
# their absence is a SKIP, not a build failure.
if not (os.path.exists("/usr/bin/ffmpeg") or os.path.exists("/usr/local/bin/ffmpeg")):
    # Be lenient: check PATH like test_audio_led.py does.
    import shutil as _shutil
    if not (_shutil.which("ffmpeg") and _shutil.which("ffprobe")):
        print("SKIP: ffmpeg/ffprobe not on PATH -- this suite needs both")
        sys.exit(0)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)

def note(msg):
    print("  ....  " + msg)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

def http_json(url, data=None, method=None, timeout=10, headers=None):
    hdrs = {"Content-Type": "application/json"} if data is not None else {}
    if headers:
        hdrs.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null"), dict(e.headers)
        except Exception:
            return e.code, None, {}

def wait_true(desc, fn, timeout, interval=0.2):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
            if last:
                return True, last
        except Exception as e:
            last = e
        time.sleep(interval)
    check(desc, False, "still false after %.0fs (last=%r)" % (timeout, last))
    return False, last

PROCS = []
def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ---------------------------------------------------------------------------
# Fake TTS server: records the request, returns a real 1 s WAV.
# ---------------------------------------------------------------------------
def _make_wav():
    """Return bytes of a 1 s 16 kHz mono 16-bit WAV sine wave."""
    buf = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    wav_path = buf.name
    buf.close()
    with _wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        import struct
        frames = b""
        for i in range(16000):
            import math
            v = int(32767 * 0.3 * math.sin(2 * math.pi * 440 * i / 16000))
            frames += struct.pack("<h", v)
        wf.writeframes(frames)
    with open(wav_path, "rb") as f:
        data = f.read()
    os.unlink(wav_path)
    return data

class FakeTTSHandler:
    """In-process fake TTS: records last request, serves 1 s wav."""
    def __init__(self):
        self.last = {"method": None, "path": None, "headers": {}, "body": None}
        self._wav = _make_wav()

    def handle(self, req):
        self.last["method"] = req.command
        self.last["path"] = req.path
        self.last["headers"] = dict(req.headers)
        length = int(req.headers.get("Content-Length", 0))
        if length:
            self.last["body"] = req.rfile.read(length)
        else:
            self.last["body"] = b""
        if req.command == "POST" and req.path.endswith("/audio/speech"):
            req.send_response(200)
            req.send_header("Content-Type", "audio/wav")
            req.send_header("Content-Length", str(len(self._wav)))
            req.end_headers()
            req.wfile.write(self._wav)
            return
        req.send_response(404)
        req.end_headers()

class FakeTTSServer(threading.Thread):
    """Threaded HTTPServer wrapper so we can kill it cleanly."""
    def __init__(self, host, port, handler):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.handler = handler
        self.server = None

    def run(self):
        from http.server import HTTPServer, BaseHTTPRequestHandler
        class Adaptor(BaseHTTPRequestHandler):
            wav_handler = None  # set before serve_forever
            def do_POST(self):
                self.wav_handler.handle(self)
            def do_GET(self):
                self.wav_handler.handle(self)
            def log_message(self, fmt, *args):
                pass
        Adaptor.wav_handler = self.handler
        self.server = HTTPServer((self.host, self.port), Adaptor)
        self.server.serve_forever()

    def stop(self):
        if self.server is not None:
            self.server.shutdown()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
def start_fake_lane(scratch, name):
    store = os.path.join(scratch, "fake_%s" % name)
    os.makedirs(os.path.join(store, "outputs"), exist_ok=True)
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "fixtures", "fake_comfy.py"), "--port", str(port), "--store", store],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(proc)
    url = "http://127.0.0.1:%d/system_stats" % port
    ok, _ = wait_true("fake lane %s answers /system_stats" % name,
                      lambda: http_json(url)[0] == 200, 15)
    if not ok:
        raise SystemExit("fake lane %s did not come up" % name)
    return proc, port, store

def start_server(scratch, lane_port, tts_port, api_key=None):
    data_dir = os.path.join(scratch, "data_speech")
    cfg_path = os.path.join(scratch, "config_speech.json")
    port = free_port()
    lanes = [{"id": "t", "name": "Target lane", "host": "127.0.0.1", "port": lane_port,
              "caps": ["image", "video", "audio"]}]
    cfg = {"title": "speech test", "port": port, "bind": "127.0.0.1", "lanes": lanes,
           "timing": {"poll_seconds": 0.3, "job_poll_seconds": 0.3,
                      "http_timeout": 5.0, "free_settle_seconds": 1.0, "discover_seconds": 300.0},
           "speech": {"url": "http://127.0.0.1:%d" % tts_port, "model": "tts-1", "voice": "alloy",
                      "api_key_env": "SPEECH_API_KEY_TEST", "timeout": 10}}
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data_dir)
    if api_key is not None:
        env["SPEECH_API_KEY_TEST"] = api_key
    logf = open(os.path.join(scratch, "server_speech.log"), "w")
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                             stdout=logf, stderr=subprocess.STDOUT)
    PROCS.append(proc)
    url = "http://127.0.0.1:%d/" % port
    ok, _ = wait_true("server is up", lambda: http_json(url + "api/health")[0] == 200, 20)
    if not ok:
        logf.flush()
        with open(os.path.join(scratch, "server_speech.log")) as f:
            note("server log tail: %s" % f.read()[-1500:])
    return proc, url, data_dir, logf


if __name__ == "__main__":
    SCRATCH = tempfile.mkdtemp(prefix="bwf_speech_")
    FAKE_LANE_PROC, FAKE_LANE_PORT, FAKE_LANE_STORE = start_fake_lane(SCRATCH, "t")
    TTS = FakeTTSHandler()
    TTS_PORT = free_port()
    TTS_THREAD = FakeTTSServer("127.0.0.1", TTS_PORT, TTS)
    TTS_THREAD.start()
    time.sleep(0.3)
    SERVER_PROC, URL, DATA_DIR, SERVER_LOG = start_server(SCRATCH, FAKE_LANE_PORT, TTS_PORT, api_key="tok-secret-123")

    # 1. disabled by default: config with no speech key
    DATA_DIR_OFF = os.path.join(SCRATCH, "data_off")
    CFG_OFF = os.path.join(SCRATCH, "config_off.json")
    with open(CFG_OFF, "w") as f:
        json.dump({"title": "off", "port": free_port(), "bind": "127.0.0.1",
                   "lanes": [{"id": "t", "name": "Target lane", "host": "127.0.0.1", "port": FAKE_LANE_PORT,
                              "caps": ["image", "video", "audio"]}],
                   "timing": {"poll_seconds": 0.3}}, f)
    env = dict(os.environ, GENCENTER_CONFIG=CFG_OFF, GENCENTER_DATA=DATA_DIR_OFF)
    OFF_PROC = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    PROCS.append(OFF_PROC)
    time.sleep(1.5)
    OFF_URL = "http://127.0.0.1:%d/" % (json.load(open(CFG_OFF))["port"],)
    # We need the off-server port - read it from cfg
    off_cfg = json.load(open(CFG_OFF))
    OFF_URL = "http://127.0.0.1:%d/" % off_cfg["port"]
    ok, _ = wait_true("off server is up", lambda: http_json(OFF_URL + "api/health")[0] == 200, 15)

    # --- disabled by default checks ---
    code, body, _ = http_json(OFF_URL + "api/speech", data={"lane": "t", "text": "hello"}, method="POST")
    check("POST /api/speech 404 when disabled", code == 404 and isinstance(body, dict) and body.get("ok") is False)
    code, body, _ = http_json(OFF_URL + "api/speech/status")
    check("GET /api/speech/status reports enabled false when disabled", code == 200 and body == {"enabled": False})
    # /api/upload response shape unchanged when disabled (basic sanity)
    code_u, body_u, _ = http_json(OFF_URL + "api/upload", data=None, method="GET")
    # GET /api/upload without multipart returns a dict - just check server answers (byte-identical shape: dict with ok or error)
    check("GET /api/upload returns a dict (not 500 crash) when disabled", isinstance(body_u, dict))

    # --- enabled happy path ---
    code, body, _ = http_json(URL + "api/speech", data={"lane": "t", "text": "hello world"})
    check("POST /api/speech returns ok=true", code == 200 and body.get("ok") is True, body)
    files = body.get("files", []) if isinstance(body, dict) else []
    check("files[0] present", len(files) == 1)
    if files:
        f = files[0]
        check("files[0] has name", isinstance(f.get("name"), str) and len(f["name"]) > 0)
        check("files[0] original=speech.wav", f.get("original") == "speech.wav")
        check("files[0] bytes > 0", isinstance(f.get("bytes"), int) and f["bytes"] > 0)
        check("files[0] seconds ~1.0", isinstance(f.get("seconds"), (int, float)) and abs(f["seconds"] - 1.0) < 0.2)
    check("fake TTS saw POST /audio/speech", TTS.last["path"] == "/audio/speech")
    check("fake TTS saw model=tts-1", json.loads(TTS.last["body"]).get("model") == "tts-1")
    check("fake TTS saw voice=alloy", json.loads(TTS.last["body"]).get("voice") == "alloy")
    check("fake TTS saw input text", json.loads(TTS.last["body"]).get("input") == "hello world")
    check("fake TTS saw response_format=wav", json.loads(TTS.last["body"]).get("response_format") == "wav")
    check("fake TTS saw Bearer header", TTS.last["headers"].get("Authorization", "").startswith("Bearer tok-secret-123"))

    # --- voice override ---
    TTS.last = {"method": None, "path": None, "headers": {}, "body": None}
    code, body, _ = http_json(URL + "api/speech", data={"lane": "t", "text": "x", "voice": "echo"})
    check("voice override sent to TTS", json.loads(TTS.last["body"]).get("voice") == "echo")

    # --- instructions forwarded ---
    TTS.last = {"method": None, "path": None, "headers": {}, "body": None}
    code, body, _ = http_json(URL + "api/speech", data={"lane": "t", "text": "x", "instructions": "speak softly"})
    check("instructions forwarded", json.loads(TTS.last["body"]).get("instructions") == "speak softly")

    # --- empty text 400 ---
    code, body, _ = http_json(URL + "api/speech", data={"lane": "t", "text": "   "})
    check("empty/whitespace text -> 400", code == 400)

    # --- 2001-char text 400 ---
    code, body, _ = http_json(URL + "api/speech", data={"lane": "t", "text": "a" * 2001})
    check("2001-char text -> 400", code == 400)

    # --- unknown lane 400 ---
    code, body, _ = http_json(URL + "api/speech", data={"lane": "nope", "text": "hello"})
    check("unknown lane -> 400", code == 400 and "unknown lane" in str(body))

    # --- upstream 500 -> 502, no token in body ---
    TTS.last = {"method": None, "path": None, "headers": {}, "body": None}
    # Replace TTS handler temporarily with one that returns 500
    class ErrorTTSHandler(FakeTTSHandler):
        def handle(self, req):
            self.last["method"] = req.command
            self.last["path"] = req.path
            self.last["headers"] = dict(req.headers)
            length = int(req.headers.get("Content-Length", 0))
            self.last["body"] = req.rfile.read(length) if length else b""
            req.send_response(500)
            req.send_header("Content-Type", "text/plain")
            req.end_headers()
            req.wfile.write(b"boom")
    ERR_TTS = ErrorTTSHandler()
    ERR_PORT = free_port()
    ERR_THREAD = FakeTTSServer("127.0.0.1", ERR_PORT, ERR_TTS)
    ERR_THREAD.start()
    time.sleep(0.2)
    # Start a one-off server pointing at error TTS
    ERR_SCRATCH = tempfile.mkdtemp(prefix="bwf_speech_err_")
    ERR_CFG = os.path.join(ERR_SCRATCH, "config.json")
    ERR_DATA = os.path.join(ERR_SCRATCH, "data")
    os.makedirs(ERR_DATA)
    with open(ERR_CFG, "w") as f:
        json.dump({"title": "err", "port": free_port(), "bind": "127.0.0.1",
                   "lanes": [{"id": "t", "name": "Target lane", "host": "127.0.0.1", "port": FAKE_LANE_PORT,
                              "caps": ["image", "video", "audio"]}],
                   "timing": {"poll_seconds": 0.3},
                   "speech": {"url": "http://127.0.0.1:%d" % ERR_PORT}}, f)
    eproc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO,
                              env=dict(os.environ, GENCENTER_CONFIG=ERR_CFG, GENCENTER_DATA=ERR_DATA),
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    PROCS.append(eproc)
    eurl = "http://127.0.0.1:%d/" % json.load(open(ERR_CFG))["port"]
    ok, _ = wait_true("err server up", lambda: http_json(eurl + "api/health")[0] == 200, 15)
    code, body, _ = http_json(eurl + "api/speech", data={"lane": "t", "text": "hello"})
    check("upstream 500 -> 502", code == 502 and body.get("ok") is False)
    check("502 body has no bearer token", isinstance(body.get("detail"), str) and "tok-secret-123" not in body.get("detail", ""))

    # --- upstream non-audio bytes -> 502 ---
    class RawTTSHandler(FakeTTSHandler):
        def handle(self, req):
            req.send_response(200)
            req.send_header("Content-Type", "application/octet-stream")
            req.send_header("Content-Length", "4")
            req.end_headers()
            req.wfile.write(b"not wav")
    RAW_TTS = RawTTSHandler()
    RAW_PORT = free_port()
    RAW_THREAD = FakeTTSServer("127.0.0.1", RAW_PORT, RAW_TTS)
    RAW_THREAD.start()
    time.sleep(0.2)
    RAW_SCRATCH = tempfile.mkdtemp(prefix="bwf_speech_raw_")
    RAW_CFG = os.path.join(RAW_SCRATCH, "config.json")
    RAW_DATA = os.path.join(RAW_SCRATCH, "data")
    os.makedirs(RAW_DATA)
    with open(RAW_CFG, "w") as f:
        json.dump({"title": "raw", "port": free_port(), "bind": "127.0.0.1",
                   "lanes": [{"id": "t", "name": "Target lane", "host": "127.0.0.1", "port": FAKE_LANE_PORT,
                              "caps": ["image", "video", "audio"]}],
                   "timing": {"poll_seconds": 0.3},
                   "speech": {"url": "http://127.0.0.1:%d" % RAW_PORT}}, f)
    rproc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO,
                              env=dict(os.environ, GENCENTER_CONFIG=RAW_CFG, GENCENTER_DATA=RAW_DATA),
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    PROCS.append(rproc)
    rurl = "http://127.0.0.1:%d/" % json.load(open(RAW_CFG))["port"]
    ok, _ = wait_true("raw server up", lambda: http_json(rurl + "api/health")[0] == 200, 15)
    code, body, _ = http_json(rurl + "api/speech", data={"lane": "t", "text": "hello"})
    check("non-audio upstream -> 502 'not a sound file'", code == 502 and "sound file" in str(body))

    # --- file:// URL treated as not configured ---
    FILE_SCRATCH = tempfile.mkdtemp(prefix="bwf_speech_file_")
    FILE_CFG = os.path.join(FILE_SCRATCH, "config.json")
    FILE_DATA = os.path.join(FILE_SCRATCH, "data")
    os.makedirs(FILE_DATA)
    with open(FILE_CFG, "w") as f:
        json.dump({"title": "filecfg", "port": free_port(), "bind": "127.0.0.1",
                   "lanes": [{"id": "t", "name": "Target lane", "host": "127.0.0.1", "port": FAKE_LANE_PORT,
                              "caps": ["image", "video", "audio"]}],
                   "timing": {"poll_seconds": 0.3},
                   "speech": {"url": "file:///etc/passwd"}}, f)
    fproc = subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO,
                              env=dict(os.environ, GENCENTER_CONFIG=FILE_CFG, GENCENTER_DATA=FILE_DATA),
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    PROCS.append(fproc)
    furl = "http://127.0.0.1:%d/" % json.load(open(FILE_CFG))["port"]
    ok, _ = wait_true("file server up", lambda: http_json(furl + "api/health")[0] == 200, 15)
    code, body, _ = http_json(furl + "api/speech", data={"lane": "t", "text": "hello"})
    check("file:// URL -> 404 treated as not configured", code == 404)
    code, body, _ = http_json(furl + "api/speech/status")
    check("file:// URL -> status enabled false", code == 200 and body == {"enabled": False})

    # --- enabled status ---
    code, body, _ = http_json(URL + "api/speech/status")
    check("enabled status reports true", code == 200 and body == {"enabled": True})

    # --- cleanup ---
    for p in PROCS:
        stop(p)
    TTS_THREAD.stop()
    ERR_THREAD.stop()
    RAW_THREAD.stop()

    print()
    print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
    sys.exit(1 if FAILED else 0)
