"""Acceptance gate for portability fix P1: numpy/Pillow are not stdlib, and
before this fix engines/_quantise.py imported both at module top -- reached
at server startup via engines/pixelart.py -> engines/__init__.py's
_discover(), so a machine with neither installed could not even bind the
server. This blocks numpy/PIL from being imported (the way a stranger's
fresh machine behaves) and checks the server starts anyway, /api/engines
answers, and the pixelart mode reports itself unavailable with a sentence
naming the fix.

RED on the pre-fix tree: server.py's own import chain raises ImportError
before it can be exec'd at all.

Run: python3 tests/test_portability_deps.py
"""
import builtins
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond else ""))
    if not cond:
        FAILED.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


_real_import = builtins.__import__


def _blocking_import(name, *a, **k):
    if name == "numpy" or name.startswith("numpy.") or name == "PIL" or name.startswith("PIL."):
        raise ImportError("No module named %r (blocked for this test)" % name)
    return _real_import(name, *a, **k)


SCRATCH = tempfile.mkdtemp(prefix="bwf_p1_deps_")
CONFIG = os.path.join(SCRATCH, "config.json")
API_PORT = free_port()
with open(CONFIG, "w") as f:
    json.dump({
        "port": API_PORT, "bind": "127.0.0.1", "title": "p1-deps",
        "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
        # A non-empty lane is required (config.json refuses an empty list);
        # its port is never actually dialled by this test.
        "lanes": [{"id": "t", "name": "Test lane", "host": "127.0.0.1", "port": 1, "caps": ["image"]}],
    }, f)
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")

print("server.py starts with numpy/Pillow unimportable")
builtins.__import__ = _blocking_import
try:
    spec = importlib.util.spec_from_file_location("srv_p1_deps", os.path.join(ROOT, "server.py"))
    srv = importlib.util.module_from_spec(spec)
    import_error = None
    try:
        spec.loader.exec_module(srv)
    except ImportError as e:
        import_error = e
finally:
    builtins.__import__ = _real_import

check("server.py imports without raising", import_error is None, import_error)
if import_error is not None:
    print()
    print("FAILED: %d: %s" % (len(FAILED) or 1, "server import"))
    sys.exit(1)

from http.server import ThreadingHTTPServer

HTTPD = ThreadingHTTPServer(("127.0.0.1", API_PORT), srv.Handler)
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()

with urllib.request.urlopen("http://127.0.0.1:%d/api/engines" % API_PORT) as r:
    body = json.loads(r.read())
check("/api/engines answers", bool(body))

pixelart_mode = next((mo for mo in body.get("image", {}).get("modes", [])
                       if mo["id"] == "pixelart"), None)
check("pixelart mode is still listed", pixelart_mode is not None, body.get("image"))
if pixelart_mode:
    check("pixelart reports unavailable", pixelart_mode.get("available") is False, pixelart_mode)
    sentence = ("Pixel art needs the numpy and Pillow Python packages: "
                "python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt")
    check("pixelart's missing list carries the fix sentence",
          sentence in (pixelart_mode.get("missing") or []), pixelart_mode.get("missing"))

builtins.__import__ = _blocking_import
try:
    pil_err = None
    try:
        srv._pil()
    except RuntimeError as e:
        pil_err = str(e)
finally:
    builtins.__import__ = _real_import
check("_pil()'s Pillow-missing message leads with a venv, not a bare pip install",
      bool(pil_err) and ".venv/bin/python -m pip install Pillow" in pil_err,
      pil_err)

HTTPD.shutdown()

print()
if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("All P1 (numpy/Pillow optional) checks passed.")
