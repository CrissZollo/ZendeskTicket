@echo off
setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

set "PY_VER=3.13.5"
set "PY_ZIP=python-%PY_VER%-embed-amd64.zip"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/%PY_ZIP%"
set "PY_DEST=%~dp0python"
set "LOCAL_PY=%PY_DEST%\python.exe"

REM 1. Prefer the official py launcher (always real, never the MS Store stub).
where py >nul 2>nul
if not errorlevel 1 (
    py -3 zdweb.py
    goto :end
)

REM 2. Try real python.exe on PATH; skip the MS Store WindowsApps stub
REM    (0-byte App Execution Alias under %LOCALAPPDATA%\Microsoft\WindowsApps).
set "REAL_PY="
for /f "delims=" %%I in ('where python 2^>nul') do (
    if not defined REAL_PY (
        set "CAND=%%I"
        set "CAND_SIZE=%%~zI"
        echo !CAND! | findstr /i "WindowsApps" >nul
        if errorlevel 1 (
            if !CAND_SIZE! GTR 0 set "REAL_PY=!CAND!"
        )
    )
)
if defined REAL_PY (
    "!REAL_PY!" zdweb.py
    goto :end
)

REM 3. Reuse a previously provisioned local Python.
if exist "%LOCAL_PY%" (
    "%LOCAL_PY%" zdweb.py
    goto :end
)

REM 4. Provision a portable Python next to the app.
echo.
echo Python was not found. Setting up a portable Python (one-time, ~10 MB) ...
echo Source: %PY_URL%
echo.

set "PY_TMP=%TEMP%\%PY_ZIP%"
if exist "%PY_TMP%" del /q "%PY_TMP%" >nul 2>nul

set "DOWNLOAD_OK="
where curl.exe >nul 2>nul
if not errorlevel 1 (
    curl.exe -fL --retry 2 -o "%PY_TMP%" "%PY_URL%"
    if not errorlevel 1 set "DOWNLOAD_OK=1"
)
if not defined DOWNLOAD_OK (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]'Tls12,Tls13'; try { Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%PY_TMP%' -UseBasicParsing; exit 0 } catch { exit 1 }"
    if not errorlevel 1 set "DOWNLOAD_OK=1"
)
if not defined DOWNLOAD_OK goto :download_failed
if not exist "%PY_TMP%" goto :download_failed

if not exist "%PY_DEST%" mkdir "%PY_DEST%"

set "EXTRACT_OK="
where tar.exe >nul 2>nul
if not errorlevel 1 (
    tar -xf "%PY_TMP%" -C "%PY_DEST%"
    if not errorlevel 1 set "EXTRACT_OK=1"
)
if not defined EXTRACT_OK (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%PY_TMP%' -DestinationPath '%PY_DEST%' -Force"
    if not errorlevel 1 set "EXTRACT_OK=1"
)
del /q "%PY_TMP%" >nul 2>nul

if not defined EXTRACT_OK goto :extract_failed
if not exist "%LOCAL_PY%" goto :extract_failed

echo.
echo Python is ready. Starting the app ...
echo.
"%LOCAL_PY%" zdweb.py
goto :end

:download_failed
echo.
echo Could not download Python automatically.
echo Check your internet connection, or install Python manually:
echo   https://www.python.org/downloads/
echo During install, tick "Add Python to PATH".
echo.
goto :end

:extract_failed
echo.
echo Could not extract the downloaded Python archive.
echo You can install Python manually instead:
echo   https://www.python.org/downloads/
echo.
goto :end

:end
endlocal
pause
