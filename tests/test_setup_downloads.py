"""W3 one-click model downloads, against the REAL functions and a real server.py, and only ever a local fake
Hugging Face (tests/_fake_hf.py, reached through server.py's test-only 127.0.0.1-pinned hooks):

(1) the models folder: each failed check has its own sentence; a symlinked folder resolves to its real path;
    a folder that cannot be written to is refused;
(2) containment: a manifest entry with "../" or an absolute folder/subdir, a subfolder that is a link leading
    outside, or a downloads.json edited to point elsewhere never writes outside the models folder;
(3) size: a server sending more than the expected size is cut off and the file refused; a Content-Length
    that is not the expected size (or none) is refused before a .part exists; the exact size is renamed into
    place; a file already there at its size is Installed without a request; a different file is left alone;
(4) resume: the server is killed mid-file and restarted; the queue resumes with Range and the file is
    byte-identical; a .part that is a link or not ours is refused; Cancel keeps the .part, Discard removes it;
(5) gated: 403 -> skipped with a sentence naming the repo page and "needs a Hugging Face login"; HF_TOKEN goes
    to the Hugging Face host only (never the CDN, never echoed); a redirect off the pinned hosts is refused;
(6) pre-flight: too little free space (disk_usage monkeypatched) -> a refusal naming both numbers; more bytes
    than the page showed -> refused; "not run by us" files are never planned;
(8) the downloads routes answer 403 on a server bound beyond localhost; start/models-folder 404 once configured.

Needs port 3998 free (Setup mode's port). Writes ~70 MB under the scratch folder (TMPDIR).
Run: python3 tests/test_setup_downloads.py
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import _fake_hf  # noqa: E402

HF = _fake_hf.serve()
CDN = _fake_hf.serve()
ELSEWHERE = _fake_hf.serve()          # NOT pinned: a redirect here must be refused
HF.redirect_to = CDN.origin
os.environ["BWF_TEST_HF_DOWNLOAD"] = HF.origin
os.environ["BWF_TEST_HF_CDN"] = CDN.origin
os.environ.pop("HF_TOKEN", None)

import _setup_fixture as fx  # noqa: E402
from _setup_fixture import check  # noqa: E402
import _scratch_config  # noqa: E402,F401 -- before server.py's own import-time config read

spec = importlib.util.spec_from_file_location("srv_w3", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

W = os.path.join(fx.SCRATCH, "w3")
os.makedirs(W)
BG = {"repo": "ZhengPeng7/BiRefNet", "file": "model.safetensors", "size": 444_473_596, "folder": "background_removal"}
UP = {"repo": "schwgHao/RealESRGAN_x4plus", "file": "RealESRGAN_x4plus.pth", "size": 67_040_989,
      "folder": "upscale_models"}


def gate(name, fn):
    """Run one gate; a crash (e.g. the function does not exist yet) is that gate's FAIL, not the suite's end."""
    print("\n" + name)
    try:
        fn()
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(name + ": ran", False, repr(e))


def models_folder(name, subs=("checkpoints", "vae", "loras", "upscale_models", "background_removal")):
    d = os.path.join(W, name)
    for s in subs:
        os.makedirs(os.path.join(d, s), exist_ok=True)
    return d


def entry(src, size=None, **over):
    e = {"room": "t", "repo": src["repo"], "file": src["file"], "folder": src["folder"],
         "subdir": src.get("subdir") or "", "dest": "", "expected": size or src["size"], "state": "running",
         "error": ""}
    e.update(over)
    return e


def serve_as(src, size, mode="ok"):
    path = _fake_hf.file_path(src["repo"], src["file"])
    HF.sizes[path], HF.routes[path] = size, mode
    return path


def one(root, e):
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(models=root)
    return srv._dl_one(root, e)


def own(root, rel):
    """Mark a .part as this app's own, the way the app records one it creates (its identity)."""
    if hasattr(srv, "_part_set"):
        srv._part_set(rel, os.lstat(os.path.join(root, rel)))
    else:                         # the tree before identity records: a bare path string
        with srv.MODEL_DL_LOCK:
            srv.MODEL_DL["parts"].append(rel)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# -- (1) the models folder ------------------------------------------------------------------------------------
def g1():
    good = models_folder("m1")
    few = models_folder("m1few", ("checkpoints", "vae"))
    afile = os.path.join(W, "a-file")
    open(afile, "w").close()
    for label, path, want in (("empty", "", "Type the path"),
                              ("relative", "ComfyUI/models", "whole path"),
                              ("missing", os.path.join(W, "nope"), "Nothing is at that path"),
                              ("a file", afile, "a file, not a folder"),
                              ("too few ComfyUI folders", few, "holds 2 of ComfyUI's usual folders")):
        real, why = srv._models_folder_check(path)
        check("%s -> refused with its own sentence" % label, real is None and why and want in why, why)
    link = os.path.join(W, "m1-link")
    os.symlink(good, link)
    real, why = srv._models_folder_check(link)
    check("a symlinked folder is accepted and resolves to its real path",
          why is None and real == os.path.realpath(good), (real, why))
    ro = models_folder("m1ro")
    os.chmod(ro, 0o555)
    try:
        if os.name == "nt":
            print("  SKIP  a folder that cannot be written to is refused, saying so (POSIX-only)")
        elif hasattr(os, "geteuid") and os.geteuid() == 0:
            print("  SKIP  the non-writable folder check: running as root, who can write anywhere")
        else:
            real, why = srv._models_folder_check(ro)
            check("a folder that cannot be written to is refused, saying so",
                  real is None and "cannot write" in why, why)
    finally:
        os.chmod(ro, 0o755)
    real, why = srv._models_folder_check(good)
    check("a good folder passes and the write probe leaves nothing behind",
          why is None and not [n for n in os.listdir(good) if n.startswith(".bwf")], why)


