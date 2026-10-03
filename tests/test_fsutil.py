"""Acceptance gate for fsutil.py: the shared retry helper for os.replace /
os.unlink. On Windows a file that was just written may be locked for a moment
by a virus scanner, the indexer or another thread's handle (PermissionError),
which is gone a moment later, so the call is retried; POSIX runs exactly one
plain call with no sleep. A different error is never retried.

server.py and forge_run.py must not call os.replace( from code any more.

Runs on ANY platform -- the Windows behaviour is exercised by setting
fsutil.IS_WINDOWS, never by being on Windows.

Run: python3 tests/test_fsutil.py
"""
import os
import re
import shutil
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fsutil

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond else ""))
    if not cond: FAILED.append(name)

SCRATCH = tempfile.mkdtemp(prefix="bwf_fsutil_")
REAL_FLAG = fsutil.IS_WINDOWS


class _Refuse(object):
    """Stand-in that raises PermissionError `refusals` times, then really calls `real`."""
    def __init__(self, real, refusals, exc=PermissionError):
        self.real, self.refusals, self.exc = real, refusals, exc
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        if self.calls <= self.refusals:
            raise self.exc(13, "used by another process", a[0] if a else None)
        return self.real(*a, **k)


class _Slept(object):
    """time.sleep stand-in: counts the pauses instead of taking them."""
    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1


class _Patch(object):
    """Set fsutil.IS_WINDOWS and swap the os functions + sleep, restore everything."""
    def __init__(self, windows):
        self.windows = windows

    def __enter__(self):
        self.saved = (fsutil.IS_WINDOWS, fsutil.time.sleep, fsutil.os.replace, fsutil.os.unlink)
        self.slept = _Slept()
        fsutil.IS_WINDOWS = self.windows
        fsutil.time.sleep = self.slept
        return self

    def __exit__(self, *exc):
        fsutil.IS_WINDOWS, fsutil.time.sleep, fsutil.os.replace, fsutil.os.unlink = self.saved
        return False


# 1. Real files: replace moves the content over the destination, unlink deletes
A = os.path.join(SCRATCH, "a.txt")
B = os.path.join(SCRATCH, "b.txt")
P = os.path.join(SCRATCH, "p.txt")
open(A, "w").write("new")
open(B, "w").write("old")
open(P, "w").write("gone")

with _Patch(REAL_FLAG):
    fsutil.replace(A, B)
check("replace: the source content lands on the destination", open(B).read() == "new", open(B).read())
check("replace: the source is gone", not os.path.exists(A))

with _Patch(REAL_FLAG):
    fsutil.unlink(P)
check("unlink: the file is deleted", not os.path.exists(P))

# 2. Windows: a transient PermissionError is retried, then the replace succeeds
A = os.path.join(SCRATCH, "r1.txt")
B = os.path.join(SCRATCH, "r2.txt")
open(A, "w").write("new")
open(B, "w").write("old")
with _Patch(True) as p:
    lock = _Refuse(os.replace, 2)
    fsutil.os.replace = lock
    raised = None
    try:
        fsutil.replace(A, B)
    except Exception as e:                       # noqa: BLE001 -- recorded for the check below
        raised = e
check("replace: Windows: two refusals are slept off, then the replace lands",
      raised is None and lock.calls == 3 and p.slept.calls == 2
      and open(B).read() == "new" and not os.path.exists(A),
      (repr(raised), lock.calls, p.slept.calls))

# 3. Windows: a permanent PermissionError re-raises after every attempt
A = os.path.join(SCRATCH, "p1.txt")
open(A, "w").write("new")
with _Patch(True) as p:
    lock = _Refuse(os.replace, 99)
    fsutil.os.replace = lock
    raised = None
    try:
        fsutil.replace(A, os.path.join(SCRATCH, "p2.txt"))
    except PermissionError as e:
        raised = e
check("replace: Windows: a permanent refusal re-raises after fsutil._ATTEMPTS",
      isinstance(raised, PermissionError) and lock.calls == fsutil._ATTEMPTS
      and p.slept.calls == fsutil._ATTEMPTS - 1,
      (raised, lock.calls, p.slept.calls))

# 4. POSIX: one plain call, no sleep -- the refusal propagates immediately
A = os.path.join(SCRATCH, "q1.txt")
open(A, "w").write("new")
with _Patch(False) as p:
    lock = _Refuse(os.replace, 1)
    fsutil.os.replace = lock
    raised = None
    try:
        fsutil.replace(A, os.path.join(SCRATCH, "q2.txt"))
    except PermissionError as e:
        raised = e
check("replace: POSIX: one attempt only, no sleep, the refusal propagates",
      isinstance(raised, PermissionError) and lock.calls == 1 and p.slept.calls == 0,
      (raised, lock.calls, p.slept.calls))
check("replace: POSIX: the untouched source is still there", os.path.exists(A))

# 5. A different error is never retried, in either mode
for flag in (True, False):
    A = os.path.join(SCRATCH, "n%d.txt" % flag)
    open(A, "w").write("new")
    with _Patch(flag) as p:
        lock = _Refuse(os.replace, 99, exc=FileNotFoundError)
        fsutil.os.replace = lock
        raised = None
        try:
            fsutil.replace(A, os.path.join(SCRATCH, "m%d.txt" % flag))
        except FileNotFoundError as e:
            raised = e
    check("replace: Windows=%s: a FileNotFoundError is never retried" % flag,
          isinstance(raised, FileNotFoundError) and lock.calls == 1 and p.slept.calls == 0,
          (raised, lock.calls, p.slept.calls))

A = os.path.join(SCRATCH, "u1.txt")
open(A, "w").write("gone")
with _Patch(True) as p:
    lock = _Refuse(os.unlink, 99, exc=FileNotFoundError)
    fsutil.os.unlink = lock
    raised = None
    try:
        fsutil.unlink(A)
    except FileNotFoundError as e:
        raised = e
check("unlink: a FileNotFoundError is never retried",
      isinstance(raised, FileNotFoundError) and lock.calls == 1 and p.slept.calls == 0,
      (raised, lock.calls, p.slept.calls))

# 6. Static: no code line in server.py / forge_run.py calls os.replace( any more
CALL = re.compile(r"^\s*[^#\n]*\bos\.replace\(\s*[A-Za-z_]")
offenders = []
for name in ("server.py", "forge_run.py"):
    with open(os.path.join(ROOT, name), "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if CALL.search(line):
                offenders.append("%s:%d %s" % (name, i, line.rstrip()))
for o in offenders:
    print("    offending: " + o)
check("static: no code line calls os.replace( directly", not offenders, len(offenders))

shutil.rmtree(SCRATCH, ignore_errors=True)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
