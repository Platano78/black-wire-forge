"""Gate for the guide's server-side default: "helper".{"guide_default": "on"}.

The guide must be opt-in. With a helper configured but nothing said, the
server reports the guide as OFF (/api/engines helper_guide_on, /api/guide
guide_default_on), so a browser that has not chosen starts with the prompt
box as the main field. "on" turns that default on; anything that is not
exactly "on" or "off" is a config error and the server refuses to start.

server.py is loaded in-process from scratch configs (test_helper.py's
pattern), once per case, each on its own port; the refusal case runs
server.py as a subprocess and reads what it printed.

Run: python3 tests/test_guide_default.py
"""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer

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
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


ROOM = json.load(open(os.path.join(ROOT, "rooms.json"), encoding="utf-8"))[0]["id"]


def serve(name, helper):
    """server.py in-process on a scratch config -> (url, httpd). helper is the
    config's "helper" object, or None to leave the key out entirely."""
    scratch = tempfile.mkdtemp(prefix="bwf_guide_default_")
    data = os.path.join(scratch, "data")
    os.makedirs(data)
    cfg_path = os.path.join(scratch, "config.json")
    port = free_port()
    cfg = {"port": port, "bind": "127.0.0.1", "title": name,
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "c1", "name": "Comfy lane", "host": "127.0.0.1", "port": 1, "caps": ["image"]}]}
    if helper is not None:
        cfg["helper"] = helper
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    os.environ["GENCENTER_CONFIG"] = cfg_path
    os.environ["GENCENTER_DATA"] = data
    spec = importlib.util.spec_from_file_location("srv_gd_" + name, os.path.join(ROOT, "server.py"))
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return ("http://127.0.0.1:%d" % port), httpd, scratch


def http_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


HELPER = {"url": "http://127.0.0.1:1/v1", "model": "test-model", "timeout_s": 2}

SERVED = []
try:
    print('helper with "guide_default": "on" -> the guide starts on')
    url_on, httpd, scratch = serve("on", dict(HELPER, guide_default="on"))
    SERVED.append((httpd, scratch))
    eng = http_json(url_on + "/api/engines?lane=c1")
    gd = http_json(url_on + "/api/guide?room=" + ROOM)
    check("engines: helper is true", eng.get("helper") is True, repr(eng.get("helper")))
    check("engines: helper_guide_on is true", eng.get("helper_guide_on") is True, repr(eng.get("helper_guide_on")))
    check("guide: guide_default_on is true", gd.get("guide_default_on") is True, repr(gd.get("guide_default_on")))
    httpd.shutdown(); shutil.rmtree(scratch, ignore_errors=True)

    print("helper with no guide_default -> the guide starts off")
    url_plain, httpd, scratch = serve("plain", dict(HELPER))
    SERVED.append((httpd, scratch))
    eng = http_json(url_plain + "/api/engines?lane=c1")
    gd = http_json(url_plain + "/api/guide?room=" + ROOM)
    check("engines: helper is still true", eng.get("helper") is True, repr(eng.get("helper")))
    check("engines: helper_guide_on is false", eng.get("helper_guide_on") is False, repr(eng.get("helper_guide_on")))
    check("guide: guide_default_on is false", gd.get("guide_default_on") is False, repr(gd.get("guide_default_on")))
    httpd.shutdown(); shutil.rmtree(scratch, ignore_errors=True)

    print("no helper at all -> both are false")
    url_none, httpd, scratch = serve("none", None)
    SERVED.append((httpd, scratch))
    eng = http_json(url_none + "/api/engines?lane=c1")
    gd = http_json(url_none + "/api/guide?room=" + ROOM)
    check("engines: helper is false", eng.get("helper") is False, repr(eng.get("helper")))
    check("engines: helper_guide_on is false", eng.get("helper_guide_on") is False, repr(eng.get("helper_guide_on")))
    check("guide: guide_default_on is false", gd.get("guide_default_on") is False, repr(gd.get("guide_default_on")))
    httpd.shutdown(); shutil.rmtree(scratch, ignore_errors=True)

    print('"guide_default": "maybe" -> the server refuses to start, and says why')
    scratch = tempfile.mkdtemp(prefix="bwf_guide_default_bad_")
    cfg_path = os.path.join(scratch, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "bad",
                   "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
                   "lanes": [{"id": "c1", "name": "Comfy lane", "host": "127.0.0.1", "port": 1, "caps": ["image"]}],
                   "helper": dict(HELPER, guide_default="maybe")}, f)
    env = dict(os.environ, GENCENTER_CONFIG=cfg_path, GENCENTER_DATA=os.path.join(scratch, "data"))
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "server.py")], cwd=ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    said = proc.stdout.decode("utf-8", "replace")
    check("a bad guide_default: the server exits non-zero", proc.returncode != 0, repr(proc.returncode))
    check("a bad guide_default: the sentence names guide_default and says on or off",
          "guide_default" in said and '"on"' in said and '"off"' in said, said.strip())
    check("a bad guide_default: it never bound the port", "Traceback" not in said, said.strip())
    shutil.rmtree(scratch, ignore_errors=True)
finally:
    for httpd, scratch in SERVED:
        try:
            httpd.shutdown()
        except Exception:
            pass
        shutil.rmtree(scratch, ignore_errors=True)

if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")