# -- (2) containment ------------------------------------------------------------------------------------------
def g2():
    root = os.path.realpath(models_folder("m2"))
    outside = os.path.join(W, "outside2")
    os.makedirs(outside)
    planted = [("a subdir with ../", {"folder": "loras", "subdir": "../../outside2"}),
               ("an absolute subdir", {"folder": "loras", "subdir": outside}),
               ("an absolute folder", {"folder": outside}),
               ("a folder of ..", {"folder": ".."}),
               ("an empty folder", {"folder": ""})]
    for label, over in planted:
        src = dict(UP, **over)
        try:
            srv._model_dest(root, src)
            raised = False
        except ValueError:
            raised = True
        check("%s: refused by the containment check" % label, raised)
        state, why = one(root, entry(src, 64))
        check("%s: the queue refuses it too, with a sentence" % label, state == "failed" and "outside" in why,
              (state, why))
    src = dict(UP, file="../../../evil.pth")
    check("a file path with ../ lands as its basename, inside its folder",
          srv._model_dest(root, src)[1] == os.path.join(root, "upscale_models", "evil.pth"))
    check("nothing was written outside the models folder", os.listdir(outside) == [], os.listdir(outside))
    # A downloads.json edited to point elsewhere: every entry is re-derived from the packs' own sources.
    os.makedirs(os.path.dirname(srv.MODEL_DL_FILE), exist_ok=True)
    with open(srv.MODEL_DL_FILE, "w") as f:
        json.dump({"models": root, "parts": ["../outside2/x.part", "/etc/passwd.part", "upscale_models/y.part"],
                   "queue": [dict(entry(UP), dest="../outside2/RealESRGAN_x4plus.pth", folder="../outside2",
                                  state="queued"),
                             dict(entry(UP), repo="evil/repo", state="queued"),
                             dict(entry(UP), expected=5, state="queued")]}, f)
    srv._dl_load()
    q, parts = srv.MODEL_DL["queue"], srv.MODEL_DL["parts"]
    check("an edited downloads.json: the entry's folder and dest come from the manifest, not the file",
          len(q) == 1 and q[0]["dest"].replace("\\", "/") == "upscale_models/RealESRGAN_x4plus.pth"
          and q[0]["folder"] == "upscale_models", q)
    check("... an entry that is not a manifest file, or not at its manifest size, is dropped", len(q) == 1, q)
    check("... .part records that are only a path (no identity), or outside the models folder, are dropped",
          parts == [], parts)
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(queue=[], parts=[])


