"""v1.4.4: a fake_comfy.py fixture must not outlive the process that started it.

scripts/run-tests.sh runs each suite under `timeout`; SIGTERM reaches the suite's
python only, and the fixtures it started with subprocess.Popen (tests/test_cut_scale.py,
tests/test_audio_led.py, ...) were left running on a random port for days (22 of them,
up to 2.7 days old, found 2026-10-10). The fixture now carries a parent watchdog
(tests/fixtures/fake_comfy.py, `_watch_parent`).

This starts a throwaway python as the fixture's parent, SIGKILLs that parent (so
nothing can clean up after it), and asserts the fixture's port stops answering within
6 s. A control first proves the fixture does answer, and keeps answering past the
watchdog's 2 s tick while its parent is alive (no false trip).

Run: python3 tests/test_fixture_reaping.py
"""
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "fake_comfy.py")

if os.name != "posix":
    print("SKIP: the parent watchdog is POSIX-only (os.getppid() does not re-parent on Windows)")
    sys.exit(0)

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond and detail != "" else ""))
    if not cond:
        FAILED.append(name)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def answers(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % port, timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie (killed, not yet reaped by its new parent) is not running
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


PARENT = (
    "import subprocess, sys, time\n"
    "p = subprocess.Popen([sys.executable, sys.argv[1], '--port', sys.argv[2]],\n"
    "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print(p.pid, flush=True)\n"
    "time.sleep(120)\n"
)

port = free_port()
parent = subprocess.Popen([sys.executable, "-c", PARENT, FIXTURE, str(port)], stdout=subprocess.PIPE, text=True)
fixture_pid = None
try:
    fixture_pid = int(parent.stdout.readline())
    deadline = time.time() + 15
    while time.time() < deadline and not answers(port):
        time.sleep(0.2)
    check("control: the fixture answers while its parent is alive", answers(port))
    time.sleep(3)   # past the watchdog's 2 s tick
    check("control: it is still answering after 3 s with its parent alive (no false trip)", answers(port))

    parent.send_signal(signal.SIGKILL)
    parent.wait(timeout=10)
    t0 = time.time()
    while time.time() - t0 < 6 and answers(port):
        time.sleep(0.25)
    gone = not answers(port)
    check("after the parent is SIGKILLed the fixture's port stops answering within 6 s", gone,
          "still answering %.1fs after the parent died" % (time.time() - t0))
    t1 = time.time()
    while time.time() - t1 < 3 and alive(fixture_pid):
        time.sleep(0.2)
    check("and the fixture process is gone", not alive(fixture_pid), "pid %d still running" % fixture_pid)
finally:
    # a fixture that did not reap itself (the bug) must not outlive THIS test either
    if fixture_pid and alive(fixture_pid):
        try:
            os.kill(fixture_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if parent.poll() is None:
        parent.kill()
        parent.wait()

print()
if FAILED:
    print("FAILED: %d" % len(FAILED))
    sys.exit(1)
print("ALL PASS")
