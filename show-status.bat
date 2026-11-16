@echo off
rem RF4 launcher auto-updater - show status (read only).
setlocal
set "HERE=%~dp0"
set "PYW="
set "PY="
for %%I in (pythonw.exe) do set "PYW=%%~$PATH:I"
for %%I in (python.exe) do set "PY=%%~$PATH:I"
if not defined PYW if exist "%LOCALAPPDATA%\Python\pythoncore-3.15-64\pythonw.exe" set "PYW=%LOCALAPPDATA%\Python\pythoncore-3.15-64\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Python\pythoncore-3.15-64\python.exe" set "PY=%LOCALAPPDATA%\Python\pythoncore-3.15-64\python.exe"
if not defined PY (
  echo [ERROR] python.exe not found.
  pause
  exit /b 1
)
"%PY%" "%HERE%rf4_updater.py" status
echo.
pause
