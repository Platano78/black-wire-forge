"""Fake ComfyUI lane for tests/test_ui_smoke.py and tests/test_refs.py.

Serves exactly the slice of the ComfyUI wire protocol that server.py's lane
poller, discovery and carry() use, so those tests can run with zero real
hardware:

  GET /system_stats        fake version string, one fake 16 GiB GPU, 1 byte ram_free
  GET /queue               empty queue (running and pending)
  GET /object_info/<Node>  real dropdown contents, built from
                           tests/golden/pools_rig.json (captured from the real
                           rig by capture_pools_rig.py), merged with
                           tests/fixtures/pools_extra.json when that file
                           exists (same shape: pool -> {"Node.field": opts};
                           the merge is by exact "Node.field" key).
                           Pools whose entries never match a pack rule are
                           harmless: pick_model finds no candidate and the
                           mode stays unavailable.
  POST /prompt             500 by default -- this lane can never render
                           anything, so a job can never be started
                           end-to-end here UNLESS told otherwise (below).
  POST /_control/queue     {"running": [[number, prompt_id], ...],
                           "pending": [...]} sets /queue's contents (default
                           empty). Test-only; lets a test keep a prompt_id live
                           so job_poller's lost-job rule can see it.
  POST /_control/vram      {"vram_free": <bytes>} sets the fake GPU's
                           /system_stats vram_free (default: fully free, all
                           16 GiB). Test-only; lets a test move the machine
                           chip's meter without touching vram_total.
  POST /_control/accept    {"outputs": [{"filename":..., "subfolder":...,
                           "type":..., "node": "9"}, ...]} (node optional,
                           default "9") flips /prompt to ACCEPT: it returns
                           200 {"prompt_id": <uuid>} and records a completed
                           /history/<prompt_id> entry with those outputs
                           under their node id, so a real job_poller sees a
                           finished job. Persists across requests; test-only,
                           never used by server.py itself. {"outputs": null}
                           (or no body) turns it back off (the smoke test's
                           "nothing renders" default). Every POST /prompt
                           (accepted or not) also writes DIR/last_prompt.json
                           -- the graph dict a test can inspect.
  GET  /history/<id>       the completed entry set by /_control/accept, or
                           {} if unknown (ComfyUI's own shape for "not yet").
  POST /upload/image       multipart; stores the bytes under DIR/inputs/ (the
                           --store dir, a temp dir by default) and answers
                           {"name", "subfolder": "", "type": "input"}. A file
                           of that name with IDENTICAL bytes already present
                           answers the same name without writing a second
                           file (ComfyUI's own hash-compare, as carry()'s
                           docstring remembers it); different bytes under the
                           same name get "<stem> (1)<ext>".
  GET /view?filename=X&type=output
                           serves DIR/outputs/X if present, else 404. type=input
                           serves DIR/inputs/X instead (what an uploaded file
                           lands under, real ComfyUI's own type split).
  anything else            404

Every request is appended as one JSON line to DIR/requests.log (method,
path, and for an upload the filename and byte length) -- carry()'s "never
contacts the source lane" claims are checked against this file, not guessed
from behaviour.

The 16 GiB figure is deliberate: it is under server.py's 25 GB "small card"
line, so quantised builds win discovery exactly as they would on a real rig.
The 1 byte ram_free is deliberate too: it makes the page show an honest "RAM
not measured" instead of inventing a free-RAM figure.

Run standalone (useful to eyeball it):

    python3 tests/fixtures/fake_comfy.py --port 8123
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RIG_POOLS = os.path.join(HERE, os.pardir, "golden", "pools_rig.json")
EXTRA_POOLS = os.path.join(HERE, "pools_extra.json")


def parse_multipart(body, boundary):
    """A ~25-line port of server.py's own parse_multipart (cgi is deprecated/
    removed; this fixture stays self-contained rather than depend on it)."""
    parts = []
    delim = b"--" + boundary
    for chunk in body.split(delim):
        if not chunk or chunk[:2] == b"--":
            continue
        chunk = chunk.lstrip(b"\r\n")
        if b"\r\n\r\n" not in chunk:
            continue
        raw_head, data = chunk.split(b"\r\n\r\n", 1)
        if data.endswith(b"\r\n"):
            data = data[:-2]
        head = {}
        for line in raw_head.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                head[k.strip().lower()] = v.strip()
        disp = head.get("content-disposition", "")
        name = re.search(r'name="([^"]*)"', disp)
        fname = re.search(r'filename="([^"]*)"', disp)
        parts.append({
            "name": name.group(1) if name else "",
            "filename": fname.group(1) if fname else None,
            "data": data,
        })
    return parts


def _object_info():
    """pools (rig golden + optional extra) -> per-node /object_info payloads."""
    pools = {}
    for path in (RIG_POOLS, EXTRA_POOLS):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for pool, entries in json.load(f).items():
                pools.setdefault(pool, {}).update(entries)
    nodes = {}
    for entries in pools.values():
        for key, opts in entries.items():
            node, field = key.split(".", 1)
            nodes.setdefault(node, {})[field] = opts
    return nodes


NODES = _object_info()

SYSTEM_STATS = {
    "system": {"comfyui_version": "fake", "ram_free": 1},
    "devices": [{"name": "fake gpu", "vram_total": 17179869184,
                 "vram_free": 17179869184, "torch_vram_total": 0}],
}
QUEUE = {"queue_running": [], "queue_pending": []}

STORE_DIR = None   # set in main(); --store DIR (default a temp dir)
ACCEPT_OUTPUTS = None   # None = /prompt always 500 (default); a list = ACCEPT mode
HISTORY = {}             # prompt_id -> the /history/<id> entry once accepted


def _log_request(method, path, filename=None, length=None):
    entry = {"method": method, "path": path}
    if filename is not None:
        entry["filename"] = filename
        entry["length"] = length
    with open(os.path.join(STORE_DIR, "requests.log"), "a") as f:
        f.write(json.dumps(entry) + "\n")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        _log_request("GET", path)
        if path == "/system_stats":
            self._send(200, SYSTEM_STATS)
        elif path == "/queue":
            self._send(200, QUEUE)
        elif path.startswith("/object_info/"):
            node = path[len("/object_info/"):].strip("/")
            if node in NODES:
                self._send(200, {node: {"input": {"required": NODES[node]}}})
            else:
                self._send(404, {"error": "no such node"})
        elif path.startswith("/history/"):
            pid = path[len("/history/"):].strip("/")
            self._send(200, {pid: HISTORY[pid]} if pid in HISTORY else {})
        elif path == "/view":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            filename = (qs.get("filename") or [""])[0]
            kind = "inputs" if (qs.get("type") or ["output"])[0] == "input" else "outputs"
            src = os.path.join(STORE_DIR, kind, os.path.basename(filename))
            if filename and os.path.isfile(src):
                with open(src, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send(404, {"error": "no such output"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        global ACCEPT_OUTPUTS
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/_control/accept"):
            data = json.loads(body) if body else {}
            ACCEPT_OUTPUTS = data.get("outputs")
            self._send(200, {"accept": ACCEPT_OUTPUTS is not None})
        elif self.path.startswith("/_control/queue"):
            global QUEUE
            data = json.loads(body) if body else {}
            QUEUE = {"queue_running": data.get("running", []),
                     "queue_pending": data.get("pending", [])}
            self._send(200, {"queue": QUEUE})
        elif self.path.startswith("/_control/vram"):
            data = json.loads(body) if body else {}
            if "vram_free" in data:
                SYSTEM_STATS["devices"][0]["vram_free"] = data["vram_free"]
            self._send(200, {"vram_free": SYSTEM_STATS["devices"][0]["vram_free"]})
        elif self.path.startswith("/prompt"):
            _log_request("POST", "/prompt")
            try:
                with open(os.path.join(STORE_DIR, "last_prompt.json"), "w") as f:
                    json.dump(json.loads(body).get("prompt"), f)
            except Exception:
                pass
            if ACCEPT_OUTPUTS is None:
                self._send(500, {"error": "fake lane never renders"})
            else:
                pid = uuid.uuid4().hex
                outputs = {}
                for o in ACCEPT_OUTPUTS:
                    node = o.get("node", "9")
                    outputs.setdefault(node, {"images": []})["images"].append(
                        {"filename": o["filename"], "subfolder": o.get("subfolder", ""),
                         "type": o.get("type", "output")})
                HISTORY[pid] = {"status": {"completed": True, "status_str": "success", "messages": []},
                                 "outputs": outputs}
                self._send(200, {"prompt_id": pid, "number": 0, "node_errors": {}})
        elif self.path.startswith("/upload/image"):
            self._upload(body)
        else:
            _log_request("POST", self.path)
            self._send(404, {"error": "not found"})

    def _upload(self, body):
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if not m:
            self._send(400, {"error": "no multipart boundary"})
            return
        parts = parse_multipart(body, m.group(1).encode())
        part = next((p for p in parts if p.get("filename")), None)
        if not part:
            self._send(400, {"error": "no file part"})
            return
        filename, data = part["filename"], part["data"]
        _log_request("POST", "/upload/image", filename=filename, length=len(data))
        self._send(200, self._store(filename, data))

    def _store(self, filename, data):
        inputs = os.path.join(STORE_DIR, "inputs")
        os.makedirs(inputs, exist_ok=True)
        stem, ext = os.path.splitext(filename)
        dest, path = filename, os.path.join(inputs, filename)
        if os.path.exists(path):
            with open(path, "rb") as f:
                existing = f.read()
            if hashlib.sha256(existing).digest() != hashlib.sha256(data).digest():
                dest = "%s (1)%s" % (stem, ext)
                path = os.path.join(inputs, dest)
                with open(path, "wb") as f:
                    f.write(data)
        else:
            with open(path, "wb") as f:
                f.write(data)
        return {"name": dest, "subfolder": "", "type": "input"}

    def log_message(self, fmt, *args):
        pass    # keep the test's stdout clean


def main():
    global STORE_DIR
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--store", default=None, help="dir for uploads/outputs/requests.log (default: a temp dir)")
    args = ap.parse_args()
    if args.store:
        STORE_DIR = args.store
        os.makedirs(STORE_DIR, exist_ok=True)
    else:
        import tempfile
        STORE_DIR = tempfile.mkdtemp(prefix="fake_comfy_")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print("fake comfyui lane on 127.0.0.1:%d (%d nodes, store %s)" % (args.port, len(NODES), STORE_DIR),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
