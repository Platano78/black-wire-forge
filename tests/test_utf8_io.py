"""Acceptance gate for explicit UTF-8 text I/O. Text-mode `open()` must name
its encoding, `subprocess` calls that ask for text must say how to decode it,
and a config/rooms file full of non-ASCII must survive an ASCII-default
interpreter (the stricter stand-in for Windows cp1252).

Three parts:
  (a) an AST lint -- no import, so it runs even if a module is broken;
  (b) a CHILD python started with LC_ALL=C / PYTHONUTF8=0 that imports server,
      prints a non-ASCII lane name as UTF-8 bytes and calls server.log();
  (c) the same child checks rooms.json's em dash (U+2014) loads intact.

Run: python3 tests/test_utf8_io.py
"""
import ast
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond else ""))
    if not cond: FAILED.append(name)

# --------------------------------------------------------------------------
# (a) AST lint: every text-mode open() and every text=True subprocess call
# --------------------------------------------------------------------------
LINT_FILES = ["server.py", "guides.py", "forge_run.py", "forge_master.py",
              "runner.py", "sanitize.py"]
LINT_FILES += sorted(glob.glob(os.path.join(ROOT, "engines", "*.py")))
LINT_FILES += sorted(glob.glob(os.path.join(ROOT, "engines", "producer_tools", "*.py")))

def lint_offenders(path):
    """-> [file:line reason] for every unhardened open()/subprocess() call."""
    src = open(path, encoding="utf-8").read()
    bad = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            kws = {k.arg for k in node.keywords}
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = None
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    mode = node.args[1].value
                if isinstance(mode, str) and "b" in mode:
                    continue                       # binary: no encoding wanted
                if "encoding" not in kws:
                    bad.append("%s:%d open(%s) has no encoding=" % (
                        path, node.lineno, "r" if not mode else repr(mode)))
            if (isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in ("run", "Popen", "check_output")
                    and kws & {"text", "universal_newlines"}
                    and not (kws & {"encoding", "errors"})):
                bad.append("%s:%d subprocess.%s(text=True) has no encoding=/errors=" % (
                    path, node.lineno, node.func.attr))
    return bad

OFFENDERS = []
for _f in LINT_FILES:
    OFFENDERS += lint_offenders(_f)
check("ast lint: %d files, every text open()/subprocess text=True names its codec"
      % len(LINT_FILES), not OFFENDERS, "\n    ".join(OFFENDERS))

# --------------------------------------------------------------------------
# (b)+(c) a child interpreter whose default text encoding is ASCII
# --------------------------------------------------------------------------
SCRATCH = tempfile.mkdtemp(prefix="bwf_utf8_io_")
DATA = os.path.join(SCRATCH, "data")
LANE_NAME = "Bo\xeete \u201cde\u201d rendu \u65e5\u672c"
CONFIG = os.path.join(SCRATCH, "config.json")
with open(CONFIG, "w", encoding="utf-8") as _f:
    json.dump({"port": 3998, "bind": "127.0.0.1", "title": "utf8-io",
               "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
               "lanes": [{"id": "w", "name": LANE_NAME, "host": "127.0.0.1",
                          "port": 1, "caps": ["image"]}]}, _f, ensure_ascii=False)

CHILD = r'''
import json, sys
import server
name = server.LANES[0]["name"]
out = sys.stdout.buffer
out.write(b"NAME=" + name.encode("utf-8") + b"\n")
server.log("lane \u65e5\u672c")
out.write(b"LOGGED\n")
out.flush()
import engines
src = json.load(open("rooms.json", encoding="utf-8"))
want = [r["empty"] for r in src if "\u2014" in r.get("empty", "")]
got = [r.get("empty") for r in engines.rooms() if "\u2014" in (r.get("empty") or "")]
out.write(b"EMDASH=%d\n" % (1 if want == got and want else 0))
out.flush()
'''

ENV = dict(os.environ)
ENV.update({"GENCENTER_CONFIG": CONFIG, "GENCENTER_DATA": DATA,
            "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"})
ENV.pop("PYTHONWARNINGS", None)

PROBE = subprocess.run([sys.executable, "-c", "import sys; print(sys.stdout.encoding)"],
                       env=ENV, capture_output=True, text=True)
check("stand-in: the child interpreter's default text encoding is not UTF-8",
      PROBE.stdout.strip() not in ("utf-8", "UTF-8"), PROBE.stdout.strip() + PROBE.stderr)

CHILD_RUN = subprocess.run([sys.executable, "-c", CHILD], cwd=ROOT, env=ENV, capture_output=True)
TAIL = CHILD_RUN.stdout.decode("utf-8", "replace")
if CHILD_RUN.returncode != 0:
    print("    child rc=%s stderr=%s" % (CHILD_RUN.returncode,
                                          CHILD_RUN.stderr.decode("utf-8", "replace")[-800:]))
check("child: imports server with an ASCII default encoding and exits 0",
      CHILD_RUN.returncode == 0, CHILD_RUN.returncode)
check("child: a non-ASCII lane name from config.json round-trips exactly",
      ("NAME=" + LANE_NAME) in TAIL, repr(TAIL))
check("child: server.log() with non-ASCII does not raise on the console",
      "LOGGED" in TAIL, repr(TAIL))
check("child: rooms.json's em dash (U+2014) loads intact",
      "EMDASH=1" in TAIL, repr(TAIL))

shutil.rmtree(SCRATCH, ignore_errors=True)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)