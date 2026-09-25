"""Gate: /api/view on a lane that is down, or that no longer has the file,
answers with one plain sentence (502 / 404) -- never the generic 500 with a
raw urlopen error in it. No browser.

Run: python3 tests/test_view_lane_down.py
"""
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
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


class NoSuchFile(BaseHTTPRequestHandler):          # a ComfyUI that answers, without the file
    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


comfy = ThreadingHTTPServer(("127.0.0.1", 0), NoSuchFile)
threading.Thread(target=comfy.serve_forever, daemon=True).start()
scratch = tempfile.mkdtemp(prefix="bwf_view_down_")
cfg, port = os.path.join(scratch, "config.json"), free_port()
with open(cfg, "w") as f:
    json.dump({"port": port, "bind": "127.0.0.1", "timing": {"poll_seconds": 30},
               "lanes": [{"id": "down", "name": "Box A", "host": "127.0.0.1", "port": free_port()},
                         {"id": "nofile", "name": "Box B", "host": "127.0.0.1",
                          "port": comfy.server_address[1]}]}, f)
os.environ["GENCENTER_CONFIG"] = cfg
os.environ["GENCENTER_DATA"] = os.path.join(scratch, "data")
spec = importlib.util.spec_from_file_location("srv_view_down", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
httpd = ThreadingHTTPServer(("127.0.0.1", port), srv.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def view(lane):
    url = "http://127.0.0.1:%d/api/view?lane=%s&filename=a.png&subfolder=&type=output&dl=1" % (port, lane)
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


print("a lane that is not answering")
code, body = view("down")
check("502 with a sentence naming the lane", code == 502 and isinstance(body, dict)
      and body.get("error", "").startswith("Box A is not answering"), (code, body))
check("no raw error text or detail", isinstance(body, dict) and "detail" not in body
      and "Errno" not in json.dumps(body), body)

print("a lane that answers but no longer has the file")
code, body = view("nofile")
check("404 with a sentence naming the lane", code == 404 and isinstance(body, dict)
      and body.get("error") == "Box B does not have that file any more.", (code, body))

if FAILED:
    print("FAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("OK: all checks passed")
