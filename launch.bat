@echo off
setlocal
title Edge GEX Lab
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)

REM Rebuild the web page when there is no build or a web source file is newer than it.
powershell -NoProfile -Command "$d = Get-Item 'web\dist\index.html' -ErrorAction SilentlyContinue; if (-not $d) { exit 1 }; $src = @(Get-ChildItem 'web\src' -Recurse -File) + @(Get-Item 'web\index.html','web\package.json','web\vite.config.ts' -ErrorAction SilentlyContinue); if ($src | Where-Object { $_.LastWriteTime -gt $d.LastWriteTime }) { exit 1 }; exit 0"
if errorlevel 1 (
    echo Building the web page...
    pushd web
    call npm run build --silent
    if errorlevel 1 (
        popd
        echo The web build failed. Run setup.bat again.
        pause
        exit /b 1
    )
    popd
)

if /i "%~1"=="demo" set "GEXLAB_DEMO=1"
if "%GEXLAB_PORT%"=="" set "GEXLAB_PORT=8240"

echo Edge GEX Lab on http://127.0.0.1:%GEXLAB_PORT% (close this window to stop it)
if not "%GEXLAB_NO_BROWSER%"=="1" start "" /b powershell -NoProfile -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:%GEXLAB_PORT%/'"
".venv\Scripts\python.exe" -m uvicorn gexlab.api:app --host 127.0.0.1 --port %GEXLAB_PORT%
endlocal