# -- REV-A: a models subfolder that is the user's own link to another drive -----------------------------------
def g_rev_a():
    root = os.path.realpath(models_folder("ma"))
    drive = os.path.realpath(os.path.join(W, "other-drive", "diffusion_models"))
    os.makedirs(drive)
    os.symlink(drive, os.path.join(root, "diffusion_models"))
    big = {"repo": "owner/big", "file": "diffusion_models/big.safetensors", "folder": "diffusion_models"}
    path = serve_as(big, 50_000)
    state, why = one(root, entry(big, 50_000))
    landed = os.path.join(drive, "big.safetensors")
    check("a symlinked diffusion_models -> another folder of ours: the file lands in the target",
          state == "done" and os.path.isfile(landed) and not os.path.islink(landed)
          and open(landed, "rb").read() == _fake_hf.content(path, 50_000), (state, why))
    sub = {"repo": "owner/sub", "file": "s.safetensors", "folder": "diffusion_models", "subdir": "deeper"}
    serve_as(sub, 1000)
    state, why = one(root, entry(sub, 1000))
    check("... a new subdir under the linked folder is created in the target (never a link)",
          state == "done" and os.path.isfile(os.path.join(drive, "deeper", "s.safetensors"))
          and not os.path.islink(os.path.join(drive, "deeper")), (state, why))
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(models=root, queue=[], parts=[])
    to = [t for r in srv.setup_rooms({})[0]["rooms"] for t in (r.get("download") or {}).get("to", [])]
    dm = [t for t in to if t["where"].startswith("diffusion_models")]
    check("the confirm data names each file's REAL destination (the resolved path)",
          dm and all(t["dest"].startswith(drive + os.sep) for t in dm)
          and all(t["dest"].startswith(root + os.sep) for t in to if t not in dm), dm[:2])
    ro = os.path.join(W, "other-drive", "readonly")
    os.makedirs(ro)
    os.chmod(ro, 0o555)
    os.rmdir(os.path.join(root, "vae"))
    os.symlink(ro, os.path.join(root, "vae"))
    v = {"repo": "owner/v", "file": "v.safetensors", "folder": "vae"}
    serve_as(v, 100)
    try:
        if os.name == "nt":
            print("  SKIP  a symlinked subfolder whose target cannot be written to: refused with a sentence "
                  "(POSIX-only)")
        elif hasattr(os, "geteuid") and os.geteuid() == 0:
            print("  SKIP  the not-writable link target: running as root, who can write anywhere")
        else:
            state, why = one(root, entry(v, 100))
            check("a symlinked subfolder whose target cannot be written to: refused with a sentence",
                  state == "failed" and "vae" in why and "cannot write" in why and os.listdir(ro) == [],
                  (state, why))
    finally:
        os.chmod(ro, 0o755)
    if hasattr(os, "geteuid") and os.geteuid() != 0 and os.stat("/usr/share").st_uid != os.getuid():
        os.rmdir(os.path.join(root, "checkpoints"))
        os.symlink("/usr/share", os.path.join(root, "checkpoints"))
        c = {"repo": "owner/c", "file": "c.safetensors", "folder": "checkpoints"}
        serve_as(c, 100)
        state, why = one(root, entry(c, 100))
        check("a symlinked subfolder whose target another user owns: refused with a sentence",
              state == "failed" and "belongs to another user" in why
              and not os.path.exists("/usr/share/c.safetensors"), (state, why))
    afile = os.path.join(W, "other-drive", "a-file")
    open(afile, "w").close()
    os.symlink(afile, os.path.join(root, "clip"))
    k = {"repo": "owner/k", "file": "k.safetensors", "folder": "clip"}
    serve_as(k, 100)
    state, why = one(root, entry(k, 100))
    check("a subfolder link to a file, not a folder: refused", state == "failed" and "not a folder" in why,
          (state, why))
    victim = os.path.join(W, "victim-a")
    open(victim, "wb").write(b"BEFORE")
    pl = {"repo": "owner/pl", "file": "p.safetensors", "folder": "diffusion_models"}
    serve_as(pl, 100)
    os.symlink(victim, os.path.join(drive, "p.safetensors.part"))
    state, why = one(root, entry(pl, 100))
    check("a planted symlinked .part inside the linked folder: still refused, never followed",
          state == "failed" and "not a plain file" in why and open(victim, "rb").read() == b"BEFORE", (state, why))
    os.symlink(victim, os.path.join(drive, "f.safetensors"))
    fl = {"repo": "owner/fl", "file": "f.safetensors", "folder": "diffusion_models"}
    serve_as(fl, 6)
    state, why = one(root, entry(fl, 6))
    check("a symlinked final file: never counted as installed, never replaced",
          state == "failed" and os.path.islink(os.path.join(drive, "f.safetensors"))
          and open(victim, "rb").read() == b"BEFORE", (state, why))
    esc = dict(big, subdir="../../outside-a")
    state, why = one(root, entry(esc, 100))
    check("a manifest subdir with ../ through the linked folder: still refused",
          state == "failed" and "outside" in why and not os.path.exists(os.path.join(W, "outside-a")), (state, why))
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(models=None, queue=[], parts=[])


# -- REV-B: a server that ignores Range, or answers it with 416 ----------------------------------------------
def g_rev_b():
    root = os.path.realpath(models_folder("mb"))
    size = 120_000
    dest = os.path.join(root, "upscale_models", UP["file"])
    part = dest + ".part"
    rel = "upscale_models/" + UP["file"] + ".part"
    for mode, label in (("norange", "Range answered with 200 + the whole file"), ("416", "Range answered 416")):
        path = serve_as(UP, size, mode)
        with open(part, "wb") as f:
            f.write(b"\0" * 50_000)                 # a partial whose bytes are NOT the server's
        witness = os.path.join(W, "witness-" + mode)   # a second name for the OLD .part: keeps its inode alive
        os.link(part, witness)
        with srv.MODEL_DL_LOCK:
            srv.MODEL_DL.update(models=root, parts=[])
        own(root, rel)
        n = len(HF.log)
        state, why = one(root, entry(UP, size))
        reqs = [r["headers"].get("range") for r in HF.log[n:]]
        check("%s: restarts from zero and finishes byte-identical" % label,
              state == "done" and open(dest, "rb").read() == _fake_hf.content(path, size), (state, why))
        check("%s: it asked to resume first" % label, reqs[:1] == ["bytes=50000-"], reqs)
        if mode == "416":
            check("416: then fetched the whole file without Range", reqs[1:] == [None], reqs)
        check("%s: into a fresh .part: the old one was never written to, none left" % label,
              open(witness, "rb").read() == b"\0" * 50_000 and not os.path.samefile(witness, dest)
              and not os.path.exists(part) and srv.MODEL_DL["parts"] == [], srv.MODEL_DL["parts"])
        os.unlink(dest)
    path = serve_as(UP, size, "416")
    with open(part, "wb") as f:
        f.write(_fake_hf.content(path, size))
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(models=root, parts=[])
    own(root, rel)
    n = len(HF.log)
    state, why = one(root, entry(UP, size))
    check("a .part that already holds the whole file: size checked and finalized, no request",
          state == "done" and len(HF.log) == n and os.path.getsize(dest) == size and not os.path.exists(part),
          (state, why, HF.log[n:]))
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(models=None, queue=[], parts=[])


