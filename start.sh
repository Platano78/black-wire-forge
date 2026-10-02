#!/usr/bin/env bash
# Black Wire Forge launcher -- do not edit. Create start-user.sh to override (see start-user.example.sh).
cd "$(dirname "$0")" || exit 1

# User overrides (optional): may set PYTHON and BWF_ARGS.
if [ -f ./start-user.sh ]; then
    . ./start-user.sh
fi

try_python() {
    "$1" --version >/dev/null 2>&1 || return 1
    "$1" -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" 2>/dev/null
}

FOUND=""
for cand in "$PYTHON" python3 python; do
    [ -n "$cand" ] || continue
    if try_python "$cand"; then FOUND="$cand"; break; fi
done

LOGFILE=./start.log

if [ -z "$FOUND" ]; then
    echo "Python 3.8 or newer is needed. Install it from python.org (on Windows, tick 'Add python.exe to PATH'), then run this again."
    read -r -p "Press Enter to close"
    exit 1
fi

# shellcheck disable=SC2086
"$FOUND" server.py --open $BWF_ARGS 2>&1 | tee "$LOGFILE"
RC=${PIPESTATUS[0]}

# 0 = clean stop; 130/143 = Ctrl+C / terminated by the user. Anything else is a failure.
if [ "$RC" -ne 0 ] && [ "$RC" -ne 130 ] && [ "$RC" -ne 143 ]; then
    echo
    echo "Black Wire Forge stopped. The last lines of start.log are above. Common causes: another copy is already running on port 3998, or a settings file has a typo."
    read -r -p "Press Enter to close"
    exit 1
fi
