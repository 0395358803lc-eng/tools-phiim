$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $projectRoot
if (-not $env:FLOW_STUDIO_CERT_THUMBPRINT) {
  throw "FLOW_STUDIO_CERT_THUMBPRINT must be configured for a production release"
}

& powershell -ExecutionPolicy Bypass -File scripts\release-check.ps1
if ($LASTEXITCODE -ne 0) { throw "Release quality gate failed" }
& powershell -ExecutionPolicy Bypass -File build-exe.ps1
if ($LASTEXITCODE -ne 0) { throw "EXE build failed" }
& powershell -ExecutionPolicy Bypass -File scripts\smoke-exe.ps1
if ($LASTEXITCODE -ne 0) { throw "EXE smoke failed" }
& powershell -ExecutionPolicy Bypass -File scripts\sign-artifact.ps1 -Path dist\THMedia.exe
if ($LASTEXITCODE -ne 0) { throw "EXE signing failed" }
& powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1
if ($LASTEXITCODE -ne 0) { throw "Installer build failed" }
$setup = Get-ChildItem dist\installer\THMedia-Setup-*.exe | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $setup) { throw "Installer was not produced" }
& powershell -ExecutionPolicy Bypass -File scripts\sign-artifact.ps1 -Path $setup.FullName
if ($LASTEXITCODE -ne 0) { throw "Installer signing failed" }
Write-Host "Production release passed all gates: $($setup.FullName)"
