@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if not exist "%PYTHON_EXE%" (
  echo Python executable not found:
  echo %PYTHON_EXE%
  echo.
  echo Please update PYTHON_EXE in start_server.bat.
  pause
  exit /b 1
)

echo Starting paqu video crawler...
echo Open http://127.0.0.1:8080/ in your browser.
echo Keep this window open while using the tool.
echo.

"%PYTHON_EXE%" web_app.py

echo.
echo Server stopped.
pause
