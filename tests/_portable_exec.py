"""A runnable stub program, on every platform.

On Linux the gates write a shell script and chmod it, and that stays exactly
what happens. On Windows a program has to be something CreateProcess can start
for real: a .cmd file (or sys.executable), never a shebang script. So there the
stub is written as `<name>.cmd` (a one-line `set` header plus the command) and
that path is what goes into a config.
"""
import os
import sys


def make_program(path, posix_text, windows_cmd=None, py_body=None):
    """Create a runnable stub at `path` and return the path to put in a config.
    POSIX: write posix_text, chmod 0o755, return path (identical to what the
    tests do today). Windows: the program must carry a .cmd extension and
    cannot be a shell script. If py_body is given, write it to path + '.py'
    and write path + '.cmd' containing
    @"<sys.executable>" "<path>.py" %* (CRLF line endings); else write
    windows_cmd (a .cmd body) to path + '.cmd'. Return path + '.cmd'."""
    if os.name != "nt":
        with open(path, "w") as f:
            f.write(posix_text)
        os.chmod(path, 0o755)
        return path
    if py_body is not None:
        with open(path + ".py", "w") as f:
            f.write(py_body)
        body = '@"%s" "%s.py" %%*\r\n' % (sys.executable, path)
    else:
        body = windows_cmd
    with open(path + ".cmd", "w", newline="") as f:
        f.write(body)
    return path + ".cmd"