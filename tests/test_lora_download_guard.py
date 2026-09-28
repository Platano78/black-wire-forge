"""LORA-1 Build C, against a REAL server subprocess (not a monkeypatched
module): POST /api/lora/download is behind the same B1 request guard as
every other POST -- a cross-site Origin/Sec-Fetch-Site is refused before the
route's own logic ever runs, no live network involved (the lane below has no
"downloads" configured, so even a same-origin call never reaches HF).

Run: python3 tests/test_lora_download_guard.py
"""
import http.client, json, os, socket, subprocess, sys, tempfile, time
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond: FAILED.append(name)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


SCRATCH = tempfile.mkdtemp(prefix="bwf_lora_guard_")
PORT = free_port()
cfg = {"port": PORT, "bind": "127.0.0.1", "title": "lora guard test",
       "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
       "lanes": [{"id": "off", "name": "Off lane", "host": "127.0.0.1", "port": free_port()}]}
cfg_path = os.path.join(SCRATCH, "config.json")
json.dump(cfg, open(cfg_path, "w"))
env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=os.path.join(SCRATCH, "data"))
proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], cwd=ROOT, env=env,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.time() + 20
while True:
    try:
        socket.create_connection(("127.0.0.1", PORT), timeout=0.3).close()
        break
    except Exception:
        if proc.poll() is not None or time.time() > deadline:
            sys.exit("server did not come up")
        time.sleep(0.1)


def post(path, body, origin=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    data = json.dumps(body).encode()
    headers = {"Host": "127.0.0.1:%d" % PORT, "Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    conn.request("POST", path, data, headers)
    r = conn.getresponse()
    out = json.loads(r.read().decode())
    conn.close()
    return r.status, out


try:
    print("same-origin POST reaches the route's own logic (lane has no \"downloads\": refused there, not by the guard)")
    status, body = post("/api/lora/download", {"lane": "off", "repo": "a/b", "file": "x.safetensors"})
    # Security review Finding 5: this is also 403 now (the spec's own status code for "downloads
    # off"), same numeric code as the cross-site guard refusal below -- the message text is what
    # actually distinguishes "the route's own refusal" from "the guard refused before routing".
    check("403, the route's own \"downloads are off\" refusal (not the guard's)", status == 403
          and "downloads" in body.get("error", "").lower(), (status, body))

    print()
    print("cross-site POST (different Origin) is refused by the B1 guard BEFORE routing -- 403, generic sentence")
    status2, body2 = post("/api/lora/download", {"lane": "off", "repo": "a/b", "file": "x.safetensors"},
                          origin="https://evil.example")
    check("403", status2 == 403, (status2, body2))
    check("the guard's own generic sentence, not the route's", "another site" in body2.get("error", "").lower(),
          body2)
finally:
    proc.terminate()
    print()
    print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
    sys.exit(1 if FAILED else 0)
