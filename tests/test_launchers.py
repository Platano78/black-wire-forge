"""Smoke tests for the launcher files and --open flag.

(a) six launcher/example files exist;
(b) bash -n start.sh start.command clean;
(c) .sh/.command are executable;
(d) start.bat contains CRLF and @echo off and the exact no-Python sentence;
(e) .gitignore lists start-user.sh, start-user.bat, start.log;
(f) --open really opens the browser (stub BROWSER writes a marker);
(g) without --open the marker never appears;
(h) BWF_OPENED=1 prevents the second open.

Run: python3 tests/test_launchers.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAILED.append(name)


FAILED = []

# Check we can run bash (skip on Windows)
try:
    subprocess.run(["bash", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
except FileNotFoundError:
    print("SKIP: bash not available on this platform")
    sys.exit(0)

# ---- File existence ----
print("File existence")
for f in ["start.sh", "start.command", "start.bat",
          "start-user.example.sh", "start-user.example.bat"]:
    check("launcher file exists: %s" % f,
          os.path.isfile(os.path.join(ROOT, f)))

# ---- Syntax checks ----
print("Syntax checks")
for f in ["start.sh", "start.command"]:
    # the path is relative and cwd is the repo root on purpose: Git Bash eats the
    # backslashes of an absolute C:\... path passed on the command line
    r = subprocess.run(["bash", "-n", f], cwd=ROOT, capture_output=True)
    check("bash -n %s clean" % f, r.returncode == 0, r.stderr.decode("utf-8", "replace")[:200])

# ---- Executable bits ----
print("Executable bits")
for f in ["start.sh", "start.command"]:
    path = os.path.join(ROOT, f)
    check("%s is executable" % f, os.access(path, os.X_OK))

# ---- start.bat checks ----
print("start.bat content")
bat_path = os.path.join(ROOT, "start.bat")
with open(bat_path, "rb") as bf:
    bat_raw = bf.read()
check("start.bat contains CRLF", b"\r\n" in bat_raw, "no CRLF found")
with open(bat_path, "r", encoding="utf-8") as bf:
    bat_text = bf.read()
check("start.bat starts with @echo off", bat_text.strip().startswith("@echo off"),
      "first line: " + repr(bat_text.strip().splitlines()[0]) if bat_text.strip() else "(empty)")
check("start.bat has the no-Python sentence",
      "Python 3.8 or newer is needed. Install it from python.org (on Windows, tick 'Add python.exe to PATH'), then run this again."
      in bat_text.replace("^(", "(").replace("^)", ")"))   # batch needs ^( ^) inside echo; the printed text is the same

# ---- .gitignore ----
print(".gitignore entries")
gitignore_path = os.path.join(ROOT, ".gitignore")
with open(gitignore_path, "r") as gf:
    gi_text = gf.read()
for name in ["start-user.sh", "start-user.bat", "start.log"]:
    check(".gitignore lists %s" % name, name in gi_text)

# ---- --open flag tests ----
print("--open flag tests")


def port_free(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# Create a stub BROWSER script that writes its URL argument to a marker file
BROWSER_MARKER = tempfile.mktemp(prefix="bwf_browser_")
SCRIPT_DIR = tempfile.mkdtemp(prefix="bwf_stub_")

stub_browser = os.path.join(SCRIPT_DIR, "stub_browser.py")
with open(stub_browser, "w") as sf:
    sf.write('''#!/usr/bin/env python3
import sys, os
with open(os.environ.get("BWF_MARKER_FILE", "/dev/null"), "a") as f:
    for url in sys.argv[1:]:
        f.write(url + "\\n")
''')
os.chmod(stub_browser, 0o755)
# BROWSER is a command line for webbrowser on every platform; on Windows a .py file is not directly executable.
BROWSER_CMD = stub_browser if os.name != "nt" else '"%s" "%s" %%s' % (sys.executable, stub_browser)


def run_server(open_flag=False, env_extra=None, timeout=6):
    """Start server.py in setup mode, return the process."""
    d = tempfile.mkdtemp(prefix="bwf_test_")
    cfg = os.path.join(d, "config.json")  # does not exist -> Setup mode
    logp = os.path.join(d, "server.log")
    env = dict(os.environ, GENCENTER_CONFIG=cfg, GENCENTER_DATA=os.path.join(d, "data"))
    if env_extra:
        env.update(env_extra)
    cmd = [sys.executable, os.path.join(ROOT, "server.py")]
    if open_flag:
        cmd.append("--open")
    proc = subprocess.Popen(cmd,
                            cwd=ROOT, env=env,
                            stdout=open(logp, "w"), stderr=subprocess.STDOUT)
    return proc, cfg, logp, d


def wait_for_marker(timeout=5):
    """Wait for marker file to appear with a URL. Returns the URL or None."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(BROWSER_MARKER):
            with open(BROWSER_MARKER, "r") as mf:
                content = mf.read().strip()
            if content:
                return content
        time.sleep(0.2)
    return None


