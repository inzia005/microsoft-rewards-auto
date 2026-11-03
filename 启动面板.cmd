@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  set "PY=.venv\Scripts\pythonw.exe"
) else (
  set "PY=pythonw"
)
start "" %PY% -X utf8 "%~dp0serve.py"
timeout /t 2 >nul
start "" "http://127.0.0.1:8765"
