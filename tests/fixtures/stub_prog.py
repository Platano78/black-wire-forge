#!/usr/bin/env python3
"""Tiny stand-in program for the runner tests. Prints a progress line per
step, can write a file, exit with a code, sleep, spawn a child, or echo
its own arguments back -- one behavior per flag.
"""
import argparse
import subprocess
import sys
import time


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--steps", type=int, default=0)
    p.add_argument("--write", default=None)
    p.add_argument("--exit", dest="exit_code", type=int, default=0)
    p.add_argument("--sleep", dest="sleep_s", type=float, default=0.0)
    p.add_argument("--spawn-child", action="store_true")
    p.add_argument("--echo-args", action="store_true")
    p.add_argument("rest", nargs="*", help="extra args, only ever ECHOED back")
    a = p.parse_args()

    for i in range(1, a.steps + 1):
        print("PROGRESS %d/%d" % (i, a.steps), flush=True)
    if a.write:
        with open(a.write, "w") as f:
            f.write("ok")
    if a.echo_args:
        for arg in sys.argv[1:]:
            print("ARG %s" % arg, flush=True)
    if a.spawn_child:
        # A child in the same process group: a group kill must take it down.
        child = subprocess.Popen(["sleep", "300"])
        print("CHILD %d" % child.pid, flush=True)
        time.sleep(300)
    if a.sleep_s:
        time.sleep(a.sleep_s)
    sys.exit(a.exit_code)


if __name__ == "__main__":
    main()
