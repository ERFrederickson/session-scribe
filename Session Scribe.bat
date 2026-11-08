@echo off
rem Double-click to run. The first run sets everything up (takes a few minutes).
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Setting up Session Scribe for the first time...
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
  if errorlevel 1 goto :nopython
)

rem Install or update packages whenever requirements.txt changes.
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>&1
if errorlevel 1 (
  echo Installing packages...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :pipfailed
  copy /y requirements.txt ".venv\installed-requirements.txt" >nul
)

if /I "%~1"=="check" (
  ".venv\Scripts\python.exe" -m session_scribe check
  pause
  exit /b
)
start "" ".venv\Scripts\pythonw.exe" -m session_scribe
exit /b

:nopython
echo Could not create a Python environment. Install Python 3.12 from python.org and try again.
pause
exit /b 1

:pipfailed
echo Installing packages failed. See the messages above.
pause
exit /b 1
