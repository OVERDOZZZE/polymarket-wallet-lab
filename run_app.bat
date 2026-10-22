@echo off
rem Starts Wallet Lab: http://127.0.0.1:8000  (close this window to stop the server; bots keep running)
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating Python environment...
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist frontend\dist\index.html (
  if exist .tools\node\npm.cmd (
    echo Building the interface...
    pushd frontend
    if not exist node_modules ..\.tools\node\npm.cmd install --no-fund --no-audit
    ..\.tools\node\npm.cmd run build
    popd
  ) else (
    echo frontend\dist is missing and .tools\node is not installed - see STRUCTURE.md section 2.
  )
)
.venv\Scripts\python -m backend
pause
