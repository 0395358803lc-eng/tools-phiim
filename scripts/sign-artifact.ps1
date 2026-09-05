param(
  [Parameter(Mandatory=$true)][string]$Path
)
$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Path)) { throw "Artifact not found: $Path" }
$thumbprint = $env:FLOW_STUDIO_CERT_THUMBPRINT
if (-not $thumbprint) {
  throw "FLOW_STUDIO_CERT_THUMBPRINT is not configured; refusing to create an unsigned production release"
}
$signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
  Sort-Object FullName -Descending | Select-Object -First 1
if (-not $signtool) { throw "signtool.exe was not found in the Windows SDK" }
& $signtool.FullName sign /sha1 $thumbprint /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $Path
if ($LASTEXITCODE -ne 0) { throw "signtool failed with exit code $LASTEXITCODE" }
$signature = Get-AuthenticodeSignature -LiteralPath $Path
if ($signature.Status -ne "Valid") { throw "Signature verification failed: $($signature.Status)" }
Write-Host "Signed and verified: $Path"
