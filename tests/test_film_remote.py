#!/usr/bin/env python3
"""The remote Film/YuE/Qwen packs: opt-in by their service address, and
plan building that never touches the network.

Every environment variable this sets is restored before it exits.
"""
import os, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import engines

FAILED = []
def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  " + detail) if not cond and detail else ""))
    if not cond: FAILED.append(name)

BASES = ["FILM5080_BASE", "FILM9700_BASE", "YUE2_5080_BASE", "YUE_R9700_BASE", "QWEN_IMAGE_BASE"]
REMOTE = {"film_remote", "music_remote", "qwen_remote"}
SAVED = {k: os.environ.get(k) for k in BASES}


def rescan():
    engines._PACKS = None
    return {p["id"] for p in engines.packs()}


def argv_of(plan, flag):
    argv = plan["steps"][0]["argv"]
    return argv[argv.index(flag) + 1]


try:
    for k in BASES:
        os.environ.pop(k, None)
    print("stock: no service address set")
    ids = rescan()
    check("no remote pack is loaded", not (ids & REMOTE), repr(sorted(ids & REMOTE)))
    REMOTE_MODES = {"film5080", "film9700", "yue2.yue2-bf16-5080", "yue.int8-r9700",
                    "qwen.image21-r9700", "qwen.texture-r9700"}
    offered = {m for cap in engines.caps() for m in engines.modes_for(cap)}
    check("no remote mode is offered", not (offered & REMOTE_MODES), repr(sorted(offered & REMOTE_MODES)))

    print("FILM5080_BASE set: only the Film pack appears")
    os.environ["FILM5080_BASE"] = "http://127.0.0.1:9"
    ids = rescan()
    check("film_remote loaded, the others not", ids & REMOTE == {"film_remote"}, repr(sorted(ids & REMOTE)))
    check("film5080 can run", engines.mode_deps_reason("video", "film5080") is None)
    why = engines.mode_deps_reason("video", "film9700")
    check("film9700 says which setting it needs", bool(why) and "FILM9700_BASE" in why, repr(why))

    print("every base set: all three packs appear")
    os.environ.update({"FILM9700_BASE": "http://127.0.0.1:9", "YUE2_5080_BASE": "http://127.0.0.1:9",
                       "YUE_R9700_BASE": "http://127.0.0.1:9", "QWEN_IMAGE_BASE": "http://127.0.0.1:9"})
    ids = rescan()
    check("all three loaded", REMOTE <= ids, repr(sorted(ids & REMOTE)))

    from engines.film_remote import film5080, film9700
    from engines.music_remote import yue2_5080
    from engines.qwen_remote import qwen_image

    print("plans")
    large = 278347231016169
    # Port 9 (discard) is never a Film service: if plan building probed the
    # network it would fail or wait; it must do neither.
    t0 = time.monotonic()
    plan = film5080({"seed": large}, {})
    check("building a plan does not wait on the network", time.monotonic() - t0 < 1.0)
    check("5080 seed clamped to signed 32-bit", 0 <= int(argv_of(plan, "--seed")) <= 2147483647)
    check("9700 seed untouched", int(argv_of(film9700({"seed": large}, {}), "--seed")) == large)
    check("the plan carries the configured base", argv_of(plan, "--base") == "http://127.0.0.1:9")
    check("...and names its setting for the client's sentences", argv_of(plan, "--base-setting") == "FILM5080_BASE")
    check("output name", plan["outputs"] == ["film5080.mp4"], repr(plan["outputs"]))
    check("pack no longer probes during plan building", not hasattr(sys.modules["engines.film_remote"], "_alive"))
    for name, p in (("film", plan), ("yue", yue2_5080({}, {})), ("qwen", qwen_image({}, {}))):
        check("%s plan: the client gets a deadline under the step timeout" % name,
              0 < int(argv_of(p, "--deadline-s")) < p["steps"][0]["timeout_s"])
finally:
    for k, v in SAVED.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    engines._PACKS = None

check("environment restored", all(os.environ.get(k) == v for k, v in SAVED.items()))
print()
print(("FAILED: %d" % len(FAILED)) if FAILED else "ALL PASS")
sys.exit(1 if FAILED else 0)
