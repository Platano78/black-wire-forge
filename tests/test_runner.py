"""Acceptance gate for runner.py: resolving a run plan's placeholders,
checking its declared outputs against the job dir, and running its steps
as local programs -- exit codes, progress, timeout, stop (no orphans),
and literal-argument safety.

Run: python3 tests/test_runner.py
"""
import os
import re
import shutil
import sys
import tempfile
import threading
import time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import runner

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

STUB = os.path.join(HERE, "fixtures", "stub_prog.py")
PY = sys.executable
JOBS = []

def mkjob():
    d = tempfile.mkdtemp(prefix="runner-job-")
    JOBS.append(d)
    return d

def value_error(fn):
    try:
        fn()
        return None
    except ValueError as e:
        return str(e)

# 1. done: a plan that finishes, reports progress, and leaves its output
job = mkjob()
plan = {
    "steps": [{"argv": ["{bin:py}", STUB, "--steps", "3", "--write", "{job}/out.txt"],
               "timeout_s": 60}],
    "outputs": ["out.txt"],
    "progress": r"PROGRESS (\d+)/(\d+)",
}
steps = runner.resolve_plan(plan, {"py": PY}, job, {}, os.path.join(ROOT, "engines"))
seen = []
ok, tail, err = runner.run_steps(steps, job, progress=plan["progress"],
                                 on_progress=lambda a, b: seen.append((a, b)))
check("done: run reports ok", ok, repr(err))
check("done: progress saw 1/3, 2/3, 3/3", seen == [(1, 3), (2, 3), (3, 3)], repr(seen))
check("done: output_paths finds the written file",
      runner.output_paths(plan, job) == [os.path.join(os.path.realpath(job), "out.txt")])

# 2. non-zero exit
job = mkjob()
plan = {"steps": [{"argv": ["{bin:py}", STUB, "--steps", "1", "--exit", "3"]}],
        "outputs": [], "progress": ""}
ok, tail, err = runner.run_steps(runner.resolve_plan(plan, {"py": PY}, job, {}, "/pack"), job)
check("non-zero exit: not ok", not ok, repr(err))
check("non-zero exit: exact sentence", err == "step 1 stopped with exit code 3", repr(err))
check("non-zero exit: tail kept", len(tail) > 0, repr(tail))

# 3. timeout
job = mkjob()
plan = {"steps": [{"argv": ["{bin:py}", STUB, "--sleep", "30"], "timeout_s": 1}]}
t0 = time.monotonic()
ok, tail, err = runner.run_steps(runner.resolve_plan(plan, {"py": PY}, job, {}, "/pack"), job)
dt = time.monotonic() - t0
check("timeout: not ok", not ok, repr(err))
check("timeout: sentence names the limit",
      err is not None and "ran longer than 1 seconds" in err, repr(err))
check("timeout: finished in under 10 s", dt < 10, "%.1f s" % dt)

# 4. stop: the whole process group goes, child included
job = mkjob()
plan = {"steps": [{"argv": ["{bin:py}", STUB, "--spawn-child"], "timeout_s": 30}]}
stop = threading.Event()
threading.Timer(1.0, stop.set).start()
ok, tail, err = runner.run_steps(runner.resolve_plan(plan, {"py": PY}, job, {}, "/pack"),
                                 job, stop_event=stop)
check("stop: not ok", not ok, repr(err))
check("stop: error is 'stopped'", err == "stopped", repr(err))
m = re.search(r"CHILD (\d+)", "\n".join(tail))
if m:
    child = int(m.group(1))
    dead = False
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            dead = True
            break
        time.sleep(0.2)
    check("stop: spawned child is gone within 7 s", dead)
else:
    check("stop: CHILD pid was visible in tail", False, repr(tail))

# 5. literal args: a hostile value is one inert argument, never executed
job = mkjob()
plan = {"steps": [{"argv": ["{bin:py}", STUB, "--echo-args", "{in:f}"]}],
        "outputs": [], "progress": ""}
