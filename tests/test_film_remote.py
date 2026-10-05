#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engines.film_remote as fr
from engines.film_remote import film5080, film9700


def seed(plan):
    argv = plan["steps"][0]["argv"]
    return int(argv[argv.index("--seed") + 1])


large = 278347231016169
real = fr._alive
# The seed clamp is a property of the 5080 plan, so pin the lane instead of
# letting a live probe decide which backend this check measures.
fr._alive = lambda base, token_file: True
try:
    assert 0 <= seed(film5080({"seed": large}, {})) <= 2147483647
finally:
    fr._alive = real
assert seed(film9700({"seed": large}, {})) == large

# 5080 down -> refused at submit, never silently rerouted to the R9700.
real = fr._alive
try:
    fr._alive = lambda base, token_file: False
    try:
        film5080({"seed": 7, "prompt": "x"}, {})
        raise AssertionError("dead 5080 must refuse, not reroute")
    except ValueError as e:
        assert "5080" in str(e) and "offline" in str(e), e

    fr._alive = lambda base, token_file: True
    up = film5080({"seed": 7, "prompt": "x"}, {})
    assert up["outputs"] == ["film5080.mp4"], up
finally:
    fr._alive = real

# A dead address is not available, and probing it must not raise.
assert fr._alive("http://127.0.0.1:9", None) is False

print("ALL PASS")