# -- Security review: FIFO, swapped files, tampered records, a slow drip, Windows flags ------------------------
def g_sec():
    root = os.path.realpath(models_folder("ms"))
    size = 1000
    path = serve_as(UP, size, "hold")
    rel = "upscale_models/" + UP["file"] + ".part"
    part = os.path.join(root, rel)

    def prep():
        with srv.MODEL_DL_LOCK:
            srv.MODEL_DL.update(models=root, queue=[], parts=[])
        with open(part, "wb") as f:
            f.write(_fake_hf.content(path, size)[:300])
        own(root, rel)
        HF.arrived.clear()
        HF.release.clear()

    def run_swapped(swap):
        res = []
        t = threading.Thread(target=lambda: res.append(one(root, entry(UP, size))), daemon=True)
        t.start()
        HF.arrived.wait(5)
        swap()                    # after the app checked the .part, before it opens it
        HF.release.set()
        t.join(6)
        return t, res

    prep()

    def to_fifo():
        os.unlink(part)
        os.mkfifo(part)
    t0 = time.time()
    t, res = run_swapped(to_fifo)
    hung = t.is_alive()
    if hung:                      # free the blocked writer so the suite can go on
        fd = os.open(part, os.O_RDONLY | os.O_NONBLOCK)
        t.join(3)
        os.close(fd)
    check("a .part swapped for a FIFO: that file fails fast, never hangs",
          not hung and res and res[0][0] == "failed" and time.time() - t0 < 5, (hung, res))
    check("... the FIFO is left alone", stat.S_ISFIFO(os.lstat(part).st_mode))
    _, c1 = srv.downloads_cancel({})
    b2, c2 = srv.downloads_discard({})
    check("... Cancel and Discard still answer, and Discard leaves the FIFO",
          c1 == 200 and c2 == 200 and stat.S_ISFIFO(os.lstat(part).st_mode), (b2, c2))
    os.unlink(part)

    for label, keep_old in (("a new inode", True), ("a possibly reused inode", False)):
        prep()
        keeper = os.path.join(W, "keep-old-part")
        if keep_old:
            os.link(part, keeper)             # the old inode stays alive: the new file cannot reuse its number
        foreign = b"the user's own unrelated file"

        def to_foreign():
            os.unlink(part)
            with open(part, "wb") as f:
                f.write(foreign)
        t, res = run_swapped(to_foreign)
        check("a .part swapped for a foreign file (%s): never deleted or overwritten" % label,
              not t.is_alive() and os.path.exists(part) and open(part, "rb").read() == foreign
              and res and res[0][0] == "failed", res)
        check("... (%s) nothing lands at the final name" % label, not os.path.exists(part[:-5]))
        b, c = srv.downloads_discard({})
        check("... (%s) Discard does not delete it either" % label,
              os.path.exists(part) and open(part, "rb").read() == foreign, b)
        if os.path.lexists(part):
            os.unlink(part)
        if keep_old:
            os.unlink(keeper)

    outside = os.path.join(W, "outside-sec")
    os.makedirs(outside)
    victim = os.path.join(outside, "victim.part")
    open(victim, "wb").write(b"V")
    os.symlink(outside, os.path.join(root, "escape"))
    vs = os.lstat(victim)
    forged = {"rel": "escape/victim.part", "dev": vs.st_dev, "ino": vs.st_ino, "size": vs.st_size,
              "mtime_ns": vs.st_mtime_ns}
    with open(srv.MODEL_DL_FILE, "w") as f:
        json.dump({"models": root, "queue": [], "parts": [forged, "escape/victim.part"]}, f)
    srv._dl_load()
    kept = [p.get("rel") if isinstance(p, dict) else p for p in srv.MODEL_DL["parts"]]
    check("a tampered parts entry through a linked folder is dropped at load", "escape/victim.part" not in kept, kept)
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(models=root, queue=[], parts=[forged, "escape/victim.part"])
    srv.downloads_discard({})
    check("... and Discard never deletes it", os.path.exists(victim))
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(models=None, queue=[], parts=[])


def g_windows():
    src = open(os.path.join(ROOT, "server.py")).read()
    bare = re.findall(r"os\.O_(?:NOFOLLOW|NONBLOCK)\b", src)
    check("no bare os.O_NOFOLLOW / os.O_NONBLOCK (Windows has neither): getattr(os, ..., 0)", not bare, bare)


