@echo off
setlocal

cd /d "%~dp0"

set "PAQU_OPEN_BROWSER=1"

call :find_python
if errorlevel 1 (
  echo Python 3.10 or newer was not found.
  echo.
  echo Please install Python from https://www.python.org/downloads/
  echo During installation, enable "Add python.exe to PATH", then run this file again.
  pause
  exit /b 1
)

echo Using Python: %PYTHON_EXE% %PYTHON_ARGS%

"%PYTHON_EXE%" %PYTHON_ARGS% -c "import certifi, imageio_ffmpeg, scrapling, yt_dlp, yt_dlp.extractor.extractors, yt_dlp.extractor.tiktok" >nul 2>nul
if errorlevel 1 (
  echo Installing or repairing Python dependencies...
  "%PYTHON_EXE%" %PYTHON_ARGS% -m pip install --upgrade --force-reinstall -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Failed to install Python dependencies.
    echo Please check your network connection and requirements.txt.
    pause
    exit /b 1
  )
echo.
)

"%PYTHON_EXE%" %PYTHON_ARGS% agent_core.py --init-db
if errorlevel 1 (
  echo Failed to initialize the Agent database.
  pause
  exit /b 1
)

echo Starting paqu video crawler...
echo The Agent page will open automatically: http://127.0.0.1:8080/agent
echo Keep this window open while using the tool.
echo.

"%PYTHON_EXE%" %PYTHON_ARGS% web_app.py

echo.
echo Server stopped.
pause
exit /b %errorlevel%

:find_python
set "PYTHON_EXE="
set "PYTHON_ARGS="

if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
  goto :python_found
)

if exist "%~dp0venv\Scripts\python.exe" (
  set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"
  goto :python_found
)

where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3"
    goto :python_found
  )
)

where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=python"
    goto :python_found
  )
)

if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  set "PYTHON_EXE=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
  goto :python_found
)

exit /b 1

:python_found
exit /b 0
