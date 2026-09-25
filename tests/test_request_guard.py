"""Acceptance gate for B1 -- request guard (DNS-rebinding + cross-site POST).

Written AGAINST the frozen rulings in the commission brief before the guard
exists: server.py has no Host/Origin/Content-Type check anywhere, so every
refusal below is expected to FAIL on the current tree; this file records that
as RED. The two "200" checks (plain Host, allowed_hosts entry) must pass both
before and after -- nothing the owner does today may break.

ISOLATION: every byte goes under a fresh tempfile.mkdtemp(); two server.py
subprocesses run on free ports with GENCENTER_CONFIG/GENCENTER_DATA pointed at
scratch dirs (the same move tests/test_cables.py makes in-process). Requests
go through http.client with putrequest/putheader so each Host header is
exactly what the check says it is -- urllib would overwrite it.

Every check() call is independent -- one failure never aborts the rest.

Run: python3 tests/test_request_guard.py
"""
import http.client, json, os, shutil, socket, subprocess, sys
import tempfile, time, urllib.request
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


# ---------------------------------------------------------------------------
# Two server.py subprocesses: the plain default, and one with an
# allowed_hosts entry. No lanes -- the guard must not need any.
# ---------------------------------------------------------------------------

SCRATCH = tempfile.mkdtemp(prefix="bwf_guard_")
print("scratch dir: %s (real data/ is never written)" % SCRATCH)
SERVERS = []


def start_server(port, config_extra=None):
    cfg_dir = tempfile.mkdtemp(prefix="cfg_", dir=SCRATCH)
    data_dir = tempfile.mkdtemp(prefix="data_", dir=SCRATCH)
    cfg = {"port": port, "bind": "127.0.0.1", "title": "b1 guard test",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30, "http_timeout": 2.0},
           "lanes": [{"id": "off", "name": "Offline lane",
                      "host": "127.0.0.1", "port": free_port()}]}
    cfg.update(config_extra or {})
    cfg_path = os.path.join(cfg_dir, "config.json")
    json.dump(cfg, open(cfg_path, "w"))
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=data_dir)
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")],
                         cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            break
        except Exception:
            if p.poll() is not None or time.time() > deadline:
                sys.exit("server on port %d did not come up" % port)
            time.sleep(0.1)
    SERVERS.append(p)
    return p


PORT_A = free_port()
PORT_B = free_port()
PORT_OTHER = free_port()
start_server(PORT_A)
start_server(PORT_B, {"allowed_hosts": ["forge.lan"]})


def stop_servers():
    for p in SERVERS:
        try:
            p.terminate()
        except Exception:
            pass


