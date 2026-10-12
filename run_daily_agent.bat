@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%PYTHON_EXE%" exit /b 2

"%PYTHON_EXE%" agent_core.py --limit 100 --window-days 30
exit /b %errorlevel%
