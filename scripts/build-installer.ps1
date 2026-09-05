$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $projectRoot
$exe = Join-Path $projectRoot "dist\THMedia.exe"
if (-not (Test-Path -LiteralPath $exe)) { throw "Build dist\THMedia.exe first" }

$prereqDir = Join-Path $projectRoot "installer\prereqs"
$webView2Bootstrapper = Join-Path $prereqDir "MicrosoftEdgeWebview2Setup.exe"
$webView2Url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
New-Item -ItemType Directory -Path $prereqDir -Force | Out-Null
Write-Host "Downloading Microsoft Edge WebView2 Evergreen Bootstrapper..."
Invoke-WebRequest -Uri $webView2Url -OutFile $webView2Bootstrapper -UseBasicParsing
$webView2Signature = Get-AuthenticodeSignature -LiteralPath $webView2Bootstrapper
if ($webView2Signature.Status -ne "Valid") {
  throw "WebView2 bootstrapper Authenticode signature is not valid: $($webView2Signature.Status)"
}
if (-not $webView2Signature.SignerCertificate -or $webView2Signature.SignerCertificate.Subject -notmatch "Microsoft") {
  throw "WebView2 bootstrapper signer is not Microsoft Corporation"
}
$webView2Hash = (Get-FileHash -LiteralPath $webView2Bootstrapper -Algorithm SHA256).Hash
Write-Host "Verified Microsoft WebView2 bootstrapper SHA-256: $webView2Hash"

$isccPath = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
if (-not $isccPath) {
  $candidates = @(
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
  )
  $isccPath = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}
if (-not $isccPath) { throw "Inno Setup 6 compiler (ISCC.exe) is not installed" }

& $isccPath "installer\THMedia.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
$setup = Get-ChildItem "dist\installer\THMedia-Setup-*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $setup) { throw "Installer output was not created" }
Write-Host "Created installer: $($setup.FullName)"
Remove-Item -LiteralPath $webView2Bootstrapper -Force -ErrorAction SilentlyContinue