def drip_server(first, per_byte, n):
    """A raw socket that sends `first` then n single bytes, one every per_byte seconds. -> port."""
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(5)

    def go():
        c, _ = ls.accept()
        c.recv(65536)
        try:
            c.sendall(first)
            for _ in range(n):
                time.sleep(per_byte)
                c.sendall(b"A")
            time.sleep(60)
        except OSError:
            pass
    threading.Thread(target=go, daemon=True).start()
    return ls.getsockname()[1]


def g_drip():
    saved = (srv._TEST_HF_DOWNLOAD, srv.MODEL_DL_BASE, getattr(srv, "MODEL_DL_STALL", None))
    try:
        port = drip_server(b"HTTP/1.1 200 OK\r\nContent-Length: 2000\r\n\r\n", 0.05, 2000)
        srv._TEST_HF_DOWNLOAD, srv.MODEL_DL_BASE = ("127.0.0.1", port), "http://127.0.0.1:%d" % port
        root = os.path.realpath(models_folder("mdrip"))
        with srv.MODEL_DL_LOCK:
            srv.MODEL_DL.update(models=root, queue=[entry(UP, 2000, state="queued")], parts=[])
            srv.MODEL_DL_RUN.update(thread=None, cancel=False)
            srv._dl_kick()
        time.sleep(1.0)
        t0 = time.time()
        srv.downloads_cancel({})
        stopped = wait(lambda: srv.MODEL_DL_RUN["thread"] is None, 10)
        took = time.time() - t0
        check("a 1-byte drip: Cancel stops it within about a second", stopped and took < 1.5, round(took, 2))
        check("... and says cancelled", srv.MODEL_DL["queue"][0]["state"] == "cancelled", srv.MODEL_DL["queue"])
        port = drip_server(b"HTTP/1.1 200 OK\r\nContent-Length: 2000\r\n\r\nAB", 99, 0)
        srv._TEST_HF_DOWNLOAD, srv.MODEL_DL_BASE = ("127.0.0.1", port), "http://127.0.0.1:%d" % port
        srv.MODEL_DL_STALL = 1.0                 # the real deadline is 30 s; the rule is the same
        with srv.MODEL_DL_LOCK:
            srv.MODEL_DL.update(models=root, parts=[])
            srv.MODEL_DL_RUN["cancel"] = False   # only the worker runs a file, and _dl_kick resets this
        t0 = time.time()
        state, why = one(root, entry(dict(UP, file="stall.pth"), 2000))
        check("a server that stops sending: the file fails at the stall deadline, saying so",
              state == "failed" and "stalled" in why and time.time() - t0 < 5, (state, why, round(time.time() - t0, 1)))
    finally:
        srv._TEST_HF_DOWNLOAD, srv.MODEL_DL_BASE = saved[0], saved[1]
        if saved[2] is not None:
            srv.MODEL_DL_STALL = saved[2]
        with srv.MODEL_DL_LOCK:
            srv.MODEL_DL.update(models=None, queue=[], parts=[])


# -- (3) size -------------------------------------------------------------------------------------------------
def g3():
    root = os.path.realpath(models_folder("m3"))
    size = 300_000
    dest = os.path.join(root, "upscale_models", "RealESRGAN_x4plus.pth")
    part = dest + ".part"

    path = serve_as(UP, size, "more")
    state, why = one(root, entry(UP, size))
    check("a server sending more than the expected size: refused", state == "failed" and "more than" in why,
          (state, why))
    check("... no file lands and its .part is removed", not os.path.exists(dest) and not os.path.exists(part))

    for mode, label in (("badlen", "a Content-Length that is not the expected size"),
                        ("nolen", "no Content-Length")):
        serve_as(UP, size, mode)
        state, why = one(root, entry(UP, size))
        check("%s: refused" % label, state == "failed" and "not the %d expected" % size in why, (state, why))
        check("%s: no .part was ever created, no file lands" % label,
              not os.path.exists(part) and not os.path.exists(dest))

    serve_as(UP, size, "ok")
    state, why = one(root, entry(UP, size))
    check("the exact size: renamed into place", state == "done" and os.path.getsize(dest) == size, (state, why))
    check("... byte for byte, and the .part is gone",
          open(dest, "rb").read() == _fake_hf.content(path, size) and not os.path.exists(part))
    check("... and no longer recorded as ours",
          "upscale_models/RealESRGAN_x4plus.pth.part" not in srv.MODEL_DL["parts"])

    before = len(HF.log)
    state, why = one(root, entry(UP, size))
    check("already there at its exact size: Installed, with no request",
          state == "done" and len(HF.log) == before, (state, why, len(HF.log) - before))
    check("... _model_on_disk says so only at the exact size",
          srv._model_on_disk(root, dict(UP, size=size)) and not srv._model_on_disk(root, dict(UP, size=size + 1)))

    with open(dest, "wb") as f:
        f.write(b"the user's own file")
    state, why = one(root, entry(UP, size))
    check("a different file of that name: left alone, with a sentence",
          state == "failed" and "different file" in why and open(dest, "rb").read() == b"the user's own file",
          (state, why))


