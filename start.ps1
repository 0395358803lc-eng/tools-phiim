$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Environment is not set up. Run .\setup.ps1 first."
}

& $python -c "import flow_story_studio"
if ($LASTEXITCODE -ne 0) {
    throw "Project dependencies are incomplete. Run .\setup.ps1 again."
}

& $python -m flow_story_studio.desktop
if ($LASTEXITCODE -ne 0) {
    throw "TH Media exited with code $LASTEXITCODE"
}
