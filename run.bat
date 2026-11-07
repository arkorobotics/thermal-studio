@echo off
rem Thermal Studio launcher (Windows)
rem   run.bat           real PureThermal camera (no admin, no driver changes)
rem   run.bat --demo    simulated camera, no hardware needed
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment ^(one-time^)...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist ".venv\Scripts\python.exe" (
    echo Python 3 not found. Install it from https://www.python.org/downloads/
    echo ^(tick "Add python.exe to PATH"^), then run this again.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
)

".venv\Scripts\python.exe" -c "import numpy, cv2" >nul 2>nul || (
  echo Installing packages...
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || (pause & exit /b 1)
)

echo Close the Camera app, Teams, Zoom, OBS and browser tabs using the camera first.
".venv\Scripts\python.exe" thermal_app.py %*
if errorlevel 1 pause
