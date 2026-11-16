@echo off
rem RF4 launcher auto-updater (portable entry).
rem Double-click to check for updates; if an update is needed it downloads and
rem installs it, then starts the game launcher and closes itself.
setlocal
set "HERE=%~dp0"
set "PYW="
set "PY="
for %%I in (pythonw.exe) do set "PYW=%%~$PATH:I"
for %%I in (python.exe) do set "PY=%%~$PATH:I"
if not defined PYW if exist "%LOCALAPPDATA%\Python\pythoncore-3.15-64\pythonw.exe" set "PYW=%LOCALAPPDATA%\Python\pythoncore-3.15-64\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Python\pythoncore-3.15-64\python.exe" set "PY=%LOCALAPPDATA%\Python\pythoncore-3.15-64\python.exe"
if not defined PYW (
  echo [ERROR] pythonw.exe not found. Please install Python 3 and add it to PATH.
  pause
  exit /b 1
)
start "" "%PYW%" "%HERE%rf4_updater_gui.py"
exit /b 0
