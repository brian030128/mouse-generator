@echo off
setlocal
cd /d "%~dp0"
where pythonw.exe >nul 2>&1
if not errorlevel 1 (
    start "" pythonw.exe "%~dp0recorder.py" --start
    exit /b
)
where pyw.exe >nul 2>&1
if not errorlevel 1 (
    start "" pyw.exe -3 "%~dp0recorder.py" --start
    exit /b
)
echo Python was not found. Install Python 3.10 or newer with Tk and add it to PATH.
pause