def req(port, method, path, host="auto", headers=None, body=None):
    """One request with the Host header exactly as given (None = send none)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.putrequest(method, path, skip_host=True)
        if host == "auto":
            conn.putheader("Host", "127.0.0.1:%d" % port)
        elif host is not None:
            conn.putheader("Host", host)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        if body is not None:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        if body:
            conn.send(body)
        r = conn.getresponse()
        data = r.read()
        return r.status, data
    finally:
        conn.close()


def raw_connect(port, timeout=3.0):
    return socket.create_connection(("127.0.0.1", port), timeout=timeout)


def raw_send(s, lines, body=b"", timeout=3.0):
    """One request written straight to a socket -- for shapes http.client
    cannot express (duplicate headers). `lines` are the request/header lines,
    without the trailing CRLFs. Reads the full response (headers + body, by
    Content-Length) so the socket is left clean for a second request on it.
    Returns (status, headers_dict, body_bytes) -- status is None on timeout."""
    s.settimeout(timeout)
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    except socket.timeout:
        return None, {}, b""
    head, _, rest = buf.partition(b"\r\n\r\n")
    if not head.startswith(b"HTTP"):
        return None, {}, rest
    status = int(head.split(b"\r\n", 1)[0].split(b" ", 2)[1])
    hdrs = {}
    for hl in head.split(b"\r\n")[1:]:
        if b":" in hl:
            k, v = hl.split(b":", 1)
            hdrs[k.strip().lower().decode()] = v.strip().decode()
    clen = int(hdrs.get("content-length", "0"))
    try:
        while len(rest) < clen:
            chunk = s.recv(4096)
            if not chunk:
                break
            rest += chunk
    except socket.timeout:
        pass
    return status, hdrs, rest


def req_json(port, method, path, **kw):
    status, data = req(port, method, path, **kw)
    try:
        return status, json.loads(data.decode("utf-8") or "{}")
    except Exception:
        return status, {}


def seq_count(port):
    status, obj = req_json(port, "GET", "/api/sequences")
    assert status == 200 and isinstance(obj, list), (status, obj)
    return len(obj)


SEQ_BODY = json.dumps({"title": "guard probe", "mode": "sequence"}).encode("utf-8")

# --- 1. plain GETs the owner makes every day must keep working -------------
print("\nHost check -- what must keep working")
status, _ = req(PORT_A, "GET", "/", host="127.0.0.1:%d" % PORT_A)
check("GET / with Host 127.0.0.1:<port> -> 200", status, 200)
status, _ = req(PORT_A, "GET", "/", host="localhost:%d" % PORT_A)
check("GET / with Host localhost:<port> -> 200", status, 200)
status, _ = req(PORT_A, "GET", "/", host="127.0.0.1")   # no port given is fine
check("GET / with Host 127.0.0.1 (no port) -> 200", status, 200)

# --- 2. Host check -- DNS rebinding protection -----------------------------
print("\nHost check -- refusals")
status, obj = req_json(PORT_A, "GET", "/", host="evil.example")
check("GET / with Host evil.example -> 403", status == 403, status)
check("the 403 body's error names allowed_hosts",
      status == 403 and "allowed_hosts" in (obj.get("error") or ""), obj)
status, _ = req_json(PORT_A, "GET", "/", host="evil.example:%d" % PORT_A)
check("GET / with Host evil.example:<port> -> 403", status == 403, status)
status, obj = req_json(PORT_A, "GET", "/", host="127.0.0.1:%d" % PORT_OTHER)
check("GET / with Host 127.0.0.1:<other port> -> 403", status == 403, status)
err = obj.get("error") or "" if isinstance(obj, dict) else ""
check("the port refusal names the app's port and the proxy fix, not allowed_hosts "
      "(which cannot help: the port is checked first)",
      str(PORT_A) in err and "proxy" in err and "allowed_hosts" not in err, err)
status, obj = req_json(PORT_B, "GET", "/", host="forge.lan:%d" % PORT_OTHER)
err = obj.get("error") or "" if isinstance(obj, dict) else ""
check("an allowed_hosts name on the wrong port is refused the same way",
      status == 403 and str(PORT_B) in err and "allowed_hosts" not in err, (status, err))
status, _ = req(PORT_A, "GET", "/", host=None)
check("GET / with no Host header -> 400", status == 400, status)
status, _ = req(PORT_B, "GET", "/", host="forge.lan:%d" % PORT_B)
check("server with allowed_hosts [forge.lan]: Host forge.lan:<port> -> 200",
      status, 200)

# --- 3. POST content type -- simple cross-site POST protection -------------
print("\nPOST content type")
before = seq_count(PORT_A)
status, _ = req_json(PORT_A, "POST", "/api/sequence",
                     headers={"Content-Type": "text/plain"}, body=SEQ_BODY)
check("POST /api/sequence with Content-Type text/plain -> 415", status == 415, status)
check("...and no sequence was created", seq_count(PORT_A) == before,
      "%d -> %d" % (before, seq_count(PORT_A)))

# --- 4. POST origin ---------------------------------------------------------
print("\nPOST origin")
before = seq_count(PORT_A)
status, _ = req_json(PORT_A, "POST", "/api/sequence",
                     headers={"Content-Type": "application/json",
                              "Origin": "http://evil.example"}, body=SEQ_BODY)
check("POST /api/sequence with Origin http://evil.example -> 403", status == 403, status)
check("...and nothing was created", seq_count(PORT_A) == before,
      "%d -> %d" % (before, seq_count(PORT_A)))
before = seq_count(PORT_A)
status, _ = req_json(PORT_A, "POST", "/api/sequence",
                     headers={"Content-Type": "application/json",
                              "Sec-Fetch-Site": "cross-site"}, body=SEQ_BODY)
check("POST /api/sequence with Sec-Fetch-Site cross-site -> 403", status == 403, status)
check("...and nothing was created", seq_count(PORT_A) == before,
      "%d -> %d" % (before, seq_count(PORT_A)))
before = seq_count(PORT_A)
status, obj = req_json(PORT_A, "POST", "/api/sequence",
                       headers={"Content-Type": "application/json",
                                "Origin": "http://127.0.0.1:%d" % PORT_A},
                       body=SEQ_BODY)
check("POST /api/sequence with same-origin Origin -> 200", status == 200, status)
check("...and the sequence was created", seq_count(PORT_A) == before + 1,
      "%d -> %d" % (before, seq_count(PORT_A)))

# --- 5. the multipart upload lane stays open --------------------------------
print("\nPOST /api/upload multipart")
BOUND = "bwfguardboundary"
mbody = ("--%s\r\nContent-Disposition: form-data; name=\"lane\"\r\n\r\nnope\r\n"
         "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.png\"\r\n"
         "Content-Type: image/png\r\n\r\npngbytes\r\n--%s--\r\n" % (BOUND, BOUND, BOUND)).encode()
status, obj = req_json(PORT_A, "POST", "/api/upload",
                       headers={"Content-Type": "multipart/form-data; boundary=%s" % BOUND},
                       body=mbody)
# The lane id is deliberately unknown: a 400 "unknown lane" proves the guard
# let the multipart request through and api_upload() parsed it.
check("multipart POST /api/upload is not refused by the guard (not 403/415)",
      status not in (403, 415), (status, obj))
check("...and reached api_upload (400 unknown lane)", status == 400, (status, obj))

# --- 6. duplicate Host headers -- RFC 7230 5.4: more than one is malformed --
print("\nDuplicate Host headers")
s = raw_connect(PORT_A)
status, _, body = raw_send(s, [
    "GET / HTTP/1.1",
    "Host: 127.0.0.1:%d" % PORT_A,
    "Host: evil.example",
    "Connection: close",
])
try:
    obj = json.loads(body.decode("utf-8") or "{}")
except Exception:
    obj = {}
check("GET / with two Host headers (first legitimate) -> 400", status == 400, status)
check("...and the 400 says to send exactly one",
      "exactly one Host header" in (obj.get("error") or ""), obj)
s.close()

# --- 7. a refused POST must not drain an unbounded body ---------------------
print("\nrefused POST body cap")
s = raw_connect(PORT_A)
t0 = time.time()
status, _, _ = raw_send(s, [
    "POST /api/sequence HTTP/1.1",
    "Host: evil.example",
    "Content-Type: application/json",
    "Content-Length: 1000000000",
], body=b"")   # no body ever sent -- a naive drain blocks on this
elapsed = time.time() - t0
check("refused POST w/ Content-Length 1e9 and no body answers within 2s",
      status is not None and elapsed < 2.0, (status, elapsed))
check("...and it is refused (403, evil.example Host)", status == 403, status)
s.close()
status, _ = req(PORT_A, "GET", "/", host="127.0.0.1:%d" % PORT_A)
check("...and a fresh connection right after is served normally (200)",
      status == 200, status)

# --- 8. a refused POST with a small body still drains on keep-alive --------
print("\nrefused POST small-body drain on keep-alive")
s = raw_connect(PORT_A)
small_body = b"{}"
status, _, _ = raw_send(s, [
    "POST /api/sequence HTTP/1.1",
    "Host: evil.example",
    "Content-Type: application/json",
    "Content-Length: %d" % len(small_body),
], body=small_body)
check("refused POST w/ small body on keep-alive -> 403", status == 403, status)
status2, _, _ = raw_send(s, [
    "GET / HTTP/1.1",
    "Host: 127.0.0.1:%d" % PORT_A,
])
check("...and the next GET on the SAME connection -> 200", status2 == 200, status2)
s.close()

stop_servers()
shutil.rmtree(SCRATCH, ignore_errors=True)
print("\n%s" % ("FAILED: %d -- %s" % (len(FAILED), FAILED) if FAILED else "ALL PASS"))
sys.exit(1 if FAILED else 0)
