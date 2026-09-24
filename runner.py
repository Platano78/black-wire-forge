"""Engine-agnostic run-plan runner.

A pack's run plan is a plain dict: an ordered list of steps (each an argv
plus a timeout), the names of the outputs it promises, and a progress
regex. This module resolves the plan's placeholders into real paths, runs
each step as a local program (never through a shell), reads stdout line
by line for progress and a short output tail, and can stop the whole
process group on request.

Stdlib only. Nothing here is tied to any particular program or engine;
the plan says what to run and this only runs it.
"""
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque

# The two token shapes the spec allows, and the shape that catches every
# OTHER {word} / {word:word} so it can be refused loudly. Known tokens
# must come first in the alternation so {job} / {bin:x} never fall
# through to the unknown shape.
KNOWN = re.compile(r"\{(bin|in):([A-Za-z0-9_]+)\}|\{(job|pack)\}")
UNKNOWN = re.compile(r"\{[a-z]+(:[A-Za-z0-9_]+)?\}")
TOKEN = re.compile(KNOWN.pattern + "|" + UNKNOWN.pattern)


def _fill(text, bins, inputs, job_dir, pack_dir):
    """Substitute the allowed placeholders in one argv element.

    Values are inserted literally; there is never a shell, so a value
    containing `;`, `$(...)` or friends stays one inert argument.
    """
    def repl(m):
        kind, name = m.group(1), m.group(2)
        if kind == "bin":
            if name not in bins:
                raise ValueError("this needs %s installed on this machine" % name)
            return bins[name]
        if kind == "in":
            if name not in inputs:
                raise ValueError("the plan asks for input field %s but it was not provided" % name)
            return inputs[name]
        if m.group(3):
            return job_dir if m.group(3) == "job" else pack_dir
        raise ValueError("unknown placeholder %s in the run plan" % m.group(0))
    return TOKEN.sub(repl, text)


def resolve_plan(plan, bins, job_dir, inputs, pack_dir):
    """Validate a run plan and resolve every placeholder in its argvs.

    Returns a list of {"argv": [str], "timeout_s": number} ready for
    run_steps(). Bad shapes raise ValueError in plain English.
    """
    if not isinstance(plan, dict):
        raise ValueError("the run plan must be an object with a steps list")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("the run plan has no steps")
    resolved = []
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError("step %d is not a valid step" % i)
        argv = step.get("argv")
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(a, str) for a in argv)):
            raise ValueError("step %d must name its program as a non-empty list of arguments" % i)
        timeout = step.get("timeout_s", 3600)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not (0 < timeout <= 21600)):
            raise ValueError("step %d's timeout must be a number of seconds above 0 and at most 21600" % i)
        resolved.append({
            "argv": [_fill(a, bins, inputs, job_dir, pack_dir) for a in argv],
            "timeout_s": timeout,
        })
    return resolved


def output_paths(plan, job_dir):
    """Resolve the plan's output names to real paths inside the job dir.

    Same containment rule the server uses for its local output store:
    realpath of the join must equal the job dir or sit under it. Every
    output must exist and be non-empty.
    """
    outputs = plan.get("outputs") if isinstance(plan, dict) else None
    if (not isinstance(outputs, list) or not outputs
            or not all(isinstance(o, str) for o in outputs)):
        raise ValueError("the run plan must name at least one output")
    base = os.path.realpath(job_dir)
    paths = []
    for name in outputs:
        if os.path.isabs(name):
            raise ValueError("output %s must be a path inside the job's folder, not a full path" % name)
        path = os.path.realpath(os.path.join(job_dir, name))
        if path != base and not path.startswith(base + os.sep):
            raise ValueError("output %s escapes the job's folder and is not allowed" % name)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise ValueError("output %s is missing or empty" % name)
        paths.append(path)
    return paths


def run_steps(steps, cwd, progress=None, on_progress=None, stop_event=None):
    """Run resolved steps in order as local programs, never via a shell.

    Returns (ok, tail, error): ok is True only if every step exits 0;
    tail holds the last 20 output lines across all steps; error is a
    plain sentence when something went wrong.
    """
    prog = re.compile(progress) if progress else None
    tail = deque(maxlen=20)

    def kill_group(proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

    for n, step in enumerate(steps, 1):
        argv = step["argv"]
        timeout = step["timeout_s"]
        reason = []  # filled by the watchdog: "timeout" or "stop"
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", bufsize=1, start_new_session=True,
                preexec_fn=lambda: os.nice(10))
        except (FileNotFoundError, PermissionError) as exc:
            return False, list(tail), "step %d could not start: %s" % (n, exc)
        wd_stop = threading.Event()

        def watchdog():
            end = time.monotonic() + timeout if timeout else None
            while not wd_stop.is_set():
                time.sleep(0.2)
                if wd_stop.is_set():
                    return
                timed_out = end is not None and time.monotonic() > end
                if timed_out or (stop_event is not None and stop_event.is_set()):
                    reason.append("timeout" if timed_out else "stop")
                    kill_group(proc)
                    return

        wd = threading.Thread(target=watchdog, daemon=True)
        wd.start()
        for line in proc.stdout:
            text = line.rstrip("\n")
            tail.append(text)
            if prog is not None and on_progress is not None:
                m = prog.search(line)
                if m:
                    on_progress(int(m.group(1)), int(m.group(2)))
        proc.wait()
        wd_stop.set()
        wd.join()
        if reason and reason[0] == "timeout":
            return False, list(tail), "step %d ran longer than %s seconds and was stopped" % (n, timeout)
        if reason and reason[0] == "stop":
            return False, list(tail), "stopped"
        if proc.returncode != 0:
            return False, list(tail), "step %d stopped with exit code %d" % (n, proc.returncode)
    return True, list(tail), None
