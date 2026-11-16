@echo off
rem Recreate the desktop shortcut named "Chinese Fishing 4".
rem The icon comes from cf4.ico in this folder - keep it next to this file.
setlocal
set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"
set "PYW="
set "PY="
for %%I in (pythonw.exe) do set "PYW=%%~$PATH:I"
for %%I in (python.exe) do set "PY=%%~$PATH:I"
if not defined PYW if exist "%LOCALAPPDATA%\Python\pythoncore-3.15-64\pythonw.exe" set "PYW=%LOCALAPPDATA%\Python\pythoncore-3.15-64\pythonw.exe"
if not defined PY if exist "%LOCALAPPDATA%\Python\pythoncore-3.15-64\python.exe" set "PY=%LOCALAPPDATA%\Python\pythoncore-3.15-64\python.exe"
set "PS=powershell.exe"
if not exist "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" set "PS=pwsh.exe"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%HERE%\make-shortcut.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] Failed to create the shortcut.
  pause
  exit /b 1
)
echo.
echo Done. Shortcut "Chinese Fishing 4" is on your Desktop.
timeout /t 3 >nul
exit /b 0