def stop_proc(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


# Test 1: --open should trigger browser open
PROC1 = None
PROC2 = None
try:
    if not port_free(3998):
        check("port 3998 is free for the --open test", False, "port 3998 is in use")
    else:
        open_url = None
        proc, _, _, _ = run_server(open_flag=True, env_extra={
            "BROWSER": BROWSER_CMD,
            "BWF_MARKER_FILE": BROWSER_MARKER,
        })
        PROC1 = proc
        # Wait for server to be ready (up to 10s)
        end = time.monotonic() + 10
        ready = False
        while time.monotonic() < end:
            if proc.poll() is not None:
                break
            try:
                r = urllib.request.urlopen("http://127.0.0.1:3998/api/health", timeout=1)
                h = json.loads(r.read())
                if h.get("setup"):
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not ready:
            check("--open: server started in setup mode", False, "server did not become ready")
        else:
            # Give it a moment then check for marker
            marker_url = wait_for_marker(timeout=6)
            check("--open: browser stub was called with the server URL",
                  marker_url is not None and "http://127.0.0.1:3998/" in marker_url,
                  "marker URL: %r" % marker_url)
            if marker_url:
                open_url = marker_url

        stop_proc(proc)
        PROC1 = None
finally:
    if PROC1 is not None:
        stop_proc(PROC1)
        PROC1 = None

# Clean up marker for next test
if os.path.exists(BROWSER_MARKER):
    os.unlink(BROWSER_MARKER)

# Test 2: without --open, marker should never appear
try:
    proc, _, _, _ = run_server(open_flag=False, env_extra={
        "BROWSER": BROWSER_CMD,
        "BWF_MARKER_FILE": BROWSER_MARKER,
    })
    PROC2 = proc
    end = time.monotonic() + 10
    ready = False
    while time.monotonic() < end:
        if proc.poll() is not None:
            break
        try:
            r = urllib.request.urlopen("http://127.0.0.1:3998/api/health", timeout=1)
            h = json.loads(r.read())
            if h.get("setup"):
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.2)
    if not ready:
        check("--open (absent): server started in setup mode", False, "server did not become ready")
    else:
        marker_url = wait_for_marker(timeout=3)
        check("--open (absent): browser stub was NOT called",
              marker_url is None,
              "unexpected marker: %r" % marker_url)

    stop_proc(proc)
    PROC2 = None
finally:
    if PROC2 is not None:
        stop_proc(PROC2)
        PROC2 = None

# Clean up marker
if os.path.exists(BROWSER_MARKER):
    os.unlink(BROWSER_MARKER)

# Test 3: BWF_OPENED=1 should prevent opening even with --open
try:
    proc, _, _, _ = run_server(open_flag=True, env_extra={
        "BROWSER": BROWSER_CMD,
        "BWF_MARKER_FILE": BROWSER_MARKER,
        "BWF_OPENED": "1",
    })
    PROC2 = proc
    end = time.monotonic() + 10
    ready = False
    while time.monotonic() < end:
        if proc.poll() is not None:
            break
        try:
            r = urllib.request.urlopen("http://127.0.0.1:3998/api/health", timeout=1)
            h = json.loads(r.read())
            if h.get("setup"):
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.2)
    if not ready:
        check("--open (BWF_OPENED=1): server started in setup mode", False, "server did not become ready")
    else:
        marker_url = wait_for_marker(timeout=3)
        check("--open (BWF_OPENED=1): browser stub was NOT called (BWF_OPENED guard)",
              marker_url is None,
              "unexpected marker: %r" % marker_url)

    stop_proc(proc)
    PROC2 = None
