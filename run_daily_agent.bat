@echo off
setlocal
cd /d "%~dp0"

call :find_python
if errorlevel 1 exit /b 2

"%PYTHON_EXE%" %PYTHON_ARGS% agent_core.py --limit 100 --window-days 30
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