# -- (4, in-process) .part rules ------------------------------------------------------------------------------
def g4_parts():
    root = os.path.realpath(models_folder("m4"))
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(queue=[], parts=[])
    size = 200_000
    path = serve_as(UP, size, "ok")
    dest = os.path.join(root, "upscale_models", "RealESRGAN_x4plus.pth")
    part = dest + ".part"
    victim = os.path.join(W, "victim")
    open(victim, "wb").write(b"BEFORE")
    os.symlink(victim, part)
    state, why = one(root, entry(UP, size))
    check("a .part that is a symlink: refused, never followed",
          state == "failed" and "not a plain file" in why and open(victim, "rb").read() == b"BEFORE", (state, why))
    os.unlink(part)
    open(part, "wb").write(b"someone else's")
    state, why = one(root, entry(UP, size))
    check("a .part this app did not create: refused and left alone",
          state == "failed" and "not left by Black Wire Forge" in why
          and open(part, "rb").read() == b"someone else's", (state, why))
    os.unlink(part)
    data = _fake_hf.content(path, size)
    open(part, "wb").write(data[:77_777])
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(models=root, parts=[])
    own(root, "upscale_models/RealESRGAN_x4plus.pth.part")
    n = len(HF.log)
    state, why = one(root, entry(UP, size))
    check("our own .part resumes with Range from its size",
          len(HF.log) > n and HF.log[n]["headers"].get("range") == "bytes=77777-", HF.log[n:])
    check("... and finishes byte-identical", state == "done" and open(dest, "rb").read() == data, (state, why))


# -- (5) gated, token, redirects ------------------------------------------------------------------------------
def g5():
    root = os.path.realpath(models_folder("m5"))
    gated = {"repo": "owner/gated", "file": "model.safetensors", "folder": "checkpoints"}
    serve_as(gated, 1000, "403")
    state, why = one(root, entry(gated, 1000))
    check("403: skipped with a sentence naming the repo page and 'needs a Hugging Face login'",
          state == "skipped" and "https://huggingface.co/owner/gated" in why and "needs a Hugging Face login" in why,
          (state, why))
    check("... no .part left", not os.path.exists(os.path.join(root, "checkpoints", "model.safetensors.part")))

    token = "hf_w3faketoken0123456789"
    os.environ["HF_TOKEN"] = token
    try:
        viacdn = {"repo": "owner/viacdn", "file": "vae/v.safetensors", "folder": "vae"}
        path = serve_as(viacdn, 5000, "redirect")
        CDN.sizes[path] = 5000
        h0, c0 = len(HF.log), len(CDN.log)
        state, why = one(root, entry(viacdn, 5000))
        check("through a redirect to the pinned CDN: done", state == "done", (state, why))
        check("HF_TOKEN is sent to the Hugging Face host",
              len(HF.log) > h0 and HF.log[h0]["headers"].get("authorization") == "Bearer " + token, HF.log[h0:])
        check("... and never to the CDN it redirects to",
              len(CDN.log) > c0 and all("authorization" not in r["headers"] for r in CDN.log[c0:]), CDN.log[c0:])
        away = {"repo": "owner/away", "file": "x.safetensors", "folder": "vae"}
        HF.routes[_fake_hf.file_path(away["repo"], away["file"])] = "redirect"
        HF.redirect_to = ELSEWHERE.origin
        state, why = one(root, entry(away, 5000))
        check("a redirect off the pinned hosts: refused, nothing fetched there",
              state == "failed" and "untrusted" in why and ELSEWHERE.log == [], (state, why))
        HF.redirect_to = CDN.origin
        with srv.MODEL_DL_LOCK:
            srv.MODEL_DL["queue"] = [dict(entry(gated, 1000), state="skipped", error=why)]
            srv._dl_save()
        blob = json.dumps(srv.downloads_status()[0]) + open(srv.MODEL_DL_FILE).read()
        check("the token is never echoed (status, downloads.json)", token not in blob)
    finally:
        os.environ.pop("HF_TOKEN", None)
        with srv.MODEL_DL_LOCK:
            srv.MODEL_DL["queue"] = []


