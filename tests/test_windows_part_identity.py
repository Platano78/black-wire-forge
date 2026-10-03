"""Acceptance gate for the Windows .part identity rules: on NTFS a path-based
lstat and a handle-based fstat of the SAME file disagree on size and mtime
while it is being written or just closed, so server.py must compare only
(dev, ino) there -- on Windows it must also retry a locked unlink.

Runs on ANY platform -- the Windows behaviour is exercised by setting
srv._IS_WINDOWS / fsutil.IS_WINDOWS, never by being on Windows.

Run: python3 tests/test_windows_part_identity.py
"""
import importlib.util
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import types

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import fsutil

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + str(detail)) if not cond else ""))
    if not cond: FAILED.append(name)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

SCRATCH = tempfile.mkdtemp(prefix="bwf_win_part_")
CONFIG = os.path.join(SCRATCH, "config.json")
json.dump({"port": free_port(), "bind": "127.0.0.1", "title": "win-part",
           "timing": {"poll_seconds": 30, "job_poll_seconds": 30},
           "lanes": [{"id": "t", "name": "T", "host": "127.0.0.1", "port": 1, "caps": ["image"]}]},
          open(CONFIG, "w"))
os.environ["GENCENTER_CONFIG"] = CONFIG
os.environ["GENCENTER_DATA"] = os.path.join(SCRATCH, "data")

spec = importlib.util.spec_from_file_location("srv_win_part", os.path.join(ROOT, "server.py"))
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

UID = os.getuid() if hasattr(os, "getuid") else 0

def fake_st(mode=stat.S_IFREG | 0o644, dev=7, ino=11, size=10, mtime_ns=5, uid=None):
    return types.SimpleNamespace(st_mode=mode, st_dev=dev, st_ino=ino, st_size=size,
                                 st_mtime_ns=mtime_ns, st_uid=UID if uid is None else uid)

REAL_FLAG = srv._IS_WINDOWS
def as_windows(flag):
    def deco(fn):
        def run(*a, **k):
            srv._IS_WINDOWS = flag
            fsutil.IS_WINDOWS = flag
            try:
                return fn(*a, **k)
            finally:
                srv._IS_WINDOWS = REAL_FLAG
                fsutil.IS_WINDOWS = REAL_FLAG
        return run
    return deco

# 1. _same_file: size/mtime drift is ignored on Windows, inode is not
TARGET = os.path.join(SCRATCH, "real.bin")
open(TARGET, "wb").write(b"x" * 10)
A = os.lstat(TARGET)
B = fake_st(dev=A.st_dev, ino=A.st_ino, size=A.st_size + 4096, mtime_ns=A.st_mtime_ns + 999)
C = fake_st(dev=A.st_dev, ino=A.st_ino + 1, size=A.st_size, mtime_ns=A.st_mtime_ns)
D = fake_st(mode=stat.S_IFDIR | 0o755, dev=A.st_dev, ino=A.st_ino, size=A.st_size, mtime_ns=A.st_mtime_ns)

@as_windows(False)
def same_file_posix():
    check("same_file: POSIX: a size/mtime drift is not the same file", srv._same_file(A, B) is False)
    check("same_file: POSIX: identical stat results are the same file",
          srv._same_file(A, types.SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_dev=A.st_dev,
                                                   st_ino=A.st_ino, st_size=A.st_size,
                                                   st_mtime_ns=A.st_mtime_ns, st_uid=UID)) is True)
    check("same_file: POSIX: a different inode is not the same file", srv._same_file(A, C) is False)

@as_windows(True)
def same_file_windows():
    check("same_file: Windows: a size/mtime drift IS the same file", srv._same_file(A, B) is True)
    check("same_file: Windows: a different inode is still not the same file",
          srv._same_file(A, C) is False)

@as_windows(False)
def same_file_modes_posix():
    check("same_file: POSIX: a directory is not the same file", srv._same_file(D, A) is False)

@as_windows(True)
def same_file_modes_windows():
    check("same_file: Windows: a directory is not the same file", srv._same_file(D, A) is False)
    check("same_file: Windows: None is not the same file",
          srv._same_file(None, A) is False and srv._same_file(A, None) is False)

