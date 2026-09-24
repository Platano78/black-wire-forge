"""Gate for slice L5 -- the prompt helper (internal research notes
row L5).

Everything runs against:
  - a fake OpenAI-compatible endpoint (stdlib http.server) standing in for
    the configured "helper", recording every request it receives;
  - a fake ComfyUI lane's /view?type=input, for the describe-from-a-comfy-
    upload path;
  - a real process lane's own UPLOADS_DIR, for the describe-from-a-process-
    upload path and the "unknown name / .." containment check;
  - a scratch GENCENTER_CONFIG / GENCENTER_DATA (mkdtemp), set BEFORE
    server.py is imported, the same pattern as test_process_lane.py;
  - the real HTTP API, served in-process on a free 127.0.0.1 port.

A second server, imported from a SEPARATE scratch config with no "helper"
key at all, covers the "helper absent" half of the contract.

Run: python3 tests/test_helper.py
"""
import base64
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        print("        -> %s" % (detail,))
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible helper endpoint
# ---------------------------------------------------------------------------
HELPER_STATE = {"mode": "normal", "reply": "a red kite over the sea", "requests": []}


class FakeHelperHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        HELPER_STATE["requests"].append(body)
        if HELPER_STATE["mode"] == "slow":
            time.sleep(HELPER_STATE.get("slow_seconds", 3.0))
        resp = json.dumps({"choices": [{"message": {"content": HELPER_STATE["reply"]}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


fake_helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelperHandler)
HELPER_PORT = fake_helper.server_address[1]
threading.Thread(target=fake_helper.serve_forever, daemon=True).start()

# ---------------------------------------------------------------------------
# Fake ComfyUI lane: only GET /view (any ?type=), byte dict keyed by filename
# ---------------------------------------------------------------------------
VIEW_BYTES = {}


class FakeComfyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/view"):
            self.send_response(404); self.end_headers(); return
        q = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p)
        import urllib.parse as up
        filename = up.unquote(q.get("filename", ""))
        data = VIEW_BYTES.get(filename)
        if data is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


fake_comfy = ThreadingHTTPServer(("127.0.0.1", 0), FakeComfyHandler)
COMFY_PORT = fake_comfy.server_address[1]
threading.Thread(target=fake_comfy.serve_forever, daemon=True).start()

# A tiny valid PNG (1x1), so mime-sniff-by-extension has something real behind it.
PNG_1PX = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")

# ---------------------------------------------------------------------------
# Scratch env BEFORE importing server.py (test_process_lane.py's pattern)
# ---------------------------------------------------------------------------
SCRATCH = tempfile.mkdtemp(prefix="bwf_l5_")
DATA = os.path.join(SCRATCH, "data")
os.makedirs(DATA)
CONFIG = os.path.join(SCRATCH, "config.json")

API_PORT = free_port()   # bound below: the B1 guard checks Host against the
                         # config port, so the server must listen on it
with open(CONFIG, "w") as f:
    json.dump({
        "port": API_PORT, "bind": "127.0.0.1", "title": "l5",
        "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
        "lanes": [
            {"id": "c1", "name": "Comfy lane", "host": "127.0.0.1", "port": COMFY_PORT, "caps": ["image"]},
            {"id": "proc", "name": "Proc box", "kind": "process", "caps": ["3d"], "box": "here"},
        ],
        "helper": {"url": "http://127.0.0.1:%d/v1" % HELPER_PORT, "model": "test-model", "timeout_s": 2},
    }, f)
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = DATA

_spec = importlib.util.spec_from_file_location("srv_l5", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)

HTTPD = ThreadingHTTPServer(("127.0.0.1", API_PORT), srv.Handler)
PORT = HTTPD.server_address[1]
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()


def http(path, body=None, ctype="application/json"):
    """(status, parsed-json) for a request to the in-process API."""
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data,
                               method="POST" if data is not None else "GET",
                               headers={"Content-Type": ctype} if data else {})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


# ---------------------------------------------------------------------------
# 1. write: the request carries the mode's guide as system text and the
#    user's idea; a <think> block and surrounding quotes are stripped.
# ---------------------------------------------------------------------------
print("write: mode guide as system, idea as user, <think> stripped")
HELPER_STATE["reply"] = "<think>reasoning about kites</think>\"a red kite over the sea\""
HELPER_STATE["requests"] = []
code, body = http("/api/helper", {"action": "write", "cap": "image", "mode": "t2i", "text": "a kite"})
check("write: 200 ok", code == 200 and body.get("ok") is True, repr((code, body)))
check("write: <think> and quotes stripped", body.get("text") == "a red kite over the sea", repr(body))
req = HELPER_STATE["requests"][-1] if HELPER_STATE["requests"] else {}
sysmsg = next((m["content"] for m in req.get("messages", []) if m["role"] == "system"), "")
usermsg = next((m["content"] for m in req.get("messages", []) if m["role"] == "user"), "")
guide = srv.engines.prompt_guide("image", "t2i")
check("write: system carries the mode's own guide", bool(guide) and guide in sysmsg, repr(sysmsg))
check("write: system asks for prompt text only", "Return only the prompt text." in sysmsg, repr(sysmsg))
check("write: user carries the idea verbatim", usermsg == "a kite", repr(usermsg))

print("write: no cap/mode falls back to the generic guide")
code, body = http("/api/helper", {"action": "write", "text": ""})
req = HELPER_STATE["requests"][-1]
sysmsg = next((m["content"] for m in req.get("messages", []) if m["role"] == "system"), "")
check("write: generic guide used when no mode is named",
      srv.GENERIC_PROMPT_GUIDE in sysmsg, repr(sysmsg))
check("write: an empty idea is still accepted (the helper proposes one)", code == 200, repr((code, body)))

# ---------------------------------------------------------------------------
# 2. describe (comfy upload): the request carries an image_url data URL
#    with the exact bytes.
# ---------------------------------------------------------------------------
print("describe (uploaded input on a comfy lane): image_url data URL, exact bytes")
VIEW_BYTES["kite.png"] = PNG_1PX
HELPER_STATE["reply"] = "a kite"
HELPER_STATE["requests"] = []
code, body = http("/api/helper", {"action": "describe", "cap": "image", "mode": "edit",
                                   "image": {"upload": "kite.png", "lane": "c1"}})
check("describe (comfy upload): 200 ok", code == 200 and body.get("ok") is True, repr((code, body)))
req = HELPER_STATE["requests"][-1]
usermsg = next((m["content"] for m in req.get("messages", []) if m["role"] == "user"), None)
img_url = (usermsg[0]["image_url"]["url"] if isinstance(usermsg, list) else "")
check("describe (comfy upload): user message is an image_url data URL",
      img_url.startswith("data:image/"), repr(usermsg)[:200])
sent_b64 = img_url.split(",", 1)[1] if "," in img_url else ""
check("describe (comfy upload): exact bytes reach the helper",
      base64.b64decode(sent_b64) == PNG_1PX, "byte mismatch")
sysmsg = next((m["content"] for m in req.get("messages", []) if m["role"] == "system"), "")
check("describe: system asks it to describe as a prompt for this mode",
      "Describe this picture as a prompt for this mode." in sysmsg, repr(sysmsg))

print("describe (uploaded input on a process lane): the process lane's own UPLOADS_DIR")
os.makedirs(os.path.join(srv.UPLOADS_DIR, "proc"), exist_ok=True)
with open(os.path.join(srv.UPLOADS_DIR, "proc", "abcd1234_shape.png"), "wb") as f:
    f.write(PNG_1PX)
code, body = http("/api/helper", {"action": "describe",
                                   "image": {"upload": "abcd1234_shape.png", "lane": "proc"}})
check("describe (process upload): 200 ok", code == 200 and body.get("ok") is True, repr((code, body)))

print("describe (finished job output): the existing source-bytes path")
with srv.JOBS_LOCK:
    srv.JOBS["fx-job"] = {"id": "fx-job", "lane": "c1",
                           "outputs": [{"filename": "kite.png", "subfolder": "", "type": "output"}]}
code, body = http("/api/helper", {"action": "describe", "image": {"job_id": "fx-job", "output": 0}})
check("describe (job output): 200 ok", code == 200 and body.get("ok") is True, repr((code, body)))

# ---------------------------------------------------------------------------
# 3. failure modes
# ---------------------------------------------------------------------------
print("a slow endpoint -> 503 with the sentence, within timeout+1s")
HELPER_STATE["mode"] = "slow"
HELPER_STATE["slow_seconds"] = 5.0
t0 = time.time()
code, body = http("/api/helper", {"action": "write", "text": "x"})
dt = time.time() - t0
check("slow helper: 503", code == 503, repr((code, body)))
check("slow helper: the plain sentence", body.get("error") == srv.HELPER_BUSY_SENTENCE, repr(body))
check("slow helper: answered within timeout+1s (%.2fs, timeout 2s)" % dt, dt <= 3.0, "%.2fs" % dt)
HELPER_STATE["mode"] = "normal"

# ---------------------------------------------------------------------------
# P3 (portability fix): a helper pointed at a host that is not answering AT
# ALL (not merely slow) must fail fast, via a short TCP connect probe, with
# a sentence naming the host and config.json -- not wait out the full read
# timeout_s.
# ---------------------------------------------------------------------------
print("an unreachable helper host -> fast 503 with the connect-probe sentence")
closed_port = free_port()  # bound then released: nothing listens, so a
                            # connect is refused immediately -- the fast
                            # case the pre-fix code already handled by luck;
                            # the real regression this guards is D-3's
                            # unreachable-HOST hang (see test_portability.py).
SCRATCH3 = tempfile.mkdtemp(prefix="bwf_l5_deadhelper_")
DATA3 = os.path.join(SCRATCH3, "data")
os.makedirs(DATA3)
CONFIG3 = os.path.join(SCRATCH3, "config.json")
API_PORT3 = free_port()
with open(CONFIG3, "w") as f:
    json.dump({"port": API_PORT3, "bind": "127.0.0.1", "title": "l5-deadhelper",
               "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
               "lanes": [{"id": "c1", "name": "Comfy lane", "host": "127.0.0.1",
                          "port": COMFY_PORT, "caps": ["image"]}],
               "helper": {"url": "http://127.0.0.1:%d/v1" % closed_port,
                          "model": "test-model", "timeout_s": 60}}, f)
os.environ["GENCENTER_CONFIG"] = CONFIG3
os.environ["GENCENTER_DATA"] = DATA3
_spec3 = importlib.util.spec_from_file_location("srv_l5_deadhelper", os.path.join(ROOT, "server.py"))
srv3 = importlib.util.module_from_spec(_spec3)
_spec3.loader.exec_module(srv3)
HTTPD3 = ThreadingHTTPServer(("127.0.0.1", API_PORT3), srv3.Handler)
PORT3 = HTTPD3.server_address[1]
threading.Thread(target=HTTPD3.serve_forever, daemon=True).start()

t0 = time.time()
r3 = urllib.request.Request("http://127.0.0.1:%d/api/helper" % PORT3,
                            data=b'{"action": "write", "text": "x"}', method="POST",
                            headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(r3, timeout=10) as resp:
        code3, body3 = resp.status, json.loads(resp.read())
except urllib.error.HTTPError as e:
    code3, body3 = e.code, json.loads(e.read())
dt3 = time.time() - t0
check("unreachable helper: 503", code3 == 503, repr((code3, body3)))
check("unreachable helper: answers well under the 60s read timeout (%.2fs)" % dt3, dt3 < 5.0, "%.2fs" % dt3)
check("unreachable helper: sentence names the host:port",
      "127.0.0.1:%d" % closed_port in (body3.get("error") or ""), repr(body3))
check("unreachable helper: sentence points at config.json's \"helper\" key",
      "helper" in (body3.get("error") or "") and "config.json" in (body3.get("error") or ""), repr(body3))
HTTPD3.shutdown()
shutil.rmtree(SCRATCH3, ignore_errors=True)

print("oversize text -> 400")
code, body = http("/api/helper", {"action": "write", "text": "x" * (srv.HELPER_TEXT_LIMIT + 1)})
check("oversize text: 400", code == 400, repr((code, body)))

print("oversize image -> 400")
with open(os.path.join(srv.UPLOADS_DIR, "proc", "big.png"), "wb") as f:
    f.write(b"\0" * (srv.HELPER_IMAGE_LIMIT + 1))
code, body = http("/api/helper", {"action": "describe", "image": {"upload": "big.png", "lane": "proc"}})
check("oversize image: 400", code == 400, repr((code, body)))

print("an unknown upload name / ../ -> 400")
code, body = http("/api/helper", {"action": "describe",
                                   "image": {"upload": "../../etc/passwd", "lane": "proc"}})
check("path traversal upload name: 400", code == 400, repr((code, body)))
code, body = http("/api/helper", {"action": "describe",
                                   "image": {"upload": "does-not-exist.png", "lane": "proc"}})
check("unknown upload name: 400", code == 400, repr((code, body)))

# ---------------------------------------------------------------------------
# 4. /api/engines: helper: true at top level, with a helper configured
# ---------------------------------------------------------------------------
print("/api/engines: helper: true, and t2i carries prompt_guide: true")
code, engines_body = http("/api/engines?lane=c1")
check("engines: helper true", engines_body.get("helper") is True, repr(engines_body.get("helper")))
t2i = next((m for m in engines_body.get("image", {}).get("modes", []) if m["id"] == "t2i"), {})
check("engines: t2i carries prompt_guide true", t2i.get("prompt_guide") is True, repr(t2i))

# ---------------------------------------------------------------------------
# 5. no helper in config: /api/helper 404s, helper: false
# ---------------------------------------------------------------------------
print("no helper in config: /api/helper 404, /api/engines helper: false")
SCRATCH2 = tempfile.mkdtemp(prefix="bwf_l5_nohelper_")
DATA2 = os.path.join(SCRATCH2, "data")
os.makedirs(DATA2)
CONFIG2 = os.path.join(SCRATCH2, "config.json")
API_PORT2 = free_port()
with open(CONFIG2, "w") as f:
    json.dump({"port": API_PORT2, "bind": "127.0.0.1", "title": "l5-nohelper",
               "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
               "lanes": [{"id": "c1", "name": "Comfy lane", "host": "127.0.0.1",
                          "port": COMFY_PORT, "caps": ["image"]}]}, f)
os.environ["GENCENTER_CONFIG"] = CONFIG2
os.environ["GENCENTER_DATA"] = DATA2
_spec2 = importlib.util.spec_from_file_location("srv_l5_nohelper", os.path.join(ROOT, "server.py"))
srv2 = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(srv2)
HTTPD2 = ThreadingHTTPServer(("127.0.0.1", API_PORT2), srv2.Handler)
PORT2 = HTTPD2.server_address[1]
threading.Thread(target=HTTPD2.serve_forever, daemon=True).start()

r = urllib.request.Request("http://127.0.0.1:%d/api/helper" % PORT2,
                           data=b'{"action": "write", "text": "x"}', method="POST",
                           headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(r) as resp:
        code2 = resp.status
except urllib.error.HTTPError as e:
    code2 = e.code
check("no helper configured: POST /api/helper 404s", code2 == 404, repr(code2))
with urllib.request.urlopen("http://127.0.0.1:%d/api/engines" % PORT2) as resp:
    engines2 = json.loads(resp.read())
check("no helper configured: /api/engines helper: false", engines2.get("helper") is False, repr(engines2.get("helper")))
HTTPD2.shutdown()
shutil.rmtree(SCRATCH2, ignore_errors=True)

# ---------------------------------------------------------------------------
# 6. Every mode with a text prompt has a guide with a `# source:` comment.
# ---------------------------------------------------------------------------
print("every mode with a text prompt has a prompt_guide, cited with # source:")
for pack in srv.engines.packs():
    cap = pack["cap"]
    for mode, fields in (pack.get("fields") or {}).items():
        prompt_fields = [f for f in fields if f.get("type") in ("text", "textarea")]
        if not prompt_fields:
            continue
        guide = srv.engines.prompt_guide(cap, mode)
        check("%s/%s (has a text prompt field): has a prompt_guide" % (cap, mode), bool(guide))
        path = srv.engines._PACK_FILES.get(pack["id"])
        if not (guide and path):
            continue
        src = open(path).read()
        i = src.find('"prompt_guides"')
        block = src[i:i + 4000] if i >= 0 else ""
        check("%s/%s: pack file cites # source: near its guides" % (cap, mode),
              "# source:" in block, "no prompt_guides block or no # source: found")

# ---------------------------------------------------------------------------
# 7. E1 (post-L5 owner observation): a mode whose prompt field is NOT where
#    other text (lyrics / what-to-avoid) belongs must tell the helper so
#    explicitly, or "Use this" mixes that other text into the wrong field.
#    This is the fixed instance list E1 tightened -- audio song/yue2/cover
#    (lyrics have their own field) and qwen-image t2i (negative/"avoid" has
#    its own field); every one of these guides must end with an explicit
#    "Return only" instruction.
# ---------------------------------------------------------------------------
print("every guide whose mode's prompt field is not where other text belongs says 'Return only'")
NOT_WHERE_OTHER_TEXT_BELONGS = [("audio", "song"), ("audio", "yue2"), ("audio", "cover"),
                                 ("image", "t2i")]
for cap, mode in NOT_WHERE_OTHER_TEXT_BELONGS:
    guide = srv.engines.prompt_guide(cap, mode)
    check("%s/%s: guide has an explicit 'Return only' instruction" % (cap, mode),
          bool(guide) and "Return only" in guide, repr(guide))

# ---------------------------------------------------------------------------
# 8. F1 (small-fixes recipe): H3's "continue" mode prompt guide must say the
#    prompt describes the SAME shot carrying on -- same subject, camera move
#    and framing -- and cite the l2-continuity note the ruling came from.
# ---------------------------------------------------------------------------
print("H3 continue mode's prompt guide says 'same' shot/framing and cites the l2-continuity note")
continue_guide = srv.engines.prompt_guide("video", "continue")
check("video/continue: guide mentions 'same'", bool(continue_guide) and "same" in continue_guide, repr(continue_guide))
check("video/continue: guide mentions 'framing'", bool(continue_guide) and "framing" in continue_guide, repr(continue_guide))
check("video/continue: guide names the l2-continuity note",
      bool(continue_guide) and "l2-continuity-2026-09-23" in continue_guide, repr(continue_guide))

HTTPD.shutdown()
shutil.rmtree(SCRATCH, ignore_errors=True)

print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All L5 prompt-helper checks passed.")
