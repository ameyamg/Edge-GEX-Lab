@echo off
setlocal
title Edge GEX Lab Setup
cd /d "%~dp0"

echo Edge GEX Lab setup
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run setup.bat again.
    goto :fail
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo Python 3.11 or newer is needed. This machine has:
    python --version
    goto :fail
)

where npm >nul 2>nul
if errorlevel 1 (
    echo Node.js was not found. Install the LTS version from https://nodejs.org/ and run setup.bat again.
    goto :fail
)

echo [1/5] Creating the Python environment in .venv
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :fail
)

echo [2/5] Installing the Python packages
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :fail

echo [3/5] Installing the web packages (a minute the first time)
pushd web
call npm ci --no-audit --no-fund --loglevel=error
if errorlevel 1 (
    rem An older npm can reject a lock file written by a newer one; npm install reconciles it.
    echo       Retrying with npm install
    call npm install --no-audit --no-fund --loglevel=error
    if errorlevel 1 (
        popd
        goto :fail
    )
)

echo [4/5] Building the web page
call npm run build --silent
if errorlevel 1 (
    popd
    goto :fail
)
popd

echo [5/5] Creating .env for your Alpaca keys
if exist ".env" (
    echo       .env already exists, left as it is
) else (
    copy ".env.example" ".env" >nul
    echo       Created. Open .env and paste your Alpaca key and secret. SPX and NDX work without them.
)

echo.
echo Setup finished. Start it with launch.bat, then open http://127.0.0.1:8240
echo A new install starts empty: no saved maps and no history.
echo.
pause
exit /b 0

:fail
echo.
echo Setup stopped. Fix the problem above and run setup.bat again; finished steps are skipped or repeated safely.
pause
exit /b 1
