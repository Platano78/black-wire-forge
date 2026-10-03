"""LORA-1 security review fixes (security-lora.md, orchestrator round 2).
Findings 1/3/4/5: RED reproduces the exact PRE-FIX behaviour inline (same
discipline as tests/test_generic_dispatch.py's "reproduced inline, not
re-imported" RED proof) against a second exec of server.py, monkeypatched
back to the vulnerable code -- never a copy of the file on disk. GREEN runs
the identical check against the real, fixed module. Finding 2 is checked at
the unit level (the exact allow/refuse decision) plus one real local-server
redirect (to a disallowed host).

Run: python3 tests/test_security_lora.py
"""
import importlib.util, os, sys, tempfile, threading, time, urllib.request
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)


def _raises(fn, exc=ValueError):
    try:
        fn()
        return False
    except exc:
        return True

import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below


def load_server(tag):
    spec = importlib.util.spec_from_file_location("srv_sec_%s" % tag, os.path.join(ROOT, "server.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


srv = load_server("fixed")       # the real, fixed module -- GREEN
red = load_server("red")         # same source; monkeypatched back to the pre-fix code below -- RED


def make_lane(srv_mod, lane_id, loras_dir):
    lane = {"id": lane_id, "name": "Lane " + lane_id, "caps": ["image"],
            "downloads": {"loras_dir": loras_dir}}
    srv_mod.LANE_BY_ID[lane_id] = lane
    with srv_mod.DISCOVERY_LOCK:
        srv_mod.DISCOVERY[lane_id] = {"models": {"qwen_unet": "qwen_image_2.1_Q6_K.gguf"},
                                      "pools": {}, "checked": 1.0, "err": ""}
    entry = {"id": "owner/pack", "downloads": 10, "likes": 1, "licence": "mit", "nsfw": False,
             "files": [{"filename": "style.safetensors", "size": 12345}]}
    srv_mod.CATALOG_CACHE["Qwen/Qwen-Image-2.1"] = (time.time(), [entry])
    return lane


# ── Finding 1: race in api_lora_download_start ──────────────────────────────
print("Finding 1: 'one download at a time per lane' under two concurrent starts")


def _old_api_lora_download_start(self, p):
    """Verbatim pre-fix logic: check-then-register in TWO critical sections,
    with unlocked work (here: a sleep standing in for a cold-cache HF call,
    same as the security report's own exploit) in between."""
    lane = red.LANE_BY_ID.get(p.get("lane")) if isinstance(p.get("lane"), str) else None
    if not lane:
        return {"ok": False, "error": "Pick a lane first."}, 400
    with red.DOWNLOAD_LOCK:
        running = red.DOWNLOADS.get(lane["id"])
        if running and not running.get("done"):
            return {"ok": False, "error": "A download is already running for %s." % lane["name"]}, 409
    time.sleep(0.3)   # <-- the window: unlocked, standing in for a cold catalog_for() HF call
    red.DOWNLOADS[lane["id"]] = {"repo": p.get("repo"), "file": p.get("file"), "done": False}
    return {"ok": True}, 200


def two_concurrent_starts(srv_mod, lane_id, loras_dir, start_fn):
    make_lane(srv_mod, lane_id, loras_dir)
    results = []
    def fire():
        obj = srv_mod.Handler.__new__(srv_mod.Handler)
        results.append(start_fn(obj, {"lane": lane_id, "repo": "owner/pack", "file": "style.safetensors"}))
    t1, t2 = threading.Thread(target=fire), threading.Thread(target=fire)
    t1.start(); t2.start(); t1.join(); t2.join()
    return [code for _, code in results]


L1_DIR = tempfile.mkdtemp(prefix="bwf_sec1_")
red.Handler.api_lora_download_start = _old_api_lora_download_start
codes_red = two_concurrent_starts(red, "l1red", L1_DIR, red.Handler.api_lora_download_start)
check("RED (pre-fix logic): both concurrent starts succeed -- the race is real",
      codes_red.count(200) == 2, codes_red)

_real_run_lora_download = srv._run_lora_download
srv._run_lora_download = lambda *a, **kw: None  # isolate the race from the actual transfer
codes_green = two_concurrent_starts(srv, "l1green", tempfile.mkdtemp(prefix="bwf_sec1g_"),
                                    srv.Handler.api_lora_download_start)
check("GREEN (fixed): exactly one concurrent start succeeds (200), the other is 409",
      sorted(codes_green) == [200, 409], codes_green)
srv._run_lora_download = _real_run_lora_download


# Verbatim pre-fix _run_lora_download (security-lora.md's own quoted lines
# 641-673): plain urlopen (no redirect pinning, Finding 2), plain open()
# (follows a pre-existing .part symlink, Finding 4), and a single
# `except Exception: state["error"] = str(e)` (leaks the absolute path,
# Finding 3). Used for every RED run below -- reproduced inline against the
# `red` module's own state, never re-imported from the fixed file.
def _old_run_lora_download(lane, url, dest, max_bytes, repo, filename):
    state = {"repo": repo, "file": filename, "bytes": 0, "total": None,
             "done": False, "ok": False, "error": "", "cancel": False}
    with red.DOWNLOAD_LOCK:
        red.DOWNLOADS[lane["id"]] = state
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60.0) as r:
            total = r.headers.get("Content-Length")
            state["total"] = int(total) if total and total.isdigit() else None
            if state["total"] and state["total"] > max_bytes:
                raise ValueError("That file is %d bytes, over the %d byte limit." % (state["total"], max_bytes))
            got = 0
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > max_bytes:
                        raise ValueError("That file is over the %d byte limit." % max_bytes)
                    f.write(chunk)
                    state["bytes"] = got
        os.replace(part, dest)
        state["ok"] = True
    except Exception as e:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
        state["error"] = str(e)
    finally:
        state["done"] = True


# ── Finding 2: redirect host pinning ────────────────────────────────────────
print()
print("Finding 2: a download redirect is only followed onto an allowed Hugging Face host")
check("allowed: huggingface.co itself", srv._allowed_download_host("huggingface.co"))
check("allowed: the real CDN host seen live 2026-09-28 (us.aws.cdn.hf.co)",
      srv._allowed_download_host("us.aws.cdn.hf.co"))
check("allowed: bare hf.co", srv._allowed_download_host("hf.co"))
check("refused: a lookalike suffix (huggingface.co.evil.example)",
      not srv._allowed_download_host("huggingface.co.evil.example"))
check("refused: an unrelated host", not srv._allowed_download_host("evil.example"))
check("refused: empty/None", not srv._allowed_download_host(None) and not srv._allowed_download_host(""))

import json, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


DISALLOWED_PORT = free_port()


class _Internal(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"INTERNAL-SECRET-CONTENT"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass


class _Redirector(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.server.target)
        self.end_headers()

    def log_message(self, *a): pass


internal = ThreadingHTTPServer(("127.0.0.1", 0), _Internal)
threading.Thread(target=internal.serve_forever, daemon=True).start()
redirector = ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
redirector.target = "http://127.0.0.1:%d/secret" % internal.server_address[1]
threading.Thread(target=redirector.serve_forever, daemon=True).start()
redirect_url = "http://127.0.0.1:%d/f" % redirector.server_address[1]

RED_DIR = tempfile.mkdtemp(prefix="bwf_sec2red_")
lane_red2 = make_lane(red, "l2red", RED_DIR)
dest_red = os.path.join(RED_DIR, "style.safetensors")
_old_run_lora_download(lane_red2, redirect_url, dest_red, 1 << 20, "owner/pack", "style.safetensors")
check("RED (pre-fix urlopen, no host pinning): the redirect to a different host IS followed, "
      "and the internal content lands on disk",
      os.path.isfile(dest_red) and open(dest_red, "rb").read() == b"INTERNAL-SECRET-CONTENT")

GREEN_DIR = tempfile.mkdtemp(prefix="bwf_sec2green_")
lane_green2 = make_lane(srv, "l2green", GREEN_DIR)
dest_green = os.path.join(GREEN_DIR, "style.safetensors")
srv._run_lora_download(lane_green2, redirect_url, dest_green, 1 << 20, "owner/pack", "style.safetensors")
check("GREEN (fixed): the redirect to a disallowed host is refused, no file lands",
      not os.path.isfile(dest_green))
with srv.DOWNLOAD_LOCK:
    check("GREEN: the recorded error names the refusal, not a raw exception",
          "untrusted" in srv.DOWNLOADS["l2green"]["error"], srv.DOWNLOADS["l2green"]["error"])

internal.shutdown()
redirector.shutdown()

# The ALLOW path, at the exact decision point (no live connection to a real
# host -- tests stay offline): redirect_request() must return quietly (no
# exception) for an https + allowed-host target, the same call shape used
# above for the disallowed case that DID raise.
handler = srv._PinnedRedirectHandler()
fake_req = urllib.request.Request(redirect_url)
try:
    handler.redirect_request(fake_req, None, 302, "Found", {},
                             "https://huggingface.co/owner/pack/resolve/main/style.safetensors")
    allowed_ok = True
except ValueError:
    allowed_ok = False
check("GREEN: redirect_request raises nothing for an https + allowed-host target",
      allowed_ok)
check("GREEN: redirect_request raises ValueError for an http (non-https) allowed-host target too",
      _raises(lambda: handler.redirect_request(
          fake_req, None, 302, "Found", {}, "http://huggingface.co/x")))


# ── Finding 3: a filesystem error's absolute path must not reach the client ─
print()
print("Finding 3: a download error never carries the absolute loras_dir")


def _serve_bytes(body):
    class _B(BaseHTTPRequestHandler):
        def do_GET(_self):
            _self.send_response(200)
            _self.send_header("Content-Length", str(len(body)))
            _self.end_headers()
            _self.wfile.write(body)

        def log_message(*_a): pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _B)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/f" % httpd.server_address[1]


RO_DIR = tempfile.mkdtemp(prefix="bwf_sec3_")
# The write must fail with a filesystem error whose own message carries the
# path. A read-only folder (chmod 0o500) does NOT fail for root -- the usual
# user in a container -- so the trigger is a file name over the 255-byte
# NAME_MAX instead: open() raises "File name too long: '<full path>'" for
# every user, and the folder itself exists, so it fails inside the download's
# own try, exactly where a permission error would.
# Windows caps the WHOLE path at 260 chars and raises [Errno 22] well before
# that, and it does NOT enforce NAME_MAX the way Linux does, so on nt the name
# is one Windows cannot create at all: '<' is illegal in a Windows file name
# and open() raises [Errno 22] Invalid argument with the full path in its text.
_F3_REPEAT = 300 if os.name != "nt" else max(8, 200 - len(RO_DIR) - len(".safetensors") - 2)
F3_DEST = (os.path.join(RO_DIR, "s" * _F3_REPEAT + ".safetensors") if os.name != "nt"
           else os.path.join(RO_DIR, "bad<name>.safetensors"))
httpd3, u3 = _serve_bytes(b"x" * 100)
lane_red3 = make_lane(red, "l3red", RO_DIR)
_old_run_lora_download(lane_red3, u3, F3_DEST, 1 << 20,
                       "owner/pack", "style.safetensors")
with red.DOWNLOAD_LOCK:
    err_red = red.DOWNLOADS["l3red"]["error"]
check("RED (pre-fix): the absolute loras_dir path IS present in the client-visible error",
      (RO_DIR in err_red or RO_DIR.replace("\\", "\\\\") in err_red),   # OSError's text repr()s the path: doubled backslashes on Windows
      err_red)
httpd3.shutdown()

httpd3b, u3b = _serve_bytes(b"x" * 100)
lane_green3 = make_lane(srv, "l3green", RO_DIR)
srv._run_lora_download(lane_green3, u3b, F3_DEST, 1 << 20,
                       "owner/pack", "style.safetensors")
with srv.DOWNLOAD_LOCK:
    err_green = srv.DOWNLOADS["l3green"]["error"]
check("GREEN (fixed): the absolute loras_dir path is NOT in the error",
      RO_DIR not in err_green, err_green)
check("GREEN: the error still names the file, so the message stays useful",
      "style.safetensors" in err_green, err_green)
httpd3b.shutdown()


# ── Finding 4: a pre-existing .part symlink must be refused, never followed ─
print()
print("Finding 4: a symlink planted at <file>.part is refused, not followed")

F4_DIR = tempfile.mkdtemp(prefix="bwf_sec4_")
OUTSIDE = tempfile.mkdtemp(prefix="bwf_sec4_outside_")
OUTSIDE_TARGET = os.path.join(OUTSIDE, "attacker_owned_file")
open(OUTSIDE_TARGET, "wb").write(b"BEFORE")


def plant_symlink(dest):
    part = dest + ".part"
    if os.path.exists(part) or os.path.islink(part):
        os.remove(part)
    os.symlink(OUTSIDE_TARGET, part)


httpd4, u4 = _serve_bytes(b"ATTACKER-CONTROLLED-BYTES")
lane_red4 = make_lane(red, "l4red", F4_DIR)
dest_red4 = os.path.join(F4_DIR, "a.safetensors")
plant_symlink(dest_red4)
_old_run_lora_download(lane_red4, u4, dest_red4, 1 << 20, "owner/pack", "a.safetensors")
check("RED (pre-fix plain open()): the symlink IS followed -- the outside file was overwritten",
      open(OUTSIDE_TARGET, "rb").read() == b"ATTACKER-CONTROLLED-BYTES")
httpd4.shutdown()

open(OUTSIDE_TARGET, "wb").write(b"BEFORE")   # reset for the GREEN run
httpd4b, u4b = _serve_bytes(b"ATTACKER-CONTROLLED-BYTES")
lane_green4 = make_lane(srv, "l4green", F4_DIR)
dest_green4 = os.path.join(F4_DIR, "b.safetensors")
plant_symlink(dest_green4)
srv._run_lora_download(lane_green4, u4b, dest_green4, 1 << 20, "owner/pack", "b.safetensors")
check("GREEN (fixed): the outside file is untouched",
      open(OUTSIDE_TARGET, "rb").read() == b"BEFORE")
check("GREEN: no file lands at the intended destination either", not os.path.isfile(dest_green4))
with srv.DOWNLOAD_LOCK:
    err4 = srv.DOWNLOADS["l4green"]["error"]
check("GREEN: the refusal is recorded, and (Finding 3) still carries no absolute path",
      err4 and F4_DIR not in err4, err4)
httpd4b.shutdown()


# ── Finding 5: a lane without "downloads" is refused with 403, not 400 ──────
print()
print("Finding 5: a lane without \"downloads\" opted in -> 403 (spec section C's last bullet)")


def _old_api_lora_download_start_403(self, p):
    """Verbatim pre-fix logic: ValueError from _lora_download_target (which
    covers BOTH 'downloads off' and every other refusal) always came back
    as 400 -- no distinction for the opt-in case."""
    lane = red.LANE_BY_ID.get(p.get("lane")) if isinstance(p.get("lane"), str) else None
    if not lane:
        return {"ok": False, "error": "Pick a lane first."}, 400
    try:
        # Reproduces the ORIGINAL _lora_download_target's first check (a
        # plain ValueError, before DownloadsOff existed).
        dl = lane.get("downloads")
        if not isinstance(dl, dict) or not dl.get("loras_dir"):
            raise ValueError("Downloads are off for %s." % lane["name"])
    except ValueError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True}, 200


