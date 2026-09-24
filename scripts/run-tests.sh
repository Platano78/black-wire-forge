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

CURRENT_SCRATCH=""

cleanup_current() {
    if [ -n "$CURRENT_SCRATCH" ] && [ -d "$CURRENT_SCRATCH" ]; then
        rm -rf "$CURRENT_SCRATCH"
    fi
}

on_interrupt() {
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

    start=$(date +%s)
    if [ -n "$TIMEOUT_BIN" ]; then
        TMPDIR="$CURRENT_SCRATCH" "$TIMEOUT_BIN" "$TIMEOUT" python3 "$suite_path"
    else
        TMPDIR="$CURRENT_SCRATCH" python3 "$suite_path"
    fi
    exit_code=$?
    end=$(date +%s)
    secs=$((end - start))

    # Delete scratch unconditionally, whatever the exit code (including timeout=124, killed=137).
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
