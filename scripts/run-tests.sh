#!/usr/bin/env bash
# Run test_*.py suites one at a time with TMPDIR on disk (not the box's tmpfs /tmp,
# on distros where /tmp is RAM-backed), and guarantee scratch is deleted even if a
# suite times out or is killed. Scratch defaults under ~/.cache, not the repo --
# override with SCRATCH_ROOT if needed.
set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TESTS_DIR="$REPO_DIR/tests"
SCRATCH_ROOT="${SCRATCH_ROOT:-$HOME/.cache/bwf-tests}"
TIMEOUT="${TIMEOUT:-900}"
SKIP="${SKIP:-}"

mkdir -p "$SCRATCH_ROOT"

# The repo's own virtual environment when there is one (README installs into
# .venv; a PEP 668 system python has none of requirements*.txt), else python3.
if [ -x "$REPO_DIR/.venv/bin/python" ]; then
    PYTHON="$REPO_DIR/.venv/bin/python"
elif [ -x "$REPO_DIR/.venv/Scripts/python.exe" ]; then      # Windows (Git Bash)
    PYTHON="$REPO_DIR/.venv/Scripts/python.exe"
else
    PYTHON="python3"
fi
# tests/sitecustomize.py (loaded by every test process) makes proc.kill() take a Windows
# process tree down; it needs tests/ on the module path.
case "$PYTHON" in
    *.exe) export PYTHONPATH="$TESTS_DIR${PYTHONPATH:+;$PYTHONPATH}" ;;
    *)     export PYTHONPATH="$TESTS_DIR${PYTHONPATH:+:$PYTHONPATH}" ;;
esac
echo "python: $PYTHON"

# GNU `timeout` is not a hard requirement -- stock macOS has no `timeout` at
# all (only `gtimeout` if coreutils is installed via Homebrew). Fall back to
# gtimeout, else run every suite with no per-suite time limit and say so once,
# rather than failing the whole run before a single test gets to execute.
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_BIN="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_BIN="gtimeout"
else
    TIMEOUT_BIN=""
    echo "NOTE: no 'timeout' or 'gtimeout' on PATH -- running suites with no per-suite time limit."
fi

# Build the suite list: explicit args win, else all tests/test_*.py, minus $SKIP glob(s).
if [ "$#" -gt 0 ]; then
    SUITES=("$@")
else
    SUITES=("$TESTS_DIR"/test_*.py)
fi

if [ -n "$SKIP" ]; then
    filtered=()
    for s in "${SUITES[@]}"; do
        base="$(basename "$s")"
        skip_it=0
        for pat in $SKIP; do
            # shellcheck disable=SC2053
            [[ "$base" == $pat ]] && skip_it=1
        done
        [ "$skip_it" -eq 0 ] && filtered+=("$s")
    done
    SUITES=("${filtered[@]}")
fi

# An explicit suite that is not a file is a typo, not a pass (it used to be skipped silently).
if [ "$#" -gt 0 ]; then
    for s in "$@"; do
        if [ ! -f "$s" ]; then
            echo "error: no such test file: $s (pass tests/test_*.py paths, or no arguments to run them all)" >&2
            exit 2
        fi
    done
fi
if [ "${#SUITES[@]}" -eq 0 ]; then
    echo "error: no suites to run" >&2
    exit 1
fi

CURRENT_SCRATCH=""

cleanup_current() {
    if [ -n "$CURRENT_SCRATCH" ] && [ -d "$CURRENT_SCRATCH" ]; then
        rm -rf "$CURRENT_SCRATCH"
    fi
}

# A suite killed by `timeout` gets SIGTERM on its own python only; fixtures it
# started with subprocess.Popen (tests/fixtures/fake_comfy.py) survive it. A fixture
# of THIS suite is one whose argv carries `--store <path under CURRENT_SCRATCH>`:
# the per-suite scratch is unique to it, so by construction those are its own.
# PIDs are read from /proc/<pid>/cmdline (never `pgrep -f`, never by name alone) and
# only the recorded PIDs are signalled. Where there is no /proc (macOS, Git Bash on
# Windows) this does nothing; the fixture's own parent watchdog covers POSIX anyway.
reap_suite_fixtures() {
    [ -n "$CURRENT_SCRATCH" ] && [ -r /proc/self/cmdline ] || return 0
    local d pid arg prev is_fixture owned
    local -a pids=()
    for d in /proc/[0-9]*; do
        pid="${d#/proc/}"
        [ "$pid" = "$$" ] && continue
        is_fixture=0; owned=0; prev=""
        while IFS= read -r -d '' arg; do
            case "$arg" in */fake_comfy.py|fake_comfy.py) is_fixture=1 ;; esac
            if [ "$prev" = "--store" ]; then
                case "$arg" in "$CURRENT_SCRATCH"|"$CURRENT_SCRATCH"/*) owned=1 ;; esac
            fi
            prev="$arg"
        done 2>/dev/null < "$d/cmdline"
        [ "$is_fixture" -eq 1 ] && [ "$owned" -eq 1 ] && pids+=("$pid")
    done
    [ "${#pids[@]}" -gt 0 ] || return 0
    for pid in "${pids[@]}"; do
        kill "$pid" 2>/dev/null && echo "reaped fake_comfy pid $pid"
    done
    sleep 1
    for pid in "${pids[@]}"; do
        # still there after SIGTERM and still the same kind of process: force it
        if [ -r "/proc/$pid/cmdline" ] && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'fake_comfy\.py'; then
            kill -9 "$pid" 2>/dev/null
        fi
    done
}

on_interrupt() {
    reap_suite_fixtures
    cleanup_current
    echo "interrupted"
    exit 130
}
trap on_interrupt INT TERM

declare -a RESULTS=()
FAIL_COUNT=0

for suite_path in "${SUITES[@]}"; do
    [ -f "$suite_path" ] || continue
    name="$(basename "$suite_path")"
    ts="$(date +%Y%m%d-%H%M%S)"
    CURRENT_SCRATCH="$SCRATCH_ROOT/${name%.py}-$ts"
    mkdir -p "$CURRENT_SCRATCH"
    # Never read the real data/ (it can hold a user's own data packs, which every
    # suite that walks all packs would then run): each suite gets an empty data dir.
    export GENCENTER_DATA="$CURRENT_SCRATCH/data"

    start=$(date +%s)
    if [ -n "$TIMEOUT_BIN" ]; then
        TMPDIR="$CURRENT_SCRATCH" "$TIMEOUT_BIN" "$TIMEOUT" "$PYTHON" "$suite_path"
    else
        TMPDIR="$CURRENT_SCRATCH" "$PYTHON" "$suite_path"
    fi
    exit_code=$?
    end=$(date +%s)
    secs=$((end - start))

    # Delete scratch unconditionally, whatever the exit code (including timeout=124, killed=137).
    reap_suite_fixtures
    cleanup_current
    CURRENT_SCRATCH=""

    echo "$name exit=$exit_code secs=$secs"
    RESULTS+=("$name exit=$exit_code secs=$secs")
    [ "$exit_code" -ne 0 ] && FAIL_COUNT=$((FAIL_COUNT + 1))
done

echo "---"
echo "summary: ${#RESULTS[@]} suite(s) run, $FAIL_COUNT failed"
for r in "${RESULTS[@]}"; do
    echo "  $r"
done

[ "$FAIL_COUNT" -eq 0 ]
