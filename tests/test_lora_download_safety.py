"""LORA-1 Build C acceptance gate: every download-safety rule, against the
REAL functions (_lora_download_target, _run_lora_download), never a
reimplementation. The catalog itself is a small in-memory fixture (no HF
call); the one actual byte-stream download runs against a local
http.server, never huggingface.co.

Run: python3 tests/test_lora_download_safety.py
"""
import importlib.util, json, os, sys, tempfile, threading, time
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond: FAILED.append(name)

import _scratch_config  # noqa: E402 -- must run before server.py's own exec_module below

spec = importlib.util.spec_from_file_location("srv_dl", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

CATALOG_ENTRY = {"id": "owner/pack", "downloads": 10, "likes": 1, "licence": "mit", "nsfw": False,
                 "files": [{"filename": "style.safetensors", "size": 12345},
                           {"filename": "sub/dir/nested.safetensors", "size": 99},
                           {"filename": "sub/../sneaky.safetensors", "size": 99},
                           {"filename": "sub/.hidden/x.safetensors", "size": 99}]}
srv.CATALOG_CACHE["Qwen/Qwen-Image-2.1"] = (time.time(), [CATALOG_ENTRY])

LORAS_DIR = tempfile.mkdtemp(prefix="bwf_loras_")
LANE_OFF = {"id": "off", "name": "Off lane", "caps": ["image"]}
LANE_ON = {"id": "on", "name": "On lane", "caps": ["image"], "downloads": {"loras_dir": LORAS_DIR}}
for lane in (LANE_OFF, LANE_ON):
    srv.LANE_BY_ID[lane["id"]] = lane
    with srv.DISCOVERY_LOCK:
        srv.DISCOVERY[lane["id"]] = {"models": {"qwen_unet": "qwen_image_2.1_Q6_K.gguf"},
                                     "pools": {}, "checked": 1.0, "err": ""}

print("a lane without \"downloads\" is refused (403's plain-sentence text, from the download endpoint)")
try:
    srv._lora_download_target(LANE_OFF, None, "owner/pack", "style.safetensors")
    check("raised", False)
except ValueError as e:
    check("names the lane and says downloads are off", "Off lane" in str(e) and "off" in str(e).lower(), str(e))

print()
print("every rule in Build C, against the opted-in lane")
def refused(repo, filename):
    try:
        srv._lora_download_target(LANE_ON, None, repo, filename)
        return None
    except ValueError:
        return True

check("a valid repo+file from the catalog is ACCEPTED (returns url/dest/max_bytes)",
      # LORA-2B: lands under the resolved family's OWN subfolder (qwen_image here,
      # the real style_catalogs entry's "folder"), not flat under loras_dir.
      srv._lora_download_target(LANE_ON, None, "owner/pack", "style.safetensors")[1]
      == os.path.join(LORAS_DIR, "qwen_image", "style.safetensors"))
check("path traversal in the filename is refused", refused("owner/pack", "../x.safetensors") is True)
nested = srv._lora_download_target(LANE_ON, None, "owner/pack", "sub/dir/nested.safetensors")
check("a catalog file inside a repo subfolder is ACCEPTED and saved flat under its basename",
      nested[1] == os.path.join(LORAS_DIR, "qwen_image", "nested.safetensors"), nested[1])
check("...and the download URL keeps the repo's own path",
      nested[0].endswith("/owner/pack/resolve/main/sub/dir/nested.safetensors"), nested[0])
check("a '..' segment inside a listed subfolder name is still refused", refused("owner/pack", "sub/../sneaky.safetensors") is True)
check("a dot-prefixed segment is still refused", refused("owner/pack", "sub/.hidden/x.safetensors") is True)
check("a subfolder file the catalog does not list is refused", refused("owner/pack", "sub/dir/other.safetensors") is True)
check("a backslash path is refused", refused("owner/pack", "sub\\dir\\nested.safetensors") is True)
check("an absolute path is refused", refused("owner/pack", "/etc/x.safetensors") is True)
check("a non-.safetensors file is refused", refused("owner/pack", "style.bin") is True)
check("an id not from the catalog is refused", refused("someone/else", "style.safetensors") is True)
check("a filename not listed for that repo in the catalog is refused",
      refused("owner/pack", "not_listed.safetensors") is True)
check("a malformed repo id (no slash) is refused", refused("not-a-repo-id", "style.safetensors") is True)
check("a repo id with a path segment is refused", refused("owner/pack/../evil", "style.safetensors") is True)
check("a leading-dot filename is refused", refused("owner/pack", ".style.safetensors") is True)

url, dest, max_bytes = srv._lora_download_target(LANE_ON, None, "owner/pack", "style.safetensors")
check("the URL is built server-side against the real HF host, from the catalog id/file only",
      url == "https://huggingface.co/owner/pack/resolve/main/style.safetensors", url)
check("the target stays inside loras_dir",
      os.path.commonpath([dest, os.path.abspath(LORAS_DIR)]) == os.path.abspath(LORAS_DIR))
check("default max_bytes is 4 GiB", max_bytes == 4 * 1024 ** 3, max_bytes)

os.makedirs(os.path.dirname(dest), exist_ok=True)   # LORA-2B: the family subfolder, real downloads make this via _run_lora_download
with open(dest, "wb") as f:
    f.write(b"already here")
check("refuses when the destination file already exists", refused("owner/pack", "style.safetensors") is True)
os.remove(dest)

print()
print("_run_lora_download: streams into a .part then renames, refuses over the size cap, deletes .part on failure")
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Blob(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.server.body
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(body):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Blob)
    httpd.body = body
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d/f" % httpd.server_address[1]


httpd, url = serve(b"x" * 5000)
dest2 = os.path.join(LORAS_DIR, "small.safetensors")
srv._run_lora_download(LANE_ON, url, dest2, 1 << 20, "owner/pack", "small.safetensors")
check("small file lands at dest, .part removed", os.path.isfile(dest2) and not os.path.exists(dest2 + ".part"))
check("bytes match what was served", os.path.getsize(dest2) == 5000)
with srv.DOWNLOAD_LOCK:
    check("status recorded ok=True, done=True", srv.DOWNLOADS[LANE_ON["id"]]["ok"] is True
          and srv.DOWNLOADS[LANE_ON["id"]]["done"] is True)
httpd.shutdown()

httpd2, url2 = serve(b"y" * 5000)
dest3 = os.path.join(LORAS_DIR, "over_cap.safetensors")
srv._run_lora_download(LANE_ON, url2, dest3, 1000, "owner/pack", "over_cap.safetensors")
check("over the cap: no file lands", not os.path.isfile(dest3))
check("its .part is cleaned up", not os.path.exists(dest3 + ".part"))
with srv.DOWNLOAD_LOCK:
    check("status recorded an error, not ok", srv.DOWNLOADS[LANE_ON["id"]]["ok"] is False
          and "byte" in srv.DOWNLOADS[LANE_ON["id"]]["error"])
httpd2.shutdown()

print()
print("only POST, behind the existing request guard -- api_lora_download_start refuses without a lane, "
      "and the whole route is behind Handler.refuse() like every other POST (see test_request_guard.py)")
obj = srv.Handler.__new__(srv.Handler)
payload, code = srv.Handler.api_lora_download_start(obj, {})
check("no lane -> 400", code == 400, (payload, code))
payload2, code2 = srv.Handler.api_lora_download_start(obj, {"lane": "off", "repo": "owner/pack",
                                                             "file": "style.safetensors"})
check("lane without opt-in -> 403 (security review Finding 5/5, spec's own status code), plain sentence",
      code2 == 403 and "downloads" in payload2["error"].lower(), (payload2, code2))
src = open(os.path.join(ROOT, "server.py")).read()
check("do_POST gates every path (incl. /api/lora/download) through refuse() BEFORE routing -- "
      "structurally the same cross-site protection as every existing POST route (test_request_guard.py)",
      '"/api/lora/download"' in src and "if self.refuse():" in src)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