# -- (6) pre-flight -------------------------------------------------------------------------------------------
def g6():
    root = os.path.realpath(models_folder("m6"))
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(models=root, queue=[], parts=[])
    rooms = {r["id"]: r for r in srv.setup_rooms({})[0]["rooms"]}
    cleanup = rooms["cleanup"]
    need = cleanup["download"]["bytes"]
    check("a room's download plan: its files once each, the same bytes W2's needs count",
          {k: cleanup["download"][k] for k in ("files", "bytes")} == {"files": 2, "bytes": BG["size"] + UP["size"]}
          and all(r["download"]["bytes"] == r["needs"]["bytes"] for r in rooms.values()), cleanup["download"])
    run_by_us = set(srv._manifest_sources())
    planned = [(s["repo"], s["file"]) for r in rooms.values() for s in srv._room_plan(r)]
    check("'not run by us' files are never planned", planned and all(k in run_by_us for k in planned))
    real_du = srv.shutil.disk_usage
    Usage = type(real_du(root))
    try:
        srv.shutil.disk_usage = lambda p: Usage(10 ** 12, 10 ** 12 - 10 ** 9, 10 ** 9)
        body, code = srv.downloads_start({"room": "cleanup", "bytes": need})
        check("free space short: refused", code == 400 and not body["ok"], (code, body))
        check("... in a sentence naming both numbers", "0.5 GB" in body["error"] and "1.0 GB" in body["error"],
              body["error"])
        check("... and nothing is queued", srv.MODEL_DL["queue"] == [])
        srv.shutil.disk_usage = lambda p: Usage(10 ** 13, 0, 10 ** 13)
        body, code = srv.downloads_start({"room": "cleanup", "bytes": need - 1})
        check("more bytes than the page showed: refused, nothing queued",
              code == 409 and srv.MODEL_DL["queue"] == [], (code, body))
        body, code = srv.downloads_start({"room": "nope", "bytes": 1})
        check("an unknown room: refused", code == 400, (code, body))
    finally:
        srv.shutil.disk_usage = real_du
    with open(os.path.join(root, "upscale_models", UP["file"]), "wb") as f:
        f.truncate(UP["size"])
    cleanup = {r["id"]: r for r in srv.setup_rooms({})[0]["rooms"]}["cleanup"]
    check("a file already in the folder at its size: the room shows it Installed and plans one file",
          {k: cleanup["download"][k] for k in ("files", "bytes")} == {"files": 1, "bytes": BG["size"]}
          and [r["installed"] for r in cleanup["roles"]] == [False, True], (cleanup["download"], cleanup["roles"]))
    with srv.MODEL_DL_LOCK:
        srv.MODEL_DL.update(models=None, queue=[], parts=[])


# -- (4, HTTP) kill mid-file, restart, resume; Cancel; Discard; step-1 sentences over HTTP ------------------
def status():
    body = fx.http("GET", "/api/downloads")[1]
    return body if isinstance(body, dict) else {}


def wait(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.2)
    return None


def g4_http():
    if not fx.port_free(fx.SETUP_PORT):
        check("port %d is free for the Setup server" % fx.SETUP_PORT, False, "something else is listening on it")
        return
    m = models_folder("m4http")
    with open(os.path.join(m, "background_removal", BG["file"]), "wb") as f:
        f.truncate(BG["size"])                     # sparse: its exact size, no disk used
    link = os.path.join(W, "m4http-link")
    os.symlink(m, link)
    path = serve_as(UP, UP["size"], "slow")
    proc, cfg, logp = fx.boot("dl")
    check("Setup server up", fx.wait_health(proc, want_setup=True) is not None)
    code, body = fx.http("POST", "/api/setup/models-folder", {"path": "ComfyUI/models"})
    check("models-folder over HTTP: a relative path -> 400 with its sentence",
          code == 400 and "whole path" in str(body), (code, body))
    code, body = fx.http("POST", "/api/setup/models-folder", {"path": os.path.join(W, "nope")})
    check("models-folder over HTTP: a missing path -> 400 with its sentence",
          code == 400 and "Nothing is at that path" in str(body), (code, body))
    code, body = fx.http("POST", "/api/setup/models-folder", {"path": link})
    check("models-folder over HTTP: a symlinked folder -> 200 and its real path",
          code == 200 and isinstance(body, dict) and body.get("path") == os.path.realpath(m), (code, body))
    code, rooms = fx.http("GET", "/api/setup/rooms")
    cleanup = next((r for r in rooms.get("rooms", []) if r["id"] == "cleanup"), {}) if isinstance(rooms, dict) else {}
    check("the Cleanup card: its background-removal file Installed, one file left to download",
          {k: (cleanup.get("download") or {}).get(k) for k in ("files", "bytes")} == {"files": 1, "bytes": UP["size"]},
          cleanup.get("download"))
    code, body = fx.http("POST", "/api/downloads/start", {"room": "cleanup", "bytes": UP["size"]})
    check("start -> 1 file queued", code == 200 and isinstance(body, dict) and body.get("queued") == 1, (code, body))
    part = os.path.join(m, "upscale_models", UP["file"] + ".part")
    dest = os.path.join(m, "upscale_models", UP["file"])
    got = wait(lambda: os.path.exists(part) and os.path.getsize(part) > 8 << 20, 30)
    st = status()
    check("while running: running, file 1 of 1, a percentage",
          st.get("running") is True and st.get("position") == 1 and st.get("total") == 1
          and isinstance(st.get("percent"), int), st)
    check("mid-file: the .part is growing", got)
    proc.send_signal(signal.SIGKILL)
    proc.wait(10)
    wait(lambda: fx.port_free(fx.SETUP_PORT), 10)
    cut = os.path.getsize(part) if os.path.exists(part) else -1
    check("killed mid-file: the .part stays, short of the size", 0 < cut < UP["size"], cut)
    try:
        saved = json.load(open(os.path.join(fx.SCRATCH, "dl", "data", "downloads.json")))
    except (OSError, ValueError):
        saved = {"queue": []}
    check("downloads.json holds the file, its dest, expected size and state",
          [(e["file"], e["dest"], e["expected"], e["state"]) for e in saved["queue"]]
          == [(UP["file"], "upscale_models/" + UP["file"], UP["size"], "running")], saved["queue"])
    n = len(HF.log)
    HF.slow_sleep = 0.0
    proc, cfg, logp = fx.boot("dl")
    check("restarted", fx.wait_health(proc, want_setup=True) is not None)
    done = wait(lambda: (status().get("files") or [{}])[0].get("state") == "done", 60)
    check("after the restart the queue resumes and finishes", done, status())
    ranges = [r["headers"].get("range") for r in HF.log[n:] if r["path"] == path]
    check("... with a Range request from where it stopped",
          ranges and ranges[0] and ranges[0].startswith("bytes=") and int(ranges[0][6:-1]) >= cut, ranges)
    check("... byte-identical to the server's file, the .part gone",
          os.path.exists(dest) and sha(dest) == hashlib.sha256(_fake_hf.content(path, UP["size"])).hexdigest()
          and not os.path.exists(part))
    st = status()
    check("status: 1 of 1 done, 100%, no partial files left", st.get("done") == 1 and st.get("percent") == 100
          and st.get("parts") == 0 and st.get("running") is False, st)
    # Cancel keeps the .part; Discard removes only ours.
    os.unlink(dest)
    HF.slow_sleep = 0.03
    code, body = fx.http("POST", "/api/downloads/start", {"room": "cleanup", "bytes": UP["size"]})
    check("started again", code == 200 and isinstance(body, dict) and body.get("queued") == 1, (code, body))
    wait(lambda: os.path.exists(part) and os.path.getsize(part) > 2 << 20, 30)
    code, body = fx.http("POST", "/api/downloads/cancel", {})
    check("cancel -> 200", code == 200, (code, body))
    stopped = wait(lambda: status().get("running") is False, 20)
    st = status()
    check("Cancel stops the file; it says cancelled; the .part stays for later",
          stopped and st["files"][0]["state"] == "cancelled" and os.path.exists(part) and st.get("parts") == 1, st)
    foreign = os.path.join(m, "vae", "someone.safetensors.part")
    open(foreign, "wb").write(b"not ours")
    code, body = fx.http("POST", "/api/downloads/discard", {})
    check("Discard removes our own .part only",
          code == 200 and isinstance(body, dict) and body.get("removed") == 1 and not os.path.exists(part)
          and os.path.exists(foreign), (code, body))
    fx.stop(proc)


