$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) { python -m venv .venv }
$constraints = Join-Path $projectRoot "requirements.lock.txt"
if (-not (Test-Path -LiteralPath $constraints)) { throw "requirements.lock.txt is required for release builds" }
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
& $python -m pip install -c $constraints -e ".[build]"
if ($LASTEXITCODE -ne 0) { throw "Build dependency install failed" }
$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) { throw "FFmpeg is required for QC and final merge" }
$pyiArgs = @("--noconfirm", "--clean", "--onefile", "--noconsole", "--name", "THMedia",
    "--collect-all", "webview", "--add-data", "static;static", "--add-binary", "$ffmpeg;.",
    "desktop_launcher.py")
& $python -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
$exe = Join-Path $projectRoot "dist\THMedia.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "PyInstaller did not produce dist\THMedia.exe" }
Write-Host "Created unsigned build artifact: $exe"
