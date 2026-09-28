"""UX-1 gate: "Help me write this" must not appear in a room whose engines
are all unavailable on every lane. Config has ZERO lanes, so every mode is
unavailable everywhere while still registered (roomModes(room).length > 0) --
the "declared but unreachable" shape the fix targets. SKIPs cleanly without
Playwright/Chromium. Run: python3 tests/test_helper_offer_gating.py
"""
import json, os, socket, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.dont_write_bytecode = True
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r: return json.loads(r.read())
def wait_true(desc, fn, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn(): return True
        except Exception: pass
        time.sleep(0.15)
    check(desc, False, "still false after %.0fs" % timeout); return False
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP: playwright is not installed."); sys.exit(0)
try:
    with sync_playwright() as _pw: _pw.chromium.launch().close()
except Exception as e:
    print("SKIP: chromium is not installed (%s)." % e); sys.exit(0)

class FakeHelper(BaseHTTPRequestHandler):
    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self): self._send({"data": [{"id": "test-model", "meta": {"n_ctx": 8192}}]})
    def do_POST(self): self._send({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    def log_message(self, *a): pass

fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelper)
threading.Thread(target=fake.serve_forever, daemon=True).start()
PROCS = []
SCRATCH = tempfile.mkdtemp(prefix="bwf_helper_gate_")

def start_server():
    port = free_port()
    dead = free_port()   # a port nothing listens on -- this lane never comes up
    cfg = {"title": "helper offer gating test", "port": port, "bind": "127.0.0.1",
           "lanes": [{"id": "t", "name": "Dead lane", "host": "127.0.0.1", "port": dead,
                      "caps": ["image", "video", "audio"], "models": {}}],
           "helper": {"url": "http://127.0.0.1:%d/v1" % fake.server_address[1], "model": "test-model"},
           "timing": {"poll_seconds": 0.5, "job_poll_seconds": 1.0}}
    cfg_path = os.path.join(SCRATCH, "config.json")
    json.dump(cfg, open(cfg_path, "w"))
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=os.path.join(SCRATCH, "data"))
    logf = open(os.path.join(SCRATCH, "server.log"), "w")
    PROCS.append(subprocess.Popen([sys.executable, os.path.join(REPO, "server.py")], cwd=REPO, env=env,
                                  stdout=logf, stderr=subprocess.STDOUT))
    url = "http://127.0.0.1:%d/" % port
    if not wait_true("server is up", lambda: http_json(url + "api/health").get("ok"), 30):
        raise SystemExit("server did not come up")
    return url

try:
    url = start_server()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url + "#room=picture", wait_until="networkidle", timeout=30000)
        page.wait_for_function("() => document.querySelector('#guideName') && "
                               "document.querySelector('#guideName').textContent === 'The Picture Guide'", timeout=15000)
        page.wait_for_timeout(1000)
        check("picture room: Help me write this is NOT offered with nothing reachable",
              not page.is_visible("#helperWriteBtn"), page.is_visible("#helperWriteBtn"))
        check("picture room: the plain note explains why",
              page.is_visible("#roomPlainNote") and page.inner_text("#roomPlainNote").strip() != "",
              page.inner_text("#roomPlainNote") if page.is_visible("#roomPlainNote") else "(hidden)")
        page.close(); browser.close()
finally:
    for p in PROCS:
        if p.poll() is None:
            p.terminate()
            try: p.wait(timeout=5)
            except Exception: p.kill()

print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)) if FAILED else "OK: all checks passed")
sys.exit(1 if FAILED else 0)
