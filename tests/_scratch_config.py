"""Shared by every test that `exec_module`s server.py directly: CONFIG =
load_config() runs at server.py's OWN import time (module level, server.py:215),
eagerly reading GENCENTER_CONFIG -- so a test that imports server.py without
setting this FIRST falls through to the developer's own config.json, which is
gitignored and does not exist on a fresh clone (orchestrator review 2026-09-24:
9 suites died silently there, SystemExit(2) from server.py's own die(), with
no traceback -- SystemExit is special-cased unhandled at the top level).

Importing this module is the fix: it writes one minimal, valid config.json (and
routes GENCENTER_DATA alongside it) to a fresh scratch directory, then points
both env vars there, before any test goes on to exec_module server.py itself.

Usage (before the `spec.loader.exec_module(...)` line):
    import _scratch_config
"""
import json
import os
import tempfile

_dir = tempfile.mkdtemp(prefix="bwf-test-cfg-")
os.environ["GENCENTER_CONFIG"] = os.path.join(_dir, "config.json")
os.environ["GENCENTER_DATA"] = os.path.join(_dir, "data")
with open(os.environ["GENCENTER_CONFIG"], "w") as _f:
    json.dump({"port": 0, "bind": "127.0.0.1",
               "lanes": [{"id": "x", "name": "x", "host": "127.0.0.1", "port": 1}]}, _f)
