@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
set "PAQU_OPEN_BROWSER=1"

if not exist "%PYTHON_EXE%" (
  echo Python executable not found:
  echo %PYTHON_EXE%
  echo.
  echo Please update PYTHON_EXE in start_server.bat.
  pause
  exit /b 1
)

"%PYTHON_EXE%" -c "import certifi, imageio_ffmpeg, scrapling, yt_dlp, yt_dlp.extractor.extractors, yt_dlp.extractor.tiktok" >nul 2>nul
if errorlevel 1 (
  echo Installing or repairing Python dependencies...
  "%PYTHON_EXE%" -m pip install --upgrade --force-reinstall -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Failed to install Python dependencies.
    echo Please check your network connection and requirements.txt.
    pause
    exit /b 1
  )
echo.
)

"%PYTHON_EXE%" agent_core.py --init-db
if errorlevel 1 (
  echo Failed to initialize the Agent database.
  pause
  exit /b 1
)

echo Starting paqu video crawler...
echo The Agent page will open automatically: http://127.0.0.1:8080/agent
echo Keep this window open while using the tool.
echo.

"%PYTHON_EXE%" web_app.py

echo.
echo Server stopped.
pause
