@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
  echo Usage: test-flow-ui.bat "C:\path\to\cookies.json"
  echo.
  echo This is a dry-run UI contract probe. It NEVER clicks Generate.
  exit /b 2
)

set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

"%PYTHON%" scripts\flow-ui-probe.py ^
  --cookie-file "%~1" ^
  --browser chrome ^
  --report data\diagnostics\flow-ui-probe.json ^
  --screenshot-on-failure data\diagnostics\flow-ui-probe-failure.png

set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Flow UI contract: PASS
  echo Model: Veo 3.1 - Lite [Lower Priority]
) else (
  echo Flow UI contract: FAIL
  echo See data\diagnostics\flow-ui-probe.json
  echo No video generation was submitted.
)
exit /b %EXIT_CODE%
