@echo off
REM Black Wire Forge launcher -- do not edit. Create start-user.bat to override (see start-user.example.bat).
cd /d "%~dp0"

if exist "%~dp0start-user.bat" call "%~dp0start-user.bat"

set "FOUND="
if defined PYTHON (
    "%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>&1 && set "FOUND=%PYTHON%"
)
if not defined FOUND (
    for %%p in (py python) do (
        if not defined FOUND (
            %%p -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)" >nul 2>&1 && set "FOUND=%%p"
        )
    )
)

set "LOGFILE=%~dp0start.log"

if not defined FOUND (
    echo Python 3.8 or newer is needed. Install it from python.org ^(on Windows, tick 'Add python.exe to PATH'^), then run this again.
    pause
    exit /b 1
)

echo Black Wire Forge is starting. Your browser will open. Close this window to stop it.
set "PYTHONUTF8=1"
%FOUND% server.py --open %BWF_ARGS% >"%LOGFILE%" 2>&1
if errorlevel 1 (
    echo.
    echo Black Wire Forge stopped. The last lines of start.log are below. Common causes: another copy is already running on port 3998, or a settings file has a typo.
    echo.
    powershell -NoProfile -Command "Get-Content -Tail 15 -LiteralPath '%LOGFILE%'"
    pause
    exit /b 1
)
