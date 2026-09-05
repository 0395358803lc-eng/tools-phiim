$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    python -m venv .venv
}
$flowCliWheel = Get-ChildItem -LiteralPath (Join-Path $projectRoot "vendor") -Filter "flow_cli-*.whl" | Select-Object -First 1
if (-not $flowCliWheel) { throw "Vendored Flow CLI wheel not found" }
$constraints = Join-Path $projectRoot "requirements.lock.txt"
if (-not (Test-Path -LiteralPath $constraints)) { throw "requirements.lock.txt is required for release builds" }

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
& $python -m pip install -c $constraints $flowCliWheel.FullName
if ($LASTEXITCODE -ne 0) { throw "Flow CLI install failed" }
& $python -m pip install -c $constraints -e ".[build]"
if ($LASTEXITCODE -ne 0) { throw "Build dependency install failed" }
& $python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw "Playwright Chromium install failed" }

$browserRoot = Join-Path $env:LOCALAPPDATA "ms-playwright"
if (-not (Test-Path -LiteralPath $browserRoot)) { throw "Playwright Chromium not found at $browserRoot" }
$browserMetadataPath = Join-Path $projectRoot ".venv\Lib\site-packages\playwright\driver\package\browsers.json"
$browserMetadata = Get-Content -LiteralPath $browserMetadataPath -Raw | ConvertFrom-Json
$chromiumRevision = ($browserMetadata.browsers | Where-Object name -EQ "chromium").revision
$headlessRevision = ($browserMetadata.browsers | Where-Object name -EQ "chromium-headless-shell").revision
$chromiumRoot = Join-Path $browserRoot "chromium-$chromiumRevision"
$headlessRoot = Join-Path $browserRoot "chromium_headless_shell-$headlessRevision"
if (-not (Test-Path -LiteralPath $chromiumRoot) -or -not (Test-Path -LiteralPath $headlessRoot)) {
    throw "Missing Playwright Chromium revision $chromiumRevision"
}
$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) { throw "FFmpeg is required to build last-frame continuity" }

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --noconsole `
    --name "THMedia" `
    --collect-all flow_cli `
    --collect-all webview `
    --add-data "static;static" `
    --add-data "$chromiumRoot;playwright-browsers\chromium-$chromiumRevision" `
    --add-data "$headlessRoot;playwright-browsers\chromium_headless_shell-$headlessRevision" `
    --add-binary "$ffmpeg;." `
    "desktop_launcher.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$exe = Join-Path $projectRoot "dist\THMedia.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "PyInstaller did not produce dist\THMedia.exe" }
Write-Host "Created unsigned build artifact: $exe"
