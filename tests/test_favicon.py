"""Acceptance gate for F4 (small-fixes recipe): GET /favicon.ico must answer
204 No Content (no body, a one-day cache header) instead of the app's plain
404 -- so the browser stops logging a console error on every page load. It
must go through the B1 request guard like every other GET (a bad Host still
gets refused).

RED on the pre-fix tree: /favicon.ico falls through to the generic
{"error": "not found"} 404 branch.

Run: python3 tests/test_favicon.py
"""
import http.client, json, os, socket, subprocess, sys, tempfile, time
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


SCRATCH = tempfile.mkdtemp(prefix="bwf_favicon_")
CFG_DIR = tempfile.mkdtemp(prefix="cfg_", dir=SCRATCH)
DATA_DIR = tempfile.mkdtemp(prefix="data_", dir=SCRATCH)
PORT = free_port()
cfg = {"port": PORT, "bind": "127.0.0.1", "title": "f4 favicon test",
       "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
       "lanes": [{"id": "off", "name": "Offline lane", "host": "127.0.0.1", "port": free_port()}]}
cfg_path = os.path.join(CFG_DIR, "config.json")
json.dump(cfg, open(cfg_path, "w"))
env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=DATA_DIR)
proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")],
                        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

deadline = time.time() + 20
while True:
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=0.3).close()
        break
    except Exception:
        if proc.poll() is not None or time.time() > deadline:
            proc.terminate()
            sys.exit("server did not come up")
        time.sleep(0.1)


def req(host):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    try:
        conn.putrequest("GET", "/favicon.ico", skip_host=True)
        conn.putheader("Host", host)
        conn.endheaders()
        r = conn.getresponse()
        body = r.read()
        return r.status, dict(r.getheaders()), body
    finally:
        conn.close()


try:
    print("GET /favicon.ico with a legitimate Host")
    status, hdrs, body = req("127.0.0.1:%d" % PORT)
    check("status is 204", status == 204, status)
    check("no body", body == b"", body)
    check("has a cache header of about one day", hdrs.get("Cache-Control", "") == "max-age=86400", hdrs.get("Cache-Control"))

    print("GET /favicon.ico with Host evil.example goes through the B1 guard")
    status2, hdrs2, body2 = req("evil.example")
    check("status is 403", status2 == 403, status2)
finally:
    proc.terminate()

print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All F4 favicon checks passed.")
