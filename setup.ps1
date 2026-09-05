$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $npmUserPath = Join-Path $env:APPDATA "npm"
    $env:Path = @($machinePath, $userPath, $npmUserPath) -join ";"
}

function Ensure-WingetPackage([string]$id, [string]$displayName) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "$displayName is required. Install it manually or install winget/App Installer first."
    }
    Write-Host "Installing $displayName..."
    & $winget.Source install --id $id --exact --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Unable to install $displayName with winget" }
    Refresh-ProcessPath
}

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
if (-not $flowCliWheel) { throw "Vendored legacy Flow CLI wheel not found" }

& $python -m pip install -c $constraints $flowCliWheel.FullName
if ($LASTEXITCODE -ne 0) { throw "Legacy Flow CLI install failed" }

& $python -m pip install -c $constraints -e .
if ($LASTEXITCODE -ne 0) { throw "Project dependency install failed" }

& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium install failed" }

Refresh-ProcessPath
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Ensure-WingetPackage "OpenJS.NodeJS.LTS" "Node.js LTS"
    $node = Get-Command node -ErrorAction SilentlyContinue
}
if (-not $node) { throw "Node.js 20+ is required for gflow" }

$nodeVersionText = (& $node.Source --version).Trim().TrimStart("v")
$nodeMajor = [int]($nodeVersionText.Split(".")[0])
if ($nodeMajor -lt 20) {
    throw "Node.js 20+ is required for gflow; detected $nodeVersionText"
}

$chromeCandidates = @(
    (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
    (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
)
$chrome = $chromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $chrome) {
    Ensure-WingetPackage "Google.Chrome" "Google Chrome"
    $chrome = $chromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}
if (-not $chrome) { throw "Google Chrome is required for gflow" }

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) { throw "npm was not found after Node.js setup" }

$gflow = Get-Command gflow -ErrorAction SilentlyContinue
if (-not $gflow) {
    Write-Host "Installing gflow CLI 1.1.1..."
    & $npm.Source install -g "@swissmarley/gflow-cli@1.1.1"
    if ($LASTEXITCODE -ne 0) { throw "gflow CLI install failed" }
    Refresh-ProcessPath
    $gflow = Get-Command gflow -ErrorAction SilentlyContinue
}
if (-not $gflow) { throw "gflow was installed but is not available on PATH" }

Write-Host "Setup completed."
Write-Host "Primary Google Flow transport: gflow + Google Chrome."
Write-Host "First-time login: gflow auth login"
Write-Host "Readiness check: gflow doctor"
Write-Host "After login, run .\start.ps1 to launch TH Media."
