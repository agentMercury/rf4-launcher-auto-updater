@echo off
rem RF4 launcher auto-updater - background watcher (hidden window).
rem Optional: put a shortcut of this file in shell:startup to auto-run at logon.
setlocal
set "HERE=%~dp0"
set "PYW="
set "PY="
for %%I in (pythonw.exe) do set "PYW=%%~$PATH:I"
for %%I in (python.exe) do set "PY=%%~$PATH:I"
if not defined PYW if exist "%LOCALAPPDATA%\Python\pythoncore-3.15-64\pythonw.exe" set "PYW=%LOCALAPPDATA%\Python\pythoncore-3.15-64\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Python\pythoncore-3.15-64\python.exe" set "PY=%LOCALAPPDATA%\Python\pythoncore-3.15-64\python.exe"
if not defined PYW exit /b 1
start "" "%PYW%" "%HERE%rf4_updater.py" watch
exit /b 0
