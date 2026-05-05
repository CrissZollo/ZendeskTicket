@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 zdweb.py
    goto :end
)
where python >nul 2>nul
if not errorlevel 1 (
    python zdweb.py
    goto :end
)
echo.
echo Python 3.10 or newer is required but was not found.
echo.
echo Install it from https://www.python.org/downloads/
echo During install, tick "Add Python to PATH".
echo.
:end
pause