OFF_LANE_RED = {"id": "l5red", "name": "Off lane", "caps": ["image"]}
red.LANE_BY_ID["l5red"] = OFF_LANE_RED
obj_red5 = red.Handler.__new__(red.Handler)
payload_red5, code_red5 = _old_api_lora_download_start_403(
    obj_red5, {"lane": "l5red", "repo": "owner/pack", "file": "style.safetensors"})
check("RED (pre-fix): a lane without \"downloads\" is refused with 400, not 403",
      code_red5 == 400, (payload_red5, code_red5))

OFF_LANE_GREEN = {"id": "l5green", "name": "Off lane", "caps": ["image"]}
srv.LANE_BY_ID["l5green"] = OFF_LANE_GREEN
obj_green5 = srv.Handler.__new__(srv.Handler)
payload_green5, code_green5 = srv.Handler.api_lora_download_start(
    obj_green5, {"lane": "l5green", "repo": "owner/pack", "file": "style.safetensors"})
check("GREEN (fixed): 403, spec's own status code", code_green5 == 403, (payload_green5, code_green5))
check("GREEN: still a plain sentence naming the lane",
      payload_green5 and "Off lane" in payload_green5.get("error", ""), payload_green5)
with srv.DOWNLOAD_LOCK:
    check("GREEN: the refused attempt leaves no placeholder claiming the download slot",
          "l5green" not in srv.DOWNLOADS)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