steps = runner.resolve_plan(plan, {"py": PY}, job, {"f": "x; echo PWNED $(id)"}, "/pack")
ok, tail, err = runner.run_steps(steps, job)
check("literal args: run is ok", ok, repr(err))
check("literal args: the whole string is one argument", "ARG x; echo PWNED $(id)" in tail, repr(tail))
check("literal args: nothing was ever executed",
      not any(line.startswith("PWNED") for line in tail), repr(tail))

# 6. placeholders
job = mkjob()
bins = {"py": PY}
def resolve(argv, **kw):
    inputs = kw.get("inputs", {})
    return value_error(lambda: runner.resolve_plan(
        {"steps": [{"argv": argv}], "outputs": [], "progress": ""},
        bins, job, inputs, "/pack"))
msg = resolve(["{bin:py}", STUB, "{foo}"])
check("placeholders: unknown {foo} is refused", msg is not None and "{foo}" in msg, repr(msg))
msg = resolve(["{bin:py}", "{bin:nope}"])
check("placeholders: missing bin role is named", msg is not None and "nope" in msg, repr(msg))
msg = resolve(["{bin:py}", "{in:model}"])
check("placeholders: missing input field is named", msg is not None and "model" in msg, repr(msg))
steps = runner.resolve_plan({"steps": [{"argv": ["{bin:py}", STUB, '{"a":1}']}],
                             "outputs": [], "progress": ""}, bins, job, {}, "/pack")
check("placeholders: plain braces pass through unchanged",
      steps[0]["argv"][2] == '{"a":1}', repr(steps[0]["argv"]))

# 7. outputs: every escape and every dead file is refused
job = mkjob()
OUTSIDE = tempfile.mkdtemp(prefix="runner-outside-")
JOBS.append(OUTSIDE)
outside = os.path.join(OUTSIDE, "outside.txt")
with open(outside, "w") as f:
    f.write("x")
with open(os.path.join(job, "good.bin"), "w") as f:
    f.write("ok")
open(os.path.join(job, "empty.bin"), "w").close()
os.symlink(outside, os.path.join(job, "sneaky.bin"))
def out_error(names):
    return value_error(lambda: runner.output_paths({"outputs": names}, job))
check("outputs: ../escape.txt refused", out_error(["../escape.txt"]) is not None)
check("outputs: an absolute path refused", out_error([outside]) is not None)
check("outputs: a symlink out of the job refused", out_error(["sneaky.bin"]) is not None)
check("outputs: a missing file refused", out_error(["ghost.bin"]) is not None)
check("outputs: an empty file refused", out_error(["empty.bin"]) is not None)
check("outputs: a real file passes",
      runner.output_paths({"outputs": ["good.bin"]}, job) == [os.path.join(os.path.realpath(job), "good.bin")])

# 8. bad plan shapes
def refused(fn):
    return value_error(fn) is not None
check("plan: empty steps refused",
      refused(lambda: runner.resolve_plan({"steps": []}, bins, job, {}, "/pack")))
check("plan: argv that is not a list refused",
      refused(lambda: runner.resolve_plan({"steps": [{"argv": "ls"}]}, bins, job, {}, "/pack")))
check("plan: empty argv refused",
      refused(lambda: runner.resolve_plan({"steps": [{"argv": []}]}, bins, job, {}, "/pack")))
check("plan: timeout of 0 refused",
      refused(lambda: runner.resolve_plan({"steps": [{"argv": ["x"], "timeout_s": 0}]}, bins, job, {}, "/pack")))
check("plan: timeout of 999999 refused",
      refused(lambda: runner.resolve_plan({"steps": [{"argv": ["x"], "timeout_s": 999999}]}, bins, job, {}, "/pack")))

# 9. a program that cannot start
job = mkjob()
steps = runner.resolve_plan({"steps": [{"argv": ["{bin:ghost}", "go"]}], "outputs": [], "progress": ""},
                            {"ghost": "/definitely/not/installed/here"}, job, {}, "/pack")
ok, tail, err = runner.run_steps(steps, job)
check("unstartable: not ok", not ok, repr(err))
check("unstartable: sentence names the step",
      err is not None and err.startswith("step 1 could not start"), repr(err))

for d in JOBS:
    shutil.rmtree(d, ignore_errors=True)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
