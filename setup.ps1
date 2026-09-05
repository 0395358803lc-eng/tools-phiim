$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Unable to create .venv" }
}

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$constraints = Join-Path $projectRoot "requirements.lock.txt"
if (-not (Test-Path -LiteralPath $constraints)) {
    throw "requirements.lock.txt is required for reproducible setup"
}

$flowCliWheel = Get-ChildItem -LiteralPath (Join-Path $projectRoot "vendor") -Filter "flow_cli-*.whl" | Select-Object -First 1
if (-not $flowCliWheel) {
    throw "Vendored Flow CLI wheel not found"
}

& $python -m pip install -c $constraints $flowCliWheel.FullName
if ($LASTEXITCODE -ne 0) { throw "Flow CLI install failed" }

& $python -m pip install -c $constraints -e .
if ($LASTEXITCODE -ne 0) { throw "Project dependency install failed" }

& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium install failed" }

Write-Host "Setup completed. Run .\start.ps1 to launch TH Media."
