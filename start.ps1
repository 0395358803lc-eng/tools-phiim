$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

$flowCliWheel = Get-ChildItem -LiteralPath (Join-Path $projectRoot "vendor") -Filter "flow_cli-*.whl" | Select-Object -First 1
if (-not $flowCliWheel) {
    throw "Vendored Flow CLI wheel not found"
}

& ".venv\Scripts\python.exe" -m pip install $flowCliWheel.FullName
& ".venv\Scripts\python.exe" -m pip install -e .
& ".venv\Scripts\python.exe" -m playwright install chromium
& ".venv\Scripts\python.exe" -m flow_story_studio.desktop
