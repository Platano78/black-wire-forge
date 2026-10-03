"""Acceptance gate for the Windows-safe process lanes: runner.py must not
reach for preexec_fn / os.nice / os.killpg when it is running on Windows,
must ask for a new process group instead, and must print the .venv python
path in the shape each platform uses. server.py's ffmpeg value quoting must
escape a Windows drive letter without touching a POSIX path.

Runs on ANY platform -- the Windows behaviour is exercised by setting
runner.IS_WINDOWS, never by being on Windows.

Run: python3 tests/test_runner_windows_safe.py
"""
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile

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
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p

SCRATCH = tempfile.mkdtemp(prefix="bwf_win_safe_")
CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "win-safe",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "t", "name": "T", "host": "127.0.0.1", "port": 1, "caps": ["image"]}]},
          open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")

import runner

JOB = tempfile.mkdtemp(prefix="win-safe-job-")
STEPS = runner.resolve_plan({"steps": [{"argv": ["{bin:py}", "-c", "pass"], "timeout_s": 30}],
                             "outputs": [], "progress": ""},
                            {"py": sys.executable}, JOB, {}, "/pack")

class _Recorder(object):
    """Stands in for Popen: remembers the kwargs, then fails the way a
    missing program would, so the step never actually starts."""
    def __init__(self, sink):
        self.sink = sink
    def __call__(self, *args, **kwargs):
        self.sink.append(kwargs)
        raise FileNotFoundError(2, "No such file or directory")

def captured_kwargs(is_windows):
    sink = []
    real_flag, real_popen = runner.IS_WINDOWS, runner.subprocess.Popen
    runner.IS_WINDOWS = is_windows
    runner.subprocess.Popen = _Recorder(sink)
    try:
        ok, tail, err = runner.run_steps(STEPS, JOB)
    finally:
        runner.IS_WINDOWS = real_flag
        runner.subprocess.Popen = real_popen
    return sink, ok, err

# 1. Windows: a new process group, and none of the POSIX-only knobs
sink, ok, err = captured_kwargs(True)
kw = sink[0] if sink else {}
check("windows: Popen was called once", len(sink) == 1, repr(sink))
check("windows: asks for CREATE_NEW_PROCESS_GROUP",
      "creationflags" in kw and kw.get("creationflags") == runner.CREATE_NEW_PROCESS_GROUP,
      repr(sorted(kw)))
check("windows: no preexec_fn", "preexec_fn" not in kw, repr(sorted(kw)))
check("windows: no start_new_session", "start_new_session" not in kw, repr(sorted(kw)))
check("windows: the refusal is reported as a step that could not start",
      (not ok) and err is not None and err.startswith("step 1 could not start"), repr(err))

# 2. POSIX: unchanged -- its own session and a nicer priority
sink, ok, err = captured_kwargs(False)
kw = sink[0] if sink else {}
check("posix: start_new_session=True", kw.get("start_new_session") is True, repr(sorted(kw)))
check("posix: a preexec_fn is passed", callable(kw.get("preexec_fn")), repr(sorted(kw)))
check("posix: no creationflags", "creationflags" not in kw, repr(sorted(kw)))

# 3. the printed install hint follows the platform
real_flag = runner.IS_WINDOWS
try:
    runner.IS_WINDOWS = False
    check("hint: POSIX prints .venv/bin/python",
          runner.venv_python_hint() == ".venv/bin/python", repr(runner.venv_python_hint()))
    runner.IS_WINDOWS = True
    check("hint: Windows prints .venv\\Scripts\\python",
          runner.venv_python_hint() == ".venv\\Scripts\\python", repr(runner.venv_python_hint()))
finally:
    runner.IS_WINDOWS = real_flag

# 4. ffmpeg filter quoting: a drive letter is escaped, a POSIX path is not
spec = importlib.util.spec_from_file_location("srv_win_safe", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
check("ff_quote: a Windows drive letter's colon is escaped",
      srv._ff_quote("C:/Windows/Fonts/arial.ttf") == "'C\\:/Windows/Fonts/arial.ttf'",
      repr(srv._ff_quote("C:/Windows/Fonts/arial.ttf")))
check("ff_quote: a POSIX path is quoted exactly as before",
      srv._ff_quote(DEJAVU) == "'" + DEJAVU + "'", repr(srv._ff_quote(DEJAVU)))
check("quote: a POSIX path with a single-letter dir and a colon is untouched",
      srv._ff_quote("/tmp/x:y/f.ttf") == "'/tmp/x:y/f.ttf'", repr(srv._ff_quote("/tmp/x:y/f.ttf")))
check("quote: a Windows backslash path gets forward slashes and an escaped drive colon",
      srv._ff_quote("C:\\Users\\A\\Temp\\t.txt") == "'C\\:/Users/A/Temp/t.txt'",
      repr(srv._ff_quote("C:\\Users\\A\\Temp\\t.txt")))
check("ff_quote: a quote is still escaped",
      srv._ff_quote("it's") == "'it'\\''s'", repr(srv._ff_quote("it's")))
check("fonts: the Windows candidates are in the default list",
      "C:/Windows/Fonts/arial.ttf" in srv._DEFAULT_CUT_FONTS
      and "C:/Windows/Fonts/segoeui.ttf" in srv._DEFAULT_CUT_FONTS, repr(srv._DEFAULT_CUT_FONTS))
check("fonts: the Linux candidates keep their order",
      srv._DEFAULT_CUT_FONTS[:4] == (
          DEJAVU,
          "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
          "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
          "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
      repr(srv._DEFAULT_CUT_FONTS[:4]))

shutil.rmtree(SCRATCH, ignore_errors=True)
shutil.rmtree(JOB, ignore_errors=True)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)