finally:
    if PROC2 is not None:
        stop_proc(PROC2)
        PROC2 = None

# Cleanup temp files
if os.path.exists(BROWSER_MARKER):
    os.unlink(BROWSER_MARKER)
for f in [stub_browser]:
    if os.path.exists(f):
        os.unlink(f)
if os.path.exists(SCRIPT_DIR):
    try:
        os.rmdir(SCRIPT_DIR)
    except OSError:
        pass
if os.path.exists(os.path.dirname(SCRIPT_DIR)):
    try:
        os.rmdir(os.path.dirname(SCRIPT_DIR))
    except OSError:
        pass

# --- launcher content and a REAL run of start.sh (the old test only ran `bash -n`) ---
print("Launcher content")
sh_text = open(os.path.join(ROOT, "start.sh")).read()
check("start.sh asks for bash (it uses PIPESTATUS and read -p, which dash lacks)",
      sh_text.splitlines()[0].startswith("#!") and "bash" in sh_text.splitlines()[0], sh_text.splitlines()[0])
check("start.command is identical to start.sh",
      open(os.path.join(ROOT, "start.command")).read() == sh_text)
bat_t = open(os.path.join(ROOT, "start.bat"), "rb").read().decode("utf-8", "replace")
check("start.bat lists py and python as candidates (not the split 'py -3')", "(py python)" in bat_t, "candidate list missing")
check("start.bat shows the log tail with PowerShell (more /T does not tail)", "Get-Content -Tail 15" in bat_t)

print("start.sh runs the server for real")
if port_free(3998):
    d = tempfile.mkdtemp(prefix="bwf_run_")
    marker = os.path.join(d, "opened.txt")
    stub2 = os.path.join(d, "stub.py")
    with open(stub2, "w") as f2:
        f2.write("#!/usr/bin/env python3\nimport sys\nopen(%r, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n" % marker)
    os.chmod(stub2, 0o755)
    envr = dict(os.environ, GENCENTER_CONFIG=os.path.join(d, "none.json"), GENCENTER_DATA=os.path.join(d, "data"), BROWSER=(stub2 if os.name != "nt" else '"%s" "%s" %%s' % (sys.executable, stub2)))
    envr.pop("BWF_OPENED", None)
    logf = os.path.join(ROOT, "start.log")
    # relative to cwd=d, forward-slashed: Git Bash eats the backslashes of an
    # absolute C:\... path passed on the command line
    start_sh = os.path.relpath(os.path.join(ROOT, "start.sh"), d).replace(os.sep, "/")
    sp = subprocess.Popen(["bash", start_sh], cwd=d, env=envr, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        t_end = time.monotonic() + 12
        while time.monotonic() < t_end and not (os.path.exists(marker) and os.path.exists(logf)
                                                and "set up" in open(logf, errors="replace").read()):
            time.sleep(0.3)
        check("start.sh opened the browser at the Setup page",
              os.path.exists(marker) and "http://127.0.0.1:3998/" in open(marker).read())
        check("start.sh wrote start.log with the server output",
              os.path.exists(logf) and "set up" in open(logf, errors="replace").read())
    finally:
        try:
            if hasattr(os, "killpg"):
                os.killpg(sp.pid, 15)
            else:
                sp.terminate()
        except OSError:
            pass
        try:
            sp.wait(10)
        except subprocess.TimeoutExpired:
            if hasattr(os, "killpg"):
                os.killpg(sp.pid, 9)
            else:
                sp.kill()
            sp.wait()
        if os.path.exists(os.path.join(ROOT, "start.log")):
            os.remove(os.path.join(ROOT, "start.log"))
else:
    print("  SKIP  port 3998 in use; real start.sh run not tested")

if FAILED:
    print("\nFAILED: %d checks: %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
else:
    print("\nAll checks passed")
    sys.exit(0)
