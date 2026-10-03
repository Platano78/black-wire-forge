"""Loaded automatically by every test process (scripts/run-tests.sh puts tests/ on PYTHONPATH).

On Windows a virtual environment's python.exe is only a launcher: it starts the real interpreter
as a child. A test that does proc.kill() or proc.terminate() on the launcher therefore leaves the
server or fake ComfyUI it started running, holding its port, and every later test that wants that
port talks to the wrong process. Make kill() and terminate() take the whole process tree down.
Nothing happens on POSIX.
"""
import os

if os.name == "nt":
    import subprocess

    _orig_kill = subprocess.Popen.kill
    _orig_terminate = subprocess.Popen.terminate

    def _kill_tree(proc):
        if proc.returncode is None:
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    def _kill(self):
        _kill_tree(self)
        try:
            _orig_kill(self)
        except OSError:
            pass

    def _terminate(self):
        _kill_tree(self)
        try:
            _orig_terminate(self)
        except OSError:
            pass

    subprocess.Popen.kill = _kill
    subprocess.Popen.terminate = _terminate