# -- (8) the downloads routes beyond localhost ----------------------------------------------------------------
def _call(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception as e:
        return None, {"exception": repr(e)}


def boot_configured(name, bind):
    port = fx.free_port()
    cfg = {"port": port, "bind": bind, "lanes": [{"id": "x", "name": "X", "host": "127.0.0.1", "port": 1}]}
    proc, _, logp = fx.boot(name, json.dumps(cfg))
    base = "http://127.0.0.1:%d" % port
    wait(lambda: proc.poll() is None and _call(base + "/api/health")[0] == 200, 20)
    return proc, base


def g8():
    proc, base = boot_configured("net", "0.0.0.0")
    code, body = _call(base + "/api/downloads")
    check("bound beyond localhost: GET /api/downloads -> 403 with a sentence",
          code == 403 and "this computer alone" in body.get("error", ""), (code, body))
    code, body = _call(base + "/api/downloads/cancel", {})
    check("bound beyond localhost: POST /api/downloads/cancel -> 403", code == 403, (code, body))
    fx.stop(proc)
    proc, base = boot_configured("local", "127.0.0.1")
    code, body = _call(base + "/api/downloads")
    check("configured, this computer only: GET /api/downloads answers", code == 200 and body.get("ok") is True,
          (code, body))
    code, body = _call(base + "/api/downloads/cancel", {})
    check("configured, this computer only: Cancel answers", code == 200, (code, body))
    for p in ("/api/downloads/start", "/api/downloads/discard", "/api/setup/models-folder"):
        code, body = _call(base + p, {"room": "cleanup"})
        check("configured: %s -> 404 (Setup only)" % p, code == 404, (code, body))
    fx.stop(proc)


try:
    gate("(1) the models folder", g1)
    gate("(2) containment", g2)
    gate("(3) size", g3)
    gate("REV-A: a models subfolder linked to another drive", g_rev_a)
    gate("REV-B: Range answered with 200 or 416", g_rev_b)
    gate("(4) .part rules and Range, in-process", g4_parts)
    gate("(5) gated repos, HF_TOKEN, redirects", g5)
    gate("(6) pre-flight", g6)
    gate("(4) kill mid-file, restart, resume; Cancel; Discard (HTTP)", g4_http)
    gate("(8) downloads routes on a server bound beyond localhost", g8)
    gate("Security review: FIFO, swapped files, tampered records", g_sec)
    gate("Security review: Windows open flags", g_windows)
    gate("Security review: a slow drip, a stall", g_drip)
finally:
    shutil.rmtree(W, ignore_errors=True)
    fx.finish()
