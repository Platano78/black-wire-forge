"""A Setup save must not leave the launcher with nothing to wait on: the process the
user (or this test) started stays alive across the restart, and stopping it frees the port.

On POSIX the restart is an execv() -- the same pid keeps running. On Windows there is no
execv(): the server stops itself and runs the new copy as a CHILD, so the parent process
we started is still the one to kill. Either way proc.poll() stays None and 3998 is free
afterwards (no stray grandchild left holding it).

Run: python3 tests/test_setup_restart_keeps_process.py
"""
import http.client
import os
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _setup_fixture as fx  # noqa: E402
from _setup_fixture import check  # noqa: E402


try:
    if not fx.port_free(fx.SETUP_PORT):
        check("port %d is free for the Setup server" % fx.SETUP_PORT, False,
              "something else is listening on it; stop it and run again")
        fx.finish()
    lane_port, helper_port = fx.start_fakes()

    print("(a) a Setup save restarts the server, and we are still the one running it")
    proc, cfg_path, log_path = fx.boot("restart")
    health = fx.wait_health(proc, want_setup=True)
    check("the Setup server starts", health is not None,
          "exit=%s log=%s" % (proc.poll(), open(log_path).read()[-600:]))
    if health is None:
        fx.finish()

    answers = {"comfy": {"host": "127.0.0.1", "port": lane_port, "name": "My ComfyUI"},
               "who": "local"}
    _ = helper_port   # the fakes are booted like the other Setup suites; this one needs no guide
    pid = proc.pid
    conn = None
    try:
        # A browser holds its keep-alive connection open across the save. HTTP/1.1 leaves it
        # open on its handler thread; urllib in fx.http() would not, which is why this uses
        # http.client directly. On Windows that accepted socket keeps the port "in use" and
        # the restarted child's bind() would fail without the server closing it.
        conn = http.client.HTTPConnection("127.0.0.1", 3998, timeout=10)
        conn.request("GET", "/api/health")
        conn.getresponse().read()

        code, body = fx.http("POST", "/api/setup/write", answers)
        check("write -> ok", code == 200 and body.get("ok"), (code, body))
        health = fx.wait_health(proc, want_setup=False, timeout=30)
        check("the server comes back configured", health is not None,
              "health=%s exit=%s log=%s" % (health, proc.poll(), open(log_path).read()[-600:]))

        print("(b) a keep-alive connection held open across the restart")
        check("a browser-style keep-alive connection held open across the restart does not block "
              "the new server", health is not None and health.get("setup") is False,
              "health=%s exit=%s log=%s" % (health, proc.poll(), open(log_path).read()[-600:]))

        print("(c) the process we started is still alive (execv on POSIX, supervisor on Windows)")
        check("proc.poll() is None after the restart: nothing for the launcher to lose",
              proc.poll() is None, "exit=%s log=%s" % (proc.poll(), open(log_path).read()[-600:]))
        check("it is still the same process we started", proc.pid == pid, (pid, proc.pid))
        code, body = fx.http("GET", "/api/health")
        check("and it is the one serving 3998 now", code == 200 and body.get("setup") is False,
              (code, body))

        print("(d) stopping it leaves nothing listening on 3998")
        fx.stop(proc)
        for _ in range(100):
            if fx.port_free(fx.SETUP_PORT):
                break
            time.sleep(0.1)
        check("port %d is free again: no orphan left holding it" % fx.SETUP_PORT,
              fx.port_free(fx.SETUP_PORT), open(log_path).read()[-600:])
    finally:
        if conn is not None:
            conn.close()
except SystemExit:
    raise
finally:
    fx.finish()