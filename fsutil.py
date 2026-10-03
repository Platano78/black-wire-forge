"""File replace/delete that survives a moment of Windows refusal.

Windows briefly raises PermissionError when a virus scanner, the search indexer or
another thread's open handle still has the file, and the very next attempt
succeeds, so os.replace/os.unlink are retried a few times there. POSIX never
locks this way and runs exactly one plain call, with no sleeping.
"""
import os
import time

IS_WINDOWS = (os.name == "nt")
_ATTEMPTS = 15          # x 0.2 s = 3 s at most, Windows only
_PAUSE = 0.2


def _retry(fn, *args):
    attempts = _ATTEMPTS if IS_WINDOWS else 1
    for i in range(attempts):
        try:
            return fn(*args)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(_PAUSE)


def replace(src, dst):
    """os.replace, retried briefly on Windows (see module note). POSIX: exactly one plain os.replace."""
    return _retry(os.replace, src, dst)


def unlink(path):
    """os.unlink, retried briefly on Windows. POSIX: exactly one plain os.unlink."""
    return _retry(os.unlink, path)
