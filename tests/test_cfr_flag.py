"""Acceptance gate for the constant-frame-rate flag: newer ffmpeg builds
removed `-vsync` (every cut then ends "ffmpeg could not build this cut."),
older ones (4.4, still on Ubuntu 22.04) have no `-fps_mode`. server.py must
ask the installed ffmpeg which one it understands, decide once, and use only
that spelling in the cut's encode -- so the `-vsync` literal appears exactly
once in the module, inside _cfr_args.

ISOLATION: server.py is imported in-process against a scratch
GENCENTER_CONFIG/GENCENTER_DATA (same way tests/test_runner_windows_safe.py
does it); nothing outside the tempfile is written.

Run: python3 tests/test_cfr_flag.py
"""
import ast
import importlib.util
import json
import os
import shutil
import socket
import subprocess as real_subprocess
import sys
import tempfile
import types

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond else ""))
    if not cond: FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

SCRATCH = tempfile.mkdtemp(prefix="bwf_cfr_flag_")
CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "cfr",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "t", "name": "T", "host": "127.0.0.1", "port": 1, "caps": ["image"]}]},
          open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")

spec = importlib.util.spec_from_file_location("srv_cfr_flag", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

REAL_RUN = srv.subprocess.run
FAKE = {"calls": 0}

def fake_run(rc=0):
    def run(*a, **kw):
        FAKE["calls"] += 1
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="")
    return run

def fake_raise(exc):
    def run(*a, **kw):
        FAKE["calls"] += 1
        raise exc
    return run

try:
    # 1. an ffmpeg that accepts -fps_mode -> -fps_mode cfr
    FAKE["calls"] = 0
    srv.subprocess.run = fake_run(0)
    srv._CFR_ARGS = None
    check("an ffmpeg that accepts -fps_mode gets [-fps_mode, cfr]",
          srv._cfr_args() == ["-fps_mode", "cfr"], srv._cfr_args())

    # 2. an ffmpeg that rejects it (the old 4.4 shape) -> -vsync cfr
    FAKE["calls"] = 0
    srv.subprocess.run = fake_run(1)
    srv._CFR_ARGS = None
    check("an ffmpeg that rejects -fps_mode gets [-vsync, cfr]",
          srv._cfr_args() == ["-vsync", "cfr"], srv._cfr_args())

    # 3. an absent/unrunnable ffmpeg keeps the old behaviour
    FAKE["calls"] = 0
    srv.subprocess.run = fake_raise(FileNotFoundError("ffmpeg"))
    srv._CFR_ARGS = None
    check("a missing ffmpeg keeps the old [-vsync, cfr]",
          srv._cfr_args() == ["-vsync", "cfr"], srv._cfr_args())

    # 4. the probe is memoised: a second call runs nothing
    FAKE["calls"] = 0
    srv.subprocess.run = fake_run(0)
    srv._CFR_ARGS = None
    first = srv._cfr_args()
    after_first = FAKE["calls"]
    second = srv._cfr_args()
    check("the probe runs once and is then memoised",
          after_first == 1 and FAKE["calls"] == 1 and second == first,
          (after_first, FAKE["calls"], second))

    # 5. the returned list is a copy -- mutating it cannot corrupt the cache
    got = srv._cfr_args()
    got.append("-meltdown")
    check("the returned list is a copy of the cache",
          srv._cfr_args() == first, srv._cfr_args())
finally:
    srv.subprocess.run = REAL_RUN

# 6. static: "-vsync" occurs exactly once in server.py, inside _cfr_args
with open(os.path.join(ROOT, "server.py"), "r", encoding="utf-8") as fh:
    tree = ast.parse(fh.read())
hits = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "-vsync"]
def enclosing(node):
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(fn):
                if sub is node:
                    return fn.name
    return None
check("server.py contains the -vsync literal exactly once",
      len(hits) == 1, [getattr(n, "lineno", None) for n in hits])
check("that -vsync literal is inside _cfr_args",
      len(hits) == 1 and enclosing(hits[0]) == "_cfr_args",
      enclosing(hits[0]) if hits else None)

# 7. live: the chosen flag really works on this machine's ffmpeg
if shutil.which(srv.FFMPEG_BIN):
    srv._CFR_ARGS = None
    flags = srv._cfr_args()
    r = real_subprocess.run([srv.FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
                             "-f", "lavfi", "-i", "testsrc=d=0.5:s=64x64",
                             ] + flags + ["-r", "24", "-f", "null", "-"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120)
    check("this machine's ffmpeg really accepts %s" % (flags,),
          r.returncode == 0, (r.stderr or "")[-400:])
else:
    print("  SKIP  no ffmpeg on this machine")

shutil.rmtree(SCRATCH, ignore_errors=True)
print()
print("ALL PASS" if not FAILED else "FAILED: %d -- %s" % (len(FAILED), FAILED))
sys.exit(1 if FAILED else 0)