same_file_posix(); same_file_windows(); same_file_modes_posix(); same_file_modes_windows()

# 2. _part_matches: same, for the recorded .part
REC = {"dev": 1, "ino": 2, "size": 10, "mtime_ns": 5}
DRIFT = fake_st(dev=1, ino=2, size=4096, mtime_ns=999)
OTHER = fake_st(dev=1, ino=3, size=10, mtime_ns=5)

@as_windows(False)
def part_posix():
    check("part: POSIX: a size/mtime drift does not match", srv._part_matches(REC, DRIFT) is False)
    check("part: POSIX: an unchanged .part matches",
          srv._part_matches(REC, fake_st(dev=1, ino=2, size=10, mtime_ns=5)) is True)
    check("part: POSIX: a different inode does not match", srv._part_matches(REC, OTHER) is False)

@as_windows(True)
def part_windows():
    check("part: Windows: a size/mtime drift still matches", srv._part_matches(REC, DRIFT) is True)
    check("part: Windows: a different inode does not match", srv._part_matches(REC, OTHER) is False)
    check("part: Windows: grown is not needed any more",
          srv._part_matches(REC, fake_st(dev=1, ino=2, size=1, mtime_ns=0), grown=True) is True)
    check("part: Windows: an empty record never matches", srv._part_matches({}, DRIFT) is False)
    check("part: Windows: a directory never matches",
          srv._part_matches(REC, fake_st(mode=stat.S_IFDIR | 0o755, dev=1, ino=2, size=10, mtime_ns=5)) is False)

part_posix(); part_windows()

# 3. fsutil.unlink (server._unlink_retry delegates to it): a locked file is retried
#    on Windows, refused once on POSIX
LOCKED = os.path.join(SCRATCH, "locked.part")
open(LOCKED, "wb").write(b"part")

class _Lock(object):
    """os.unlink stand-in that refuses `refusals` times before really unlinking."""
    def __init__(self, refusals):
        self.refusals, self.calls = refusals, 0
        self.real = fsutil.os.unlink
    def __call__(self, path):
        self.calls += 1
        if self.calls <= self.refusals:
            raise PermissionError(13, "used by another process", path)
        self.real(path)

@as_windows(True)
def unlink_windows_transient():
    open(LOCKED, "wb").write(b"part")
    lock, real_sleep = _Lock(2), fsutil.time.sleep
    fsutil.os.unlink, fsutil.time.sleep = lock, lambda *a, **k: None
    try:
        fsutil.unlink(LOCKED)
    finally:
        fsutil.os.unlink, fsutil.time.sleep = lock.real, real_sleep
    check("unlink: Windows: two refusals are retried, then the file is gone",
          not os.path.exists(LOCKED) and lock.calls == 3, lock.calls)

@as_windows(True)
def unlink_windows_permanent():
    open(LOCKED, "wb").write(b"part")
    lock, real_sleep = _Lock(99), fsutil.time.sleep
    fsutil.os.unlink, fsutil.time.sleep = lock, lambda *a, **k: None
    raised = False
    try:
        fsutil.unlink(LOCKED)
    except PermissionError:
        raised = True
    finally:
        fsutil.os.unlink, fsutil.time.sleep = lock.real, real_sleep
    check("unlink: Windows: a permanent lock re-raises after the attempts",
          raised and lock.calls == 15, (raised, lock.calls))

@as_windows(False)
def unlink_posix():
    open(LOCKED, "wb").write(b"part")
    lock = _Lock(99)
    fsutil.os.unlink = lock
    raised = False
    try:
        fsutil.unlink(LOCKED)
    except PermissionError:
        raised = True
    finally:
        fsutil.os.unlink = lock.real
    check("unlink: POSIX: one attempt only, and the refusal propagates",
          raised and lock.calls == 1, (raised, lock.calls))
    check("unlink: POSIX: the file is still there",
          os.path.exists(LOCKED))

unlink_windows_transient(); unlink_windows_permanent(); unlink_posix()
os.path.exists(LOCKED) and os.unlink(LOCKED)

shutil.rmtree(SCRATCH, ignore_errors=True)

print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)