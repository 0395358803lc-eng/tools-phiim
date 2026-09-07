$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Unable to create .venv" }
}
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$constraints = Join-Path $projectRoot "requirements.lock.txt"
if (-not (Test-Path -LiteralPath $constraints)) { throw "requirements.lock.txt is required" }
& $python -m pip install -c $constraints -e .
if ($LASTEXITCODE -ne 0) { throw "Project dependency install failed" }
Write-Host "Setup completed."
Write-Host "Render engine: not configured."
Write-Host "Run .\start.ps1 to launch TH Media